"""Repo profiler — auto-derive a field-flow Skill for an arbitrary repo (Option A).

Goal: make the cheap, probe-verified field-flow work on ANY React + Express + Sequelize
repo, not just the reference one. The differentiator was previously locked to one
hand-written, path-hardcoded Skill; this discovers the cross-stack field path of the
*target* repo and synthesizes that Skill automatically, cached under
``$STACKWEFT_HOME/skills/<repo_id>-add-field.md``.

Design per the brief:
* **Code-engineering first** — regex + globs + structural heuristics do the extraction;
  the LLM is a *fallback* only, and runs on the LOW (execution) tier so a scan is cheap.
  Every assisted call is logged with stage="profile" so per-level stats stay correct.
* **Solid regularities** — entity/field/shadow/anchors come from deterministic patterns
  (``Entity.init({...})`` / ``sequelize.define`` / ``entity.field`` render sites), not a
  hand-maintained noun list.
* The synthesized Skill reuses the *proven* slot instructions (already written in terms
  of ``{shadow}``/``{field}``) and only swaps in the discovered globs + entity + shadow.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Any

from stackweft.engine import layout

# ---- deterministic patterns ------------------------------------------------------------
_INIT_RE = re.compile(r"\b([A-Z]\w+)\.init\s*\(", re.M)
_DEFINE_RE = re.compile(r"sequelize\.define\s*\(\s*[\"']([A-Za-z]\w+)[\"']", re.M)
# shorthand:  field: DataTypes.STRING        object form:  field: { type: DataTypes.STRING, ... }
_ATTR_RE = re.compile(r"^\s*([a-zA-Z_]\w*)\s*:\s*DataTypes\.([A-Z]+)", re.M)
_ATTR_OBJ_RE = re.compile(r"([a-zA-Z_]\w*)\s*:\s*\{[^{}]*?type\s*:\s*DataTypes\.([A-Z]+)", re.S)
# good shadow fields: simple optional scalar text, threaded everywhere. Preference order.
_SHADOW_PREF = ["description", "body", "summary", "bio", "about", "content", "title", "text"]
_TEXTY = {"STRING", "TEXT", "CHAR", "CITEXT"}
_SKIP = {"node_modules", ".git", "dist", "build", ".cache", "coverage", ".next"}


@dataclass
class RepoProfile:
    repo_id: str
    framework: str
    entity: str
    entity_aliases: list[str]
    shadow_field: str
    default_type: str
    table: str                     # real DB table (tableName option, else Sequelize plural)
    globs: dict[str, str]          # slot -> glob/path (relative to repo root)
    detail_destructure: str        # the `const { ... } = <var>` line on the detail page
    detail_var: str                # e.g. "article"
    detail_anchor: str             # regex to locate the destructure line (anchor_fallback)
    list_anchor: str               # {shadow}-templated regex: how shadow renders in the list
    service_required: bool = True  # some repos have no separate write-service layer
    backend_create_update_evidence: list[Any] = _dc_field(default_factory=list)
    notes: list[str] = _dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in
                ("repo_id", "framework", "entity", "entity_aliases", "shadow_field",
                 "default_type", "table", "globs", "detail_destructure", "detail_var",
                 "detail_anchor", "list_anchor", "service_required",
                 "backend_create_update_evidence", "notes")}


def _rel(root: Path, p: Path) -> str:
    return str(p.relative_to(root)).replace("\\", "/")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _glob(root: Path, *patterns: str) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        for p in root.rglob(pat):
            if any(s in p.parts for s in _SKIP) or not p.is_file():
                continue
            out.append(p)
    return sorted(set(out))


# ---- model / entity / shadow discovery -------------------------------------------------

def _scan_models(root: Path) -> dict[str, dict[str, str]]:
    """entity -> {field: SQLTYPE} from Sequelize model files (deterministic)."""
    entities: dict[str, dict[str, str]] = {}
    for f in _glob(root, "*.js", "*.ts"):
        if "model" not in _rel(root, f).lower():
            continue
        txt = _read(f)
        m = _INIT_RE.search(txt) or _DEFINE_RE.search(txt)
        if not m:
            continue
        name = m.group(1)
        attrs: dict[str, str] = {}
        for fld, typ in _ATTR_RE.findall(txt):
            attrs[fld] = typ
        for fld, typ in _ATTR_OBJ_RE.findall(txt):  # object-form fields (mk48 style)
            attrs.setdefault(fld, typ)
        if attrs:
            entities.setdefault(name, {}).update(attrs)
    return entities


def _pick_entity(entities: dict[str, dict[str, str]], root: Path) -> str:
    """Main content entity: prefer the one with a preferred shadow field + most texty
    scalar fields. Deterministic, no LLM."""
    def score(name: str, attrs: dict[str, str]) -> tuple[int, int, int]:
        texty = sum(1 for t in attrs.values() if t in _TEXTY)
        has_pref = 1 if any(s in attrs for s in _SHADOW_PREF) else 0
        # a frontend route/component named after the entity is a strong signal
        routed = 1 if _glob(root, f"**/{name}/*.jsx", f"**/{name}.jsx") else 0
        return (has_pref, routed, texty)
    return max(entities, key=lambda n: score(n, entities[n]))


def _pick_shadow(attrs: dict[str, str], root: Path, entity: str) -> str:
    """A simple optional STRING/TEXT field already threaded across the stack."""
    texty = [f for f, t in attrs.items() if t in _TEXTY]
    el = entity.lower()
    # must appear as <var>.field somewhere in the frontend (so its render path exists)
    def threaded(f: str) -> bool:
        return bool(_grep_count(root, rf"\b{el}\.{f}\b", "*.jsx", "*.tsx", "*.js"))
    for pref in _SHADOW_PREF:
        if pref in texty and threaded(pref):
            return pref
    for f in texty:
        if threaded(f):
            return f
    return texty[0] if texty else (next(iter(attrs), "description"))


def _grep_count(root: Path, pattern: str, *globs: str) -> int:
    rx = re.compile(pattern)
    n = 0
    for f in _glob(root, *globs):
        n += len(rx.findall(_read(f)))
    return n


_WEB_EXTS = (".jsx", ".js", ".tsx", ".ts")


def _pats(roles: tuple[str, ...], exts: tuple[str, ...]) -> list[str]:
    return [f"*{role}*{ext}" for role in roles for ext in exts]


def _find_layer(root: Path, roots: list[str] | None, name_globs: list[str],
                must_any: list[str] | None = None, path_hint: str | None = None,
                prefer: list[str] | None = None) -> str | None:
    """Find the best file UNDER the given layer roots whose name matches name_globs and
    (if must_any) whose content matches ≥1 pattern, ranked by entity path-hint + prefer
    cues. This locates by entity+role, NOT by an assumed ``entity.field`` render — so it
    adapts to entity-dir layouts (components/product/Form.js) and config/reducer UIs."""
    bases = [(root / r if r not in (".", "") else root) for r in (roots or ["."])]
    cand: list[tuple[int, int, str]] = []
    for base in bases:
        if not base.is_dir():
            continue
        for pat in name_globs:
            for f in base.rglob(pat):
                if any(s in f.parts for s in _SKIP) or not f.is_file():
                    continue
                txt = _read(f)
                if must_any and not any(re.search(m, txt) for m in must_any):
                    continue
                rel = _rel(root, f)
                score = (3 if path_hint and path_hint in rel.lower() else 0)
                score += sum(1 for p in (prefer or []) if re.search(p, txt))
                cand.append((score, -len(rel), rel))
    if not cand:
        return None
    cand.sort(reverse=True)
    return cand[0][2]


def _find_file(root: Path, globs: tuple[str, ...], must: list[str],
               prefer: list[str] | None = None) -> str | None:
    """First file matching globs that contains ALL `must` substrings/regex; ranked by
    how many `prefer` cues it also has."""
    cand: list[tuple[int, str]] = []
    for f in _glob(root, *globs):
        txt = _read(f)
        if all(re.search(m, txt) for m in must):
            pscore = sum(1 for p in (prefer or []) if re.search(p, txt))
            cand.append((pscore, _rel(root, f)))
    if not cand:
        return None
    cand.sort(key=lambda t: (-t[0], len(t[1])))
    return cand[0][1]


# ---- the profile -----------------------------------------------------------------------

def profile_repo(sb, *, run_id: str | None = None, level: float = 0.6) -> RepoProfile:
    """Deterministically discover the cross-stack field path. LLM (low tier) is used only
    if the detail-page render site can't be found by grep."""
    root = sb.root
    entities = _scan_models(root)
    if not entities:
        raise ProfileError("no Sequelize models found (looked for `Entity.init` / "
                            "`sequelize.define` under */models/*) — not a supported shape yet")
    entity = _pick_entity(entities, root)
    attrs = entities[entity]
    shadow = _pick_shadow(attrs, root, entity)
    el, plural = entity.lower(), entity.lower() + "s"

    # layer-scoped discovery: locate files by ENTITY + ROLE within the right layer roots
    # (api vs web), then discover the render anchor adaptively. Does NOT assume an
    # `entity.field` render or fixed dir names.
    lay = layout.detect(root)
    api_roots = [r for L in lay.by_kind("api") for r in L.roots] or None
    web_roots = [r for L in lay.by_kind("web") for r in L.roots] or None

    g: dict[str, str] = {}
    g["backend_model"] = (_find_layer(root, api_roots, ["*.js", "*.ts"],
        must_any=[rf"\b{entity}\.init\b", rf"define\(\s*[\"']{el}[\"']"], path_hint=el)
        or f"backend/models/{entity}.js")
    g["backend_create_update"] = (_find_layer(root, api_roots,
        _pats(("controller", "Controller"), (".js", ".ts")) + ["*.js", "*.ts"],
        must_any=[r"\.create\(", r"\.update\(", r"req\.body"], path_hint=el,
        prefer=[r"\.update\(", r"findByPk|findOne", r"req\.body"])
        or f"backend/controllers/{plural}.js")
    g["frontend_write_service"] = (_find_layer(root, web_roots,
        _pats(("set", "Service", "service", "api", "Api"), _WEB_EXTS),
        must_any=[rf"\b{el}\b", rf"\b{re.escape(shadow)}\b"],  # must reference THIS entity/field,
        path_hint=el, prefer=[r"axios|fetch\(|\.post\(|\.put\(|PUT|POST"])  # not a generic wrapper
        or f"**/services/set{entity}.js")
    g["frontend_editor_form"] = (_find_layer(root, web_roots,
        _pats(("Form", "Editor", "Edit", "New", "Create", "form", "editor"), _WEB_EXTS),
        path_hint=el, prefer=[r"FormFieldset|<input|<textarea|useState|useReducer|onChange"])
        or f"**/{entity}Editor*/*.jsx")
    g["frontend_list_render"] = (_find_layer(root, web_roots,
        _pats(("List", "Preview", "Table", "Grid", "Index", "Card", "list"), _WEB_EXTS),
        path_hint=el, prefer=[rf"{el}\.|accessor|\.map\(|columns|<p>|preview|card"])
        or f"**/{entity}sPreview/*.jsx")
    detail = _find_detail(root, el, shadow, entity, web_roots, sb, run_id, level)
    g["frontend_detail_render"] = detail["glob"]

    aliases = _aliases(entity)
    list_anchor = _discover_render_anchor(root, g["frontend_list_render"], el, shadow)
    service_required = _resolve(root, g["frontend_write_service"]) is not None
    backend_evidence = _backend_create_update_evidence(root, g["backend_create_update"], shadow)
    model_txt = _read(_resolve(root, g["backend_model"])) or ""
    tm = re.search(r"tableName\s*:\s*[\"'](\w+)[\"']", model_txt)
    table = tm.group(1) if tm else (entity + "s")  # else Sequelize default plural (Article→Articles)
    return RepoProfile(
        repo_id=_repo_id(sb), framework="react+express+sequelize",
        entity=entity, entity_aliases=aliases, shadow_field=shadow,
        default_type="STRING", table=table, globs=g,
        detail_destructure=detail["destructure"], detail_var=detail["var"],
        detail_anchor=detail["anchor"], list_anchor=list_anchor,
        service_required=service_required, backend_create_update_evidence=backend_evidence,
        notes=[f"entities={list(entities)}", f"shadow picked from {sorted(attrs)}",
               f"list_anchor={list_anchor}", f"service_required={service_required}"])


def _find_detail(root: Path, el: str, shadow: str, entity: str, web_roots: list[str] | None,
                 sb, run_id: str | None, level: float) -> dict[str, str]:
    """The detail/page that shows ONE entity. Locate by role within the web layer; derive
    the destructure-line anchor if that idiom is used, else an adaptive shadow anchor.
    One LOW-tier LLM call only if nothing is found."""
    # rank candidate pages: the detail page renders ONE entity (single-entity destructure /
    # under routes|pages / has a title), and is NOT the list (which .maps and is named
    # Preview/List/Table/Grid/Card).
    bases = [(root / r if r not in (".", "") else root) for r in (web_roots or ["."])]
    pats = _pats(("View", "Detail", "Show", "Page", entity, el), _WEB_EXTS)
    cands: list[tuple[int, str]] = []
    for base in bases:
        if not base.is_dir():
            continue
        for pat in pats:
            for fp in base.rglob(pat):
                if any(s in fp.parts for s in _SKIP) or not fp.is_file():
                    continue
                rel, t = _rel(root, fp), _read(fp)
                score = (3 if el in rel.lower() else 0)
                score += 4 if re.search(r"=\s*\w+\s*\|\|\s*\{\}", t) else 0
                score += 2 if ("/routes/" in rel or "/pages/" in rel) else 0
                score += 2 if re.search(r"<h1|useParams|findOne|getOne", t) else 0
                score -= 4 if re.search(r"\.map\(", t) else 0
                score -= 4 if re.search(r"Preview|List|Table|Grid|Card", rel) else 0
                cands.append((score, rel))
    cands.sort(reverse=True)
    glob = cands[0][1] if cands and cands[0][0] > 0 else None
    f = _resolve(root, glob) if glob else None
    txt = _read(f) if f else ""
    m = re.search(r"const\s*\{[^}]*\}\s*=\s*(\w+)\s*\|\|\s*\{\}", txt)
    if m:  # destructure idiom (e.g. const { title, body, ... } = article || {})
        first = re.search(r"const\s*\{\s*(\w+)", m.group(0))
        anchor = (r"const \{\s*" + first.group(1)) if first else (r"=\s*" + re.escape(m.group(1)))
        return {"glob": glob, "destructure": m.group(0), "var": m.group(1), "anchor": anchor}
    if glob:  # found the page but a different render idiom → adaptive shadow anchor
        return {"glob": glob, "destructure": "", "var": el,
                "anchor": _discover_render_anchor(root, glob, el, shadow)}
    # nothing found → LOW-tier LLM fallback
    glob = _llm_pick_detail(sb, el, entity, run_id, level) or f"**/routes/{entity}/{entity}.jsx"
    return {"glob": glob, "destructure": "", "var": el, "anchor": r"\b" + "{shadow}" + r"\b"}


def _llm_pick_detail(sb, el: str, entity: str, run_id: str | None, level: float) -> str | None:
    try:
        from stackweft.core import llm
    except Exception:  # noqa: BLE001
        return None
    listing = "\n".join(sorted(_rel(sb.root, p) for p in _glob(sb.root, "**/*.jsx"))[:120])
    sys = ("You map a React repo. Reply with ONLY the single relative path of the file "
           f"that renders the DETAIL/single page of a {entity} (shows one item with its "
           "title/body). No prose, just the path.")
    try:
        r = llm.messages(system=sys, msgs=[{"role": "user", "content": listing}],
                         level=level, run_id=run_id, stage="profile",
                         purpose="profile_detail_pick", max_tokens=120)
        path = (r.text or "").strip().splitlines()[0].strip().strip("`")
        return path if path.endswith(".jsx") else None
    except Exception:  # noqa: BLE001
        return None


def _resolve(root: Path, rel: str | None) -> Path | None:
    if not rel:
        return None
    if "*" in rel:
        hits = _glob(root, rel)
        return hits[0] if hits else None
    p = root / rel
    return p if p.is_file() else None


def _discover_render_anchor(root: Path, rel: str, el: str, shadow: str) -> str:
    """Return a ``{shadow}``-parameterized regex matching HOW the shadow field is
    referenced in this file — adaptive to the repo's render idiom so shadow-cloning
    isn't pinned to ``entity.field`` JSX:
      entity.field        →  article\\.{shadow}
      accessor: "field"   →  accessor:\\s*["']{shadow}   (config/grid-driven UIs, e.g. mk48)
      state.field         →  \\w+\\.{shadow}
      bare token          →  \\b{shadow}\\b
    """
    f = _resolve(root, rel)
    txt = _read(f) if f else ""
    if re.search(rf"\b{re.escape(el)}\.{re.escape(shadow)}\b", txt):
        return re.escape(el) + r"\." + "{shadow}"
    if re.search(rf'accessor\s*:\s*["\']{re.escape(shadow)}', txt):
        return r'accessor:\s*["\']' + "{shadow}"
    if re.search(rf"\b\w+\.{re.escape(shadow)}\b", txt):
        return r"\w+\." + "{shadow}"
    return r"\b" + "{shadow}" + r"\b"


def _count_lines(txt: str, pattern: str) -> int:
    rx = re.compile(pattern)
    return sum(1 for line in txt.splitlines() if rx.search(line))


def _backend_create_update_evidence(root: Path, rel: str, shadow: str) -> list[Any]:
    """Derive evidence from how the existing shadow field is wired in the target file.

    The profiler owns repo-shape inference. The engine later only checks the declared
    regex/count contract, so the main field-flow core stays repo-agnostic.
    """
    f = _resolve(root, rel)
    txt = _read(f) if f else ""
    shadow_rx = re.escape(shadow)
    destructures = _count_lines(
        txt, rf"\{{[^}}]*\b{shadow_rx}\b[^}}]*\}}\s*=\s*[^;\n]*(?:req|request)\.body")
    object_pairs = _count_lines(txt, rf"\b{shadow_rx}\s*:\s*{shadow_rx}\b")
    assignments = _count_lines(txt, rf"\.\s*{shadow_rx}\s*=")

    min_total = max(2, min(8, destructures + object_pairs + assignments))
    evidence: list[Any] = [{"pattern": "{field}", "min_count": min_total}]
    if destructures:
        evidence.append({
            "label": "request destructure",
            "pattern": r"\{[^}]*\b{field}\b[^}]*\}\s*=\s*[^;\n]*(?:req|request)\.body",
            "min_count": destructures,
        })
    if object_pairs:
        evidence.append({
            "label": "create/write object",
            "pattern": r"\b{field}\s*:\s*{field}\b",
            "min_count": object_pairs,
        })
    if assignments:
        evidence.append({
            "label": "update assignment",
            "pattern": r"\.\s*{field}\s*=",
            "min_count": assignments,
        })
    return evidence


def _aliases(entity: str) -> list[str]:
    el = entity.lower()
    cn = {"article": "文章", "post": "博文", "comment": "评论", "user": "用户",
          "product": "商品", "order": "订单", "tag": "标签", "book": "书"}
    out = [entity, el]
    if el in cn:
        out.append(cn[el])
    return list(dict.fromkeys(out))


def _repo_id(sb) -> str:
    try:
        from stackweft.platform import projects
        raw = projects.repo_identity(str(sb.root))
    except Exception:  # noqa: BLE001
        raw = sb.root.name
    return re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-")[:16] or "repo"


class ProfileError(RuntimeError):
    pass


# ---- skill synthesis -------------------------------------------------------------------
# Reuse the PROVEN slot instructions (parameterized by {shadow}/{field}); only the globs,
# entity, shadow and detail anchor are repo-specific (filled from the discovered profile).

_SKILL_TMPL = """---
name: {skill_name}
intent: add_entity_field
description: >
  Auto-profiled by StackWeft for this repo: add a new user-editable field to the
  {entity} entity and thread it across the full stack (model + migration + create/update
  controller + API payload + frontend service + editor form + list card + detail page),
  verified by counterexample sentinel probes. Generated from the repo's own structure.
match: [{match}]
priority: 6
repo_id: {repo_id}
shadow_field: {shadow}
entity: {entity}
entity_aliases: {aliases}
default_type: STRING
---

# Skill: Add a field to {entity} (auto-profiled Shadow Field Cloning)

A new field is delivered by cloning the cross-stack path of the existing `{shadow}`
field. All locations below were discovered by profiling THIS repo.

```json
{slot_json}
```
"""


def _slot_spec(p: RepoProfile) -> dict[str, Any]:
    e, el = p.entity, p.entity.lower()
    plural = el + "s"
    g = p.globs
    # render-insert strategy per discovered idiom: the reference `entity.field` idiom uses
    # the rich {render_*} template (carries the className the probe checks); any other
    # idiom (accessor:/state./bare) uses clone_shadow_line — clone the shadow's own line.
    entity_field_idiom = (p.list_anchor == re.escape(el) + r"\." + "{shadow}")
    list_det = ({"op": "insert_after_anchor", "line": "{render_list}"} if entity_field_idiom
                else {"op": "clone_shadow_line"})
    detail_det = (None if not p.detail_anchor or p.detail_anchor.startswith("const ")
                  else {"op": "clone_shadow_line"})  # adaptive render → deterministic clone
    detail_slot = {"slot": "frontend_detail_render", "layer": "frontend", "kind": "edit",
                   "glob": g["frontend_detail_render"], "shadow_anchor": None,
                   "anchor_fallback": p.detail_anchor,
                   "evidence": ["{field}", "{render_tag}"], "min_count": 1}
    if detail_det:  # adaptive render idiom → deterministic clone of the shadow's line
        detail_det.update({})
        detail_slot["det"] = detail_det
        detail_slot["instruction"] = (f"Add `{{field}}` near where `{{shadow}}` is rendered on "
                                       f"the {e} detail page, mirroring `{{shadow}}`'s idiom.")
    else:  # destructure idiom (const {{ … }} = entity) → LLM adds to destructure + renders
        detail_slot["instruction"] = (f"Add `{{field}}` to the `{p.detail_destructure}` "
                                       f"destructure on the {e} detail page, then render exactly "
                                       f"`{{render_detail}}` near the title.")
    return {
        "intent": "add_entity_field",
        "slots": [
            {"slot": "backend_model", "layer": "backend", "kind": "edit",
             "glob": g["backend_model"], "shadow_anchor": r"{shadow}\s*:",  # shorthand OR object-form attr
             "evidence": ["{field}"], "min_count": 1,
             "instruction": ("Add a Sequelize attribute `{field}: DataTypes.{type},` to the "
                             f"{e}.init attributes object, right after the `{{shadow}}` "
                             "attribute. Mirror how `{shadow}` is declared. Field is OPTIONAL.")},
            {"slot": "backend_create_update", "layer": "backend", "kind": "edit",
             "glob": g["backend_create_update"], "shadow_anchor": "{shadow}",
             "evidence": p.backend_create_update_evidence or ["{field}"], "min_count": 2,
             "instruction": ("Thread `{field}` through the create and update handlers exactly "
                             "like `{shadow}`, but NOT required: add it to the request "
                             "destructure, the `.create({...})` object, and the update branch "
                             "as `if ({field} !== undefined) row.{field} = {field};`. "
                             "read_window around each `{shadow}` occurrence first.")},
            {"slot": "frontend_write_service", "layer": "frontend", "kind": "edit",
             "glob": g["frontend_write_service"], "shadow_anchor": "{shadow}",
             "required": p.service_required,  # some repos have no separate write-service layer
             "evidence": ["{field}"], "min_count": 2,
             "instruction": ("Add `{field}` to the destructured params AND to the payload "
                             "object, mirroring `{shadow}`.")},
            {"slot": "frontend_editor_form", "layer": "frontend", "kind": "edit",
             "glob": g["frontend_editor_form"], "shadow_anchor": "{shadow}",
             "evidence": ["{field}"], "min_count": 2,
             "instruction": ("Mirror `{shadow}` everywhere in this form so `{field}` "
                             "round-trips: default state, the load destructure, the setForm, "
                             "and the submit payload. THEN add an input control for `{field}` "
                             "(NOT required), using the existing `{shadow}` control as the "
                             "template.")},
            {"slot": "frontend_list_render", "layer": "frontend", "kind": "edit",
             "glob": g["frontend_list_render"],
             "shadow_anchor": p.list_anchor,  # adaptive: entity.field / accessor:"field" / state.field / bare
             "evidence": ["{field}", "{render_tag}"], "min_count": 1,
             "det": list_det,  # template (entity.field) OR clone_shadow_line (other idioms)
             "instruction": ("In the list view, near where `{shadow}` is rendered, "
                             "render `{field}` the same way (mirror `{shadow}`'s idiom).")},
            detail_slot,
            {"slot": "backend_migration", "layer": "backend", "kind": "new_file",
             "path_template": f"backend/migrations/20250601000000-add-{{field}}-to-{plural}.js",
             "template": "migration_addcolumn", "evidence": ["{field}"], "min_count": 1},
            {"slot": "probe_backend_attr", "layer": "backend", "kind": "new_file",
             "path_template": f"backend/test/{el}.{{field_lower}}.test.js",
             "template": "test_backend_attr_probe"},
            {"slot": "probe_list_render", "layer": "frontend", "kind": "new_file",
             "path_template": (g["frontend_list_render"].rsplit("/", 1)[0]
                               + "/{field_lower}.list.test.jsx"),
             "template": "test_list_render_probe"},
            {"slot": "probe_write_payload", "layer": "frontend", "kind": "new_file",
             "path_template": "frontend/src/services/{field_lower}.payload.test.js",
             "template": "test_write_payload_probe"},
        ],
        "hard_gates": ["all_edit_slots_have_shadow_anchor", "probes_present"],
        "table": p.table,
    }


def synthesize_skill(p: RepoProfile) -> str:
    spec = _slot_spec(p)
    # dedup + keep the action words (避免之前被 [:8] 截断丢掉 增加/add)
    kws = list(dict.fromkeys([p.entity.lower(), *[a.lower() for a in p.entity_aliases],
                              p.shadow_field.lower(), "字段", "field", "增加", "add"]))
    match = ", ".join(f'"{x}"' for x in kws)
    return _SKILL_TMPL.format(
        skill_name=f"{p.repo_id}-add-{p.entity.lower()}-field", repo_id=p.repo_id,
        entity=p.entity, shadow=p.shadow_field,
        aliases=", ".join(a for a in p.entity_aliases if a != p.entity),
        match=match, slot_json=json.dumps(spec, ensure_ascii=False, indent=2))


def repo_token(sb) -> str:
    """Stable per-repo token used in profiled skill filenames AND their `repo_id`
    frontmatter; pass this to skills.select(repo_id=…) so a profiled skill is eligible
    only for its own repo."""
    return _repo_id(sb)


def repo_skill_path(sb) -> Path | None:
    """The cached auto-profiled skill for this repo, if one exists."""
    from stackweft.engine import skills as skmod
    rid = _repo_id(sb)
    if not skmod.SKILLS_DIR.is_dir():
        return None
    hits = sorted(skmod.SKILLS_DIR.glob(f"{rid}-*.md"))
    return hits[0] if hits else None


def ensure_repo_skill(sb, *, run_id: str | None = None, level: float = 0.6) -> str | None:
    """One-time init for a newly-seen repo: if it has no profiled field-flow skill yet,
    profile it and synthesize one. Returns the skill name if it just created one, else
    None (already cached, or the shape isn't supported — caller falls back gracefully).
    Profiling is deterministic-first; any LLM assist runs on the LOW tier under
    stage='profile' so per-level stats stay correct."""
    if repo_skill_path(sb):
        return None
    try:
        return init_repo(sb, run_id=run_id, level=level, write=True).get("skill_name")
    except ProfileError:
        return None
    except Exception:  # noqa: BLE001
        return None


def init_repo(sb, *, run_id: str | None = None, level: float = 0.6,
              write: bool = True) -> dict[str, Any]:
    """Profile the repo, synthesize a field-flow Skill, cache it under the user skills dir.
    Returns {ok, profile, skill_name, path}."""
    p = profile_repo(sb, run_id=run_id, level=level)
    content = synthesize_skill(p)
    from stackweft.engine import skills as skmod
    name = f"{p.repo_id}-add-{p.entity.lower()}-field"
    out: dict[str, Any] = {"ok": True, "skill_name": name, "profile": p.to_dict()}
    if write:
        skmod.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        path = skmod.SKILLS_DIR / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        out["path"] = str(path)
    else:
        out["content"] = content
    return out
