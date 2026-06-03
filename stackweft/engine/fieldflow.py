"""Field Flow Graph engine — deterministic Shadow Field Cloning.

Keeps the LLM out of the contract step: given a Skill slot-spec and a shadow
field that already exists in the repo, it greps the shadow field's real
cross-stack path, fills in verified paths + anchors for the new field, validates
fail-fast (a bad/incomplete graph never reaches generate), and exposes a per-slot
evidence check that the cross_stack gate and targeted repair both use.

Pure stdlib + grep via tools.Sandbox. No model calls.
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Any

from stackweft.engine import tools

_FIELD_RE = re.compile(r"\b([a-z][a-z0-9]*[A-Z][A-Za-z0-9]*)\b")  # camelCase token
_WORD_RE = re.compile(r"\b([a-z][a-z0-9_]{2,})\b")  # lowercase identifier (>=3 chars)
# Tokens that look like field names but are really requirement vocabulary; never
# pick these as THE new field. (Entity names/aliases + shadow are excluded
# separately from the Skill meta, so this stays domain-agnostic.)
_FIELD_STOP = frozenset({
    "add", "new", "field", "fields", "the", "and", "for", "with", "api", "get",
    "set", "put", "create", "update", "edit", "show", "page", "detail", "form",
    "list", "card", "value", "type", "string", "number", "boolean", "text",
    "json", "payload", "model", "controller", "migration", "route", "service",
    "frontend", "backend", "stack", "full", "support", "return", "returns",
})


# ----------------------------------------------------------------------------- skill spec

def load_skill_spec(skill_text: str) -> dict[str, Any]:
    """Extract the ```json {...}``` slot spec block from a Skill markdown file."""
    m = re.search(r"```json\s*(\{.*?\})\s*```", skill_text, re.DOTALL)
    if not m:
        raise ValueError("skill has no ```json slot spec block")
    return json.loads(m.group(1))


def skill_meta(skill_text: str) -> dict[str, str]:
    """Pull simple ``key: value`` frontmatter (shadow_field, entity, default_type)."""
    out: dict[str, str] = {}
    fm = re.search(r"^---\s*(.*?)\s*---", skill_text, re.DOTALL)
    if fm:
        for line in fm.group(1).splitlines():
            if ":" in line and not line.strip().startswith(("-", "#", ">")):
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
    return out


def guess_field(requirement: str, spec_meta: dict[str, str] | None = None) -> str:
    """Deterministically pick the new field/symbol name from the requirement.
    Falls back to the Skill's declared ``default_field`` (e.g. a computed name
    like readingTime that won't appear verbatim in a Chinese requirement)."""
    meta = spec_meta or {}
    shadow = meta.get("shadow_field", "")
    # 1) camelCase is the strongest signal (coverImage, thumbnailUrl, …).
    cands = [c for c in _FIELD_RE.findall(requirement) if c != shadow]
    if cands:
        return cands[0]
    # 2) Otherwise pick the most frequent lowercase identifier that isn't the
    #    shadow field, the entity (or its aliases), or generic requirement vocab.
    #    Handles plain names like "subtitle" in a Chinese requirement.
    excluded = set(_FIELD_STOP) | {shadow.lower()}
    excluded.add((meta.get("entity", "") or "").lower())
    for a in (meta.get("entity_aliases", "") or "").split(","):
        excluded.add(a.strip().lower())
    counts: dict[str, int] = {}
    order: list[str] = []
    for w in _WORD_RE.findall(requirement):
        if w in excluded:
            continue
        if w not in counts:
            order.append(w)
        counts[w] = counts.get(w, 0) + 1
    if order:
        pos = {w: i for i, w in enumerate(order)}  # first-seen position
        order.sort(key=lambda w: (-counts[w], pos[w]))  # freq desc, then first seen
        return order[0]
    # 3) Skill-declared computed default (e.g. readingTime) or generic fallback.
    return meta.get("default_field", "newField")


# ----------------------------------------------------------------------------- graph build

# DISPLAY kind drives the render shape + probe. Keyed on the field NAME only (so it
# matches between recipe record/lookup and the graph build, regardless of language):
#   image  → <img src>      (media URL)
#   bool   → <span> badge   (flag shown only when true)
#   text   → <p>            (everything else: string / number / date — rendered as text)
_IMAGE_FIELD_RE = re.compile(
    r"(image|img|photo|picture|pic|avatar|banner|cover|thumb|thumbnail|icon|logo|url|src)$",
    re.I)
_BOOL_FIELD_RE = re.compile(
    r"^(is|has|can|should|allow|enable|disable)[A-Z0-9_]"
    r"|(featured|enabled|disabled|active|inactive|visible|hidden|published|draft|pinned"
    r"|locked|archived|verified|approved|public|private|deleted|starred|done|sticky)$", re.I)


def display_kind(field: str) -> str:
    if _IMAGE_FIELD_RE.search(field):
        return "image"
    if _BOOL_FIELD_RE.search(field):
        return "bool"
    return "text"


# SQL/Sequelize TYPE (independent of display kind; only affects model+migration).
_DATE_REQ_RE = re.compile(r"日期|时间|datetime|timestamp|\bdate\b|时刻|期限", re.I)
# Conservative INTEGER cues — avoid short tokens that collide as substrings
# (e.g. 个数 inside 一个数据库). Default stays STRING when unsure.
_INT_REQ_RE = re.compile(r"数量|计数器?|次数|整数|阅读量|浏览量|点赞数|评论数|库存量|排序值|权重值|"
                         r"\bcount\b|\bnumber\b|\binteger\b|priority", re.I)
INDEX_REQ_RE = re.compile(r"索引|加.*index|建.*index|唯一约束|\bindex\b|\bunique\b", re.I)


def guess_type(requirement: str, field: str, meta: dict[str, str] | None = None) -> str:
    """Sequelize column type from the field name + requirement cues."""
    dk = display_kind(field)
    if dk == "bool":
        return "BOOLEAN"
    if dk == "image":
        return "STRING"
    if _DATE_REQ_RE.search(requirement):
        return "DATE"
    if _INT_REQ_RE.search(requirement):
        return "INTEGER"
    return (meta or {}).get("default_type", "STRING")


def _fill(template: str | None, *, field: str, shadow: str, type_: str,
          extra: dict[str, str] | None = None) -> str | None:
    if template is None:
        return None
    out = (template.replace("{field_lower}", field.lower())
           .replace("{field}", field).replace("{shadow}", shadow)
           .replace("{type}", type_))
    if extra:  # kind-specific tokens (render snippets, placeholder, tag)
        for k, v in extra.items():
            out = out.replace("{" + k + "}", v)
    return out


def _find_file(sb: tools.Sandbox, glob: str) -> str | None:
    for fp in sorted(sb.root.rglob("*")):
        if not fp.is_file() or any(p in tools._SKIP_DIRS for p in fp.parts):
            continue
        rel = str(fp.relative_to(sb.root))
        if fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(fp.name, glob):
            return rel
    return None


def _grep_lines(sb: tools.Sandbox, rel: str, pattern: str) -> list[int]:
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    lines = (sb.root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    return [i + 1 for i, ln in enumerate(lines) if rx.search(ln)]


def build_graph(sb: tools.Sandbox, skill_text: str, *,
                field: str, type_: str | None = None,
                want_index: bool = False) -> dict[str, Any]:
    """Return a TaskIR dict: {intent, entity, field, shadow_field, slots:[node...],
    hard_gates, errors:[...]}. ``errors`` non-empty ⇒ contract is invalid."""
    spec = load_skill_spec(skill_text)
    meta = skill_meta(skill_text)
    shadow = meta.get("shadow_field", "description")
    type_ = type_ or meta.get("default_type", "STRING")

    # Type-aware render: image-URL → <img>, boolean flag → <span> badge (shown when
    # true), everything else → <p> text. The sentinel matches the kind so the probe
    # asserts the right thing (img[src] / badge marker / text content in the DOM).
    kind = display_kind(field)
    # entity var + table from the skill (auto-profiled per repo), defaulting to the
    # reference repo so existing behaviour is unchanged.
    entity_var = (meta.get("entity") or "Article")
    ev = entity_var[:1].lower() + entity_var[1:]  # "Article" → "article", "product" → "product"
    table = spec.get("table") or (entity_var + "s")
    cls = f"{ev}-{field}"
    if kind == "image":
        sentinel = f"https://example.com/__{field}_probe__.png"
        render_list = ('{__EV__.__F__ && <img src={__EV__.__F__} alt="__F__" '
                       'className="__CLS__" />}')
        render_detail = '{__F__ && <img src={__F__} alt="__F__" className="__CLS__" />}'
        render_tag, form_placeholder = "<img", "__F__ URL"
    elif kind == "bool":
        sentinel = cls  # the badge class is the marker the probe looks for
        render_list = '{__EV__.__F__ && <span className="__CLS__">__F__</span>}'
        render_detail = '{__F__ && <span className="__CLS__">__F__</span>}'
        render_tag, form_placeholder = "<span", "__F__ (true/false)"
    else:
        sentinel = f"__{field}_probe_text__"
        render_list = '{__EV__.__F__ && <p className="__CLS__">{__EV__.__F__}</p>}'
        render_detail = '{__F__ && <p className="__CLS__">{__F__}</p>}'
        render_tag, form_placeholder = "<p", "__F__"
    # optional DB index on the new column (requirement asked for an index)
    index_up = (f'    await queryInterface.addIndex("{table}", ["{field}"]);\n'
                if want_index else "")
    _sub = lambda s: s.replace("__EV__", ev).replace("__F__", field).replace("__CLS__", cls)
    extra = {"render_list": _sub(render_list), "render_detail": _sub(render_detail),
             "render_tag": render_tag, "form_placeholder": _sub(form_placeholder),
             "display_kind": kind, "index_up": index_up, "table": table}

    nodes: list[dict[str, Any]] = []
    errors: list[str] = []
    skipped: list[dict[str, Any]] = []
    for raw in spec.get("slots", []):
        # P1: a slot may be optional (required=false) and/or offer alternative globs.
        # Real repos differ by layer ABSENCE / MERGE / RENAME (e.g. no services/ dir),
        # not just folder names — so an unresolved OPTIONAL slot is skipped (not fail-fast).
        required = raw.get("required", True)
        node: dict[str, Any] = {
            "slot": raw["slot"], "layer": raw["layer"], "kind": raw["kind"],
            "evidence": [_fill(e, field=field, shadow=shadow, type_=type_, extra=extra)
                         for e in raw.get("evidence", [])],
            "min_count": raw.get("min_count", 1),
            "requirement_delta": raw.get("requirement_delta", False),
        }
        if raw["kind"] == "edit":
            globs = raw.get("alternatives") or [raw["glob"]]
            path = next((p for g in globs if (p := _find_file(sb, g))), None)
            node["path"] = path
            node["instruction"] = _fill(raw.get("instruction"), field=field,
                                        shadow=shadow, type_=type_, extra=extra)
            if raw.get("det"):  # deterministic 0-LLM edit directive (low-entropy slot)
                node["det"] = {"op": raw["det"].get("op", "insert_after_anchor"),
                               "line": _fill(raw["det"].get("line"), field=field,
                                             shadow=shadow, type_=type_, extra=extra),
                               "shadow": shadow, "field": field}  # for clone_shadow_line
            if path is None:
                if not required:
                    skipped.append({"slot": raw["slot"], "reason": f"optional: no file matches {globs}"})
                    continue
                errors.append(f"{raw['slot']}: no file matches glob {globs!r}")
                node["anchors"] = []
            else:
                anchor_re = _fill(raw.get("shadow_anchor"), field=field,
                                  shadow=shadow, type_=type_, extra=extra)
                if anchor_re is None:  # requirement delta — shadow absent here
                    anchor_re = _fill(raw.get("anchor_fallback"), field=field,
                                      shadow=shadow, type_=type_, extra=extra)
                hits = _grep_lines(sb, path, anchor_re) if anchor_re else []
                node["anchor_pattern"] = anchor_re
                node["anchors"] = hits
                if not hits:
                    if not required:
                        skipped.append({"slot": raw["slot"],
                                        "reason": f"optional: anchor /{anchor_re}/ not found in {path}"})
                        continue
                    errors.append(f"{raw['slot']}: anchor /{anchor_re}/ not found in {path}")
        else:  # new_file
            node["path"] = _fill(raw["path_template"], field=field, shadow=shadow,
                                 type_=type_, extra=extra)
            template = raw["template"]
            if template == "test_list_render_probe":
                # text → assert DOM text; bool → assert the badge marker; image keeps img[src]
                template = {"text": "test_list_render_text_probe",
                            "bool": "test_list_render_bool_probe"}.get(kind, template)
            elif template == "test_write_payload_probe" and kind == "bool":
                template = "test_write_payload_bool_probe"  # value is boolean, assert the key
            node["template"] = template
        nodes.append(node)

    # hard gate (only if the Skill declares it): render must cover list AND detail.
    slot_names = {n["slot"] for n in nodes}
    if "frontend_render_has_list_and_detail" in spec.get("hard_gates", []):
        if not ({"frontend_list_render", "frontend_detail_render"} <= slot_names):
            errors.append("frontend_render_has_list_and_detail: missing list or detail slot")

    return {
        "intent": spec.get("intent") or meta.get("intent", "add_entity_field"),
        "entity": meta.get("entity", "Article"),
        "field": field, "type": type_, "shadow_field": shadow, "display_kind": kind,
        "sentinel": sentinel, "index_up": index_up, "table": table,
        "hard_gates": spec.get("hard_gates", []),
        "clarify_questions": spec.get("clarify_questions", []),
        "slots": nodes, "errors": errors, "skipped": skipped,
    }


# ----------------------------------------------------------------------------- new-file templates

_MIGRATION = '''"use strict";
// Auto-generated by StackWeft: add the {field} column to {table}.
module.exports = {{
  async up(queryInterface, Sequelize) {{
    await queryInterface.addColumn("{table}", "{field}", {{
      type: Sequelize.{type},
      allowNull: true,
    }});
{index_up}  }},
  async down(queryInterface) {{
    await queryInterface.removeColumn("{table}", "{field}");
  }},
}};
'''

_TEST_BACKEND_ATTR = '''"use strict";
// StackWeft cross-stack probe (backend): the Article model must DEFINE {field}.
import {{ describe, it, expect }} from "vitest";
import {{ createRequire }} from "module";
const require = createRequire(import.meta.url);
const path = require("path");
const {{ loadModelAttributes }} = require("./model.helper.js");

describe("Article.{field} attribute (cross-stack probe)", () => {{
  it("defines the {field} column", () => {{
    const {{ columns }} = loadModelAttributes(path.resolve(__dirname, "../models/Article.js"));
    expect(columns).toContain("{field}");
  }});
}});
'''

_TEST_LIST_RENDER = '''import {{ render }} from "@testing-library/react";
import {{ MemoryRouter }} from "react-router-dom";
import {{ vi }} from "vitest";

// ArticlesPreview pulls in FavButton -> useAuth(); stub the context so the card
// renders in isolation. We probe only that {field} reaches the DOM.
vi.mock("../../context/AuthContext", () => ({{
  useAuth: () => ({{ headers: {{}}, isAuth: false, loggedUser: {{ username: "u" }} }}),
}}));

import ArticlesPreview from "./ArticlesPreview";

const PROBE = "{sentinel}";

test("ArticlesPreview renders the {field} cover image (cross-stack probe)", () => {{
  const articles = [{{
    slug: "probe-slug", title: "t", description: "d", {field}: PROBE,
    tagList: [], createdAt: "2020-01-01T00:00:00.000Z", favorited: false,
    favoritesCount: 0, author: {{ username: "u", image: "", following: false }},
  }}];
  const {{ container }} = render(
    <MemoryRouter>
      <ArticlesPreview articles={{articles}} loading={{false}} updateArticles={{() => {{}}}} />
    </MemoryRouter>,
  );
  const img = container.querySelector(`img[src="${{PROBE}}"]`);
  expect(img).not.toBeNull();
}});
'''

_TEST_LIST_RENDER_TEXT = '''import {{ render }} from "@testing-library/react";
import {{ MemoryRouter }} from "react-router-dom";
import {{ vi }} from "vitest";

// ArticlesPreview pulls in FavButton -> useAuth(); stub the context so the card
// renders in isolation. We probe only that {field} text reaches the DOM.
vi.mock("../../context/AuthContext", () => ({{
  useAuth: () => ({{ headers: {{}}, isAuth: false, loggedUser: {{ username: "u" }} }}),
}}));

import ArticlesPreview from "./ArticlesPreview";

const PROBE = "{sentinel}";

test("ArticlesPreview renders the {field} text (cross-stack probe)", () => {{
  const articles = [{{
    slug: "probe-slug", title: "t", description: "d", {field}: PROBE,
    tagList: [], createdAt: "2020-01-01T00:00:00.000Z", favorited: false,
    favoritesCount: 0, author: {{ username: "u", image: "", following: false }},
  }}];
  const {{ container }} = render(
    <MemoryRouter>
      <ArticlesPreview articles={{articles}} loading={{false}} updateArticles={{() => {{}}}} />
    </MemoryRouter>,
  );
  expect(container.textContent).toContain(PROBE);
}});
'''

_TEST_WRITE_PAYLOAD = '''import {{ vi }} from "vitest";
import setArticle from "./setArticle";

vi.mock("axios", () => ({{
  default: vi.fn(async () => ({{ data: {{ article: {{ slug: "probe-slug" }} }} }})),
}}));
import axios from "axios";

const PROBE = "{sentinel}";

test("setArticle payload carries {field} (cross-stack probe)", async () => {{
  await setArticle({{
    headers: {{}}, slug: "probe-slug", body: "b", description: "d",
    tagList: [], title: "t", {field}: PROBE,
  }});
  expect(axios).toHaveBeenCalled();
  const sent = JSON.stringify(axios.mock.calls[0][0]);
  expect(sent).toContain(PROBE);
}});
'''

_HELPER_READING_STATS = '''// Auto-generated by StackWeft: derive display stats from article body.
export function wordCount(body) {{
  return (body || "").trim().split(/\\s+/).filter(Boolean).length;
}}
export function readingTime(body) {{
  return Math.max(1, Math.ceil(wordCount(body) / 200));
}}
'''

_TEST_STAT_RENDER = '''import {{ render }} from "@testing-library/react";
import {{ MemoryRouter }} from "react-router-dom";
import {{ vi }} from "vitest";

// ArticlesPreview pulls in FavButton -> useAuth(); stub it so the card renders.
vi.mock("../../context/AuthContext", () => ({{
  useAuth: () => ({{ headers: {{}}, isAuth: false, loggedUser: {{ username: "u" }} }}),
}}));

import ArticlesPreview from "./ArticlesPreview";

test("ArticlesPreview shows a reading-time stat badge (cross-stack probe)", () => {{
  const articles = [{{
    slug: "probe-slug", title: "t", description: "d",
    body: Array(450).fill("word").join(" "),
    tagList: [], createdAt: "2020-01-01T00:00:00.000Z", favorited: false,
    favoritesCount: 0, author: {{ username: "u", image: "", following: false }},
  }}];
  const {{ container }} = render(
    <MemoryRouter>
      <ArticlesPreview articles={{articles}} loading={{false}} updateArticles={{() => {{}}}} />
    </MemoryRouter>,
  );
  const el = container.querySelector(".article-readtime");
  expect(el).not.toBeNull();
  expect(el.textContent).toMatch(/\\d/);
}});
'''

_TEST_LIST_RENDER_BOOL = '''import {{ render }} from "@testing-library/react";
import {{ MemoryRouter }} from "react-router-dom";
import {{ vi }} from "vitest";

vi.mock("../../context/AuthContext", () => ({{
  useAuth: () => ({{ headers: {{}}, isAuth: false, loggedUser: {{ username: "u" }} }}),
}}));

import ArticlesPreview from "./ArticlesPreview";

// boolean flag: the badge (class "{sentinel}") renders only when {field} is true.
test("ArticlesPreview shows the {field} badge when true (cross-stack probe)", () => {{
  const articles = [{{
    slug: "probe-slug", title: "t", description: "d", {field}: true,
    tagList: [], createdAt: "2020-01-01T00:00:00.000Z", favorited: false,
    favoritesCount: 0, author: {{ username: "u", image: "", following: false }},
  }}];
  const {{ container }} = render(
    <MemoryRouter>
      <ArticlesPreview articles={{articles}} loading={{false}} updateArticles={{() => {{}}}} />
    </MemoryRouter>,
  );
  expect(container.querySelector(".{sentinel}")).not.toBeNull();
}});
'''

_TEST_WRITE_PAYLOAD_BOOL = '''import {{ vi }} from "vitest";
import setArticle from "./setArticle";

vi.mock("axios", () => ({{
  default: vi.fn(async () => ({{ data: {{ article: {{ slug: "probe-slug" }} }} }})),
}}));
import axios from "axios";

// boolean field: the VALUE is a bool, so we assert the key round-trips into the payload.
test("setArticle payload carries {field} (cross-stack probe)", async () => {{
  await setArticle({{
    headers: {{}}, slug: "probe-slug", body: "b", description: "d",
    tagList: [], title: "t", {field}: true,
  }});
  expect(axios).toHaveBeenCalled();
  const sent = JSON.stringify(axios.mock.calls[0][0]);
  expect(sent).toContain('"{field}"');
}});
'''

_TEMPLATES = {
    "migration_addcolumn": _MIGRATION,
    "test_backend_attr_probe": _TEST_BACKEND_ATTR,
    "test_list_render_probe": _TEST_LIST_RENDER,
    "test_list_render_text_probe": _TEST_LIST_RENDER_TEXT,
    "test_list_render_bool_probe": _TEST_LIST_RENDER_BOOL,
    "test_write_payload_probe": _TEST_WRITE_PAYLOAD,
    "test_write_payload_bool_probe": _TEST_WRITE_PAYLOAD_BOOL,
    "helper_reading_stats": _HELPER_READING_STATS,
    "test_stat_render_probe": _TEST_STAT_RENDER,
}


def _clone_token(text: str, shadow: str, field: str) -> str:
    """Clone a line by substituting the shadow token with the new field, in all the
    case variants that show up in code (UPPER / Capitalized / lower). Word-bounded and
    case-sensitive so e.g. `Name` and `name` are handled independently and not double-hit."""
    def cap(s: str) -> str:
        return s[:1].upper() + s[1:]
    for a, b in ((shadow.upper(), field.upper()), (cap(shadow), cap(field)), (shadow, field)):
        text = re.sub(rf"\b{re.escape(a)}\b", b, text)
    return text


def apply_det(sb: tools.Sandbox, node: dict[str, Any]) -> bool:
    """Deterministic 0-LLM fill for a LOW-ENTROPY slot — pure text op, no model call.
    Two strategies (the indent is copied from the anchor so the line drops in verbatim):
      * insert_after_anchor : insert a fixed computed line (the {render_*} template)
      * clone_shadow_line   : clone the SHADOW field's own anchor line with the field
                              substituted — adapts the INSERTED content to the repo's
                              render idiom (entity.field / accessor:"f" / state.f / …)
                              instead of assuming a React <p>{entity.field}</p> shape.
    Returns True if applied (or already present), False to fall back to the LLM."""
    det = node.get("det")
    path = node.get("path")
    anchors = node.get("anchors") or []
    op = (det or {}).get("op")
    if not det or not path or not anchors or op not in ("insert_after_anchor", "clone_shadow_line"):
        return False
    full = sb.root / path
    try:
        lines = full.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return False
    idx = anchors[0] - 1  # anchors are 1-based
    if idx < 0 or idx >= len(lines):
        return False
    anchor = lines[idx]
    indent = anchor[:len(anchor) - len(anchor.lstrip())]
    if op == "clone_shadow_line":
        shadow, field = det.get("shadow"), det.get("field")
        if not shadow or not field:
            return False
        body = _clone_token(anchor.strip(), shadow, field)
        if body == anchor.strip():  # shadow token wasn't on the anchor line → can't clone safely
            return False
        new_line = indent + body
    else:
        line_tpl = (det.get("line") or "").strip()
        if not line_tpl:
            return False
        new_line = indent + line_tpl
    if new_line in lines:  # idempotent
        return True
    lines.insert(idx + 1, new_line)
    full.write_text("\n".join(lines), encoding="utf-8")
    return True


def render_new_files(sb: tools.Sandbox, taskir: dict[str, Any]) -> list[str]:
    """Write the new_file slots (migration + probe tests) correct-by-construction.

    These are deterministic so the suite is green ONLY when the worker has wired
    the sentinel value through the edited slots — the tests cannot be gamed."""
    written: list[str] = []
    field, type_, sentinel = taskir["field"], taskir["type"], taskir["sentinel"]
    index_up = taskir.get("index_up", "")
    table = taskir.get("table", "Articles")
    for n in taskir["slots"]:
        if n["kind"] != "new_file":
            continue
        body = _TEMPLATES[n["template"]].format(field=field, type=type_,
                                                sentinel=sentinel, index_up=index_up,
                                                table=table)
        sb.write_file(n["path"], body)
        written.append(n["path"])
    return written


# ----------------------------------------------------------------------------- evidence gate

def cross_stack_evidence(sb: tools.Sandbox, taskir: dict[str, Any]) -> dict[str, Any]:
    """Per-slot evidence check: each slot's field-evidence regex must appear
    ``min_count`` times in its file. Returns the structured failure repair uses."""
    missing: list[dict[str, Any]] = []
    for n in taskir["slots"]:
        path = n.get("path")
        if not path or not (sb.root / path).is_file():
            missing.append({"slot": n["slot"], "path": path,
                            "expected_evidence": n["evidence"], "reason": "file missing"})
            continue
        for ev in n["evidence"]:
            hits = _grep_lines(sb, path, ev)
            if len(hits) < n["min_count"]:
                missing.append({"slot": n["slot"], "path": path,
                                "expected_evidence": n["evidence"],
                                "found": len(hits), "need": n["min_count"]})
                break
    return {"passed": not missing, "missing_slots": missing,
            "field": taskir["field"]}


def edit_slots(taskir: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in taskir["slots"] if n["kind"] == "edit"]


def repair_scope(missing_slots: list[dict[str, Any]]) -> list[str]:
    return sorted({m["path"] for m in missing_slots if m.get("path")})
