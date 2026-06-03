"""Fixed, resumable workflow engine.

Pipeline (every stage persisted; pause / resume / redo from any stage):

    clarify → plan → localize → compile → generate → verify → pr

The worker does edits; the LLM does clarify/plan; localize builds a grep-backed
candidate list + cross-stack field map; compile turns the requirement into a
validated change graph; verify runs a differential lint gate + a real test gate +
an evidence-based cross-stack gate, with a bounded repair loop.

Design notes:
* Differential lint gate: the target repo may ship pre-existing lint errors, so
  the gate flags regressions vs a baseline captured before edits.
* The test gate uses a filesystem snapshot taken at run start (not git) to decide
  whether a test file was added, since the worker may commit mid-run.
* Three token ceilings: the worker in-loop budget, the verify->repair loop, and
  the whole run.
* Lean localize: a deterministic grep digest + one pick call.
* The cross-stack gate is evidence-based: it hard-fails only on grep proof, never
  on the LLM's layer guess (which over-classifies).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from stackweft.core import config, llm, obs
from stackweft.engine import fieldflow, layout, recipes, skills, tools, worker

try:
    from stackweft.platform import rag as _rag
except Exception:  # pragma: no cover
    _rag = None  # type: ignore[assignment]

# A read-only `compile` stage sits before generate. It turns the
# requirement into a validated Field Flow Graph (Shadow Field Cloning) so the
# worker FILLS a verified graph instead of inventing file paths. A bad/incomplete
# graph fails fast here and never reaches generate.
STAGES = ["clarify", "plan", "localize", "compile", "generate", "verify", "pr"]


@dataclass
class RunContext:
    run_id: str
    sandbox: tools.Sandbox
    requirement: str
    skill: skills.Skill
    branch: str
    repo_id: str = ""  # stable repo identity (git root-commit hash); keys WeftRecipe
    outputs: dict[str, Any] = field(default_factory=dict)
    interactive: bool = False
    ask: Callable[[str], str] | None = None


def _rag_block(ctx: RunContext) -> str:
    if _rag is None:
        return ""
    try:
        hits = _rag.retrieve(ctx.requirement, k=3, run_id=ctx.run_id,
                             repo_id=(ctx.repo_id or None))
        return _rag.render_for_prompt(hits)
    except Exception:
        return ""


def _repo_overview(sb: tools.Sandbox, max_files: int = 80) -> str:
    lines: list[str] = []
    skip = {".git", "node_modules", ".venv", "dist", "build", ".cache"}
    count = 0
    for dirpath, dirnames, filenames in os.walk(sb.root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        rel = os.path.relpath(dirpath, sb.root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > 3:
            dirnames[:] = []
            continue
        for f in sorted(filenames):
            if count >= max_files:
                return "\n".join(lines + ["…(truncated)"])
            lines.append(os.path.join(rel, f) if rel != "." else f)
            count += 1
    return "\n".join(lines)


# ── filesystem snapshot (git-independent change detection) ───────────────────

def _fs_snapshot(sb: tools.Sandbox) -> dict[str, str]:
    skip = {".git", "node_modules", ".venv", "dist", "build", ".cache"}
    exts = {".js", ".jsx", ".ts", ".tsx", ".json", ".css", ".md"}
    snap: dict[str, str] = {}
    for dp, dns, fns in os.walk(sb.root):
        dns[:] = [d for d in dns if d not in skip]
        for f in fns:
            if os.path.splitext(f)[1] not in exts:
                continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, sb.root)
            try:
                with open(full, "rb") as fh:
                    snap[rel] = hashlib.sha1(fh.read()).hexdigest()
            except OSError:
                pass
    return snap


def _changed_files(ctx: RunContext) -> set[str]:
    baseline = ctx.outputs.get("_fs_baseline")
    if baseline is None:
        row = obs.load_stage(ctx.run_id, "_fs_baseline")
        baseline = (json.loads(row["output_json"]).get("snap", {})
                    if row and row["output_json"] else {})
        ctx.outputs["_fs_baseline"] = baseline
    now = _fs_snapshot(ctx.sandbox)
    return {p for p, h in now.items() if baseline.get(p) != h}


# ── clarify / plan ───────────────────────────────────────────────────────────

def stage_clarify(ctx: RunContext) -> dict[str, Any]:
    guide = ctx.skill.guidance("clarify") or skills.FALLBACK.guidance("clarify")
    rag_block = _rag_block(ctx)
    _lang = config.lang()
    system = ("You are the requirement-clarification analyst for a full-stack "
              "delivery system. Convert a raw PM requirement into a precise, minimal, "
              f"structured spec. ALWAYS write every user-facing field (summary, "
              f"behaviour, acceptance, assumptions, open_questions, reply) in {_lang}. "
              "If the input is NOT an actionable change request, set is_requirement=false "
              "and classify `kind`: \"assist\" for a question / repo exploration / analysis "
              "the assistant can answer by reading the code (e.g. '看下目录有什么', '解释下登录怎么做的'), "
              "or \"greeting\" for pure chit-chat. For greeting, put a short warm "
              f"{_lang} `reply` (like a real account manager, no emoji) inviting a concrete "
              "change. For a delivery requirement, kind=\"delivery\". "
              "ALSO judge feasibility honestly and set `feasibility`: use \"infeasible\" "
              "if the request CANNOT be delivered here — out of scope (not a full-stack "
              "code change), self-contradictory, or impossible in this repo — and put a "
              "concrete `feasibility_reason` plus a kind `reply` that explains why and "
              "suggests a workable alternative. Use \"needs_confirm\" if it IS doable but "
              "the cost / blast-radius is large (broad refactor, data-risking schema "
              "migration, many files or entities touched, hard to reverse); put the "
              "cost/risk in `feasibility_reason` and a `reply` that states it and asks the "
              "user to confirm before spending. Otherwise \"ok\". Be conservative — only "
              "flag genuinely costly or impossible asks, never ordinary field/feature "
              "additions. "
              "FINALLY, if this is a field-add, derive `field_name`: a concise camelCase "
              "English identifier for the NEW field, EVEN WHEN the user only described it in "
              "natural language and never gave a name — e.g. '一句话标语'→'tagline', "
              "'封面图'→'coverImage', '阅读量/浏览量'→'viewCount', '是否置顶'→'pinned', "
              "'最后编辑时间'→'updatedAt'. Use the user's name verbatim if they gave one. "
              "Empty string if it isn't a single-field add. "
              + guide + ("\n\n" + rag_block if rag_block else ""))
    hint = ('Return STRICT JSON: {"is_requirement": bool, "kind": "delivery"|"assist"|"greeting", '
            '"reply": str, "summary": str, "layers": [..of "frontend"|"backend"|"db"], '
            '"behaviour": str, "acceptance": str, "assumptions": [str], '
            '"open_questions": [str], "feasibility": "ok"|"needs_confirm"|"infeasible", '
            '"feasibility_reason": str, "field_name": str}')
    user = f"User input:\n{ctx.requirement}\n\n{hint}\nJSON only."
    res = llm.messages(system=system, msgs=[{"role": "user", "content": user}],
                       level=0.8, run_id=ctx.run_id, stage="clarify",
                       purpose="clarify", max_tokens=2500)
    spec = _extract_json(res.text)
    if ctx.interactive and ctx.ask and spec.get("open_questions"):
        spec["answers"] = {q: ctx.ask(q) for q in spec["open_questions"]}
    return {"spec": spec, "raw": res.text}


def assist(ctx: RunContext) -> str:
    """General read-only assistant for NON-delivery messages (browse the repo, answer
    questions, light analysis). It never edits/runs — a real change goes through the
    delivery pipeline. Bounded; returns helpful text (empty on failure → caller falls
    back to the friendly reply). Keeps the core claim untouched."""
    repo_name = ctx.sandbox.root.name
    sys = ("You are StackWeft's general assistant (StackWeft is the delivery TOOL — NOT the "
           "name of the repository you are looking at). You can explore the user's target "
           f"workspace READ-ONLY (list_dir, read_file, read_window, grep). The workspace is "
           f"the repo at the sandbox root, whose folder name is `{repo_name}` — refer to it "
           "by that name (or 'the repo'), and NEVER call it 'StackWeft'. Describe ONLY what "
           "you actually read from disk; do not invent files or structure. Answer the user's "
           "question or do light analysis. You do NOT edit code or run commands — if the user "
           "wants a change, say so in one line and tell them to phrase it as a concrete "
           f"requirement (the delivery pipeline handles that). Answer in {config.lang()}, "
           "concise and genuinely helpful.")
    prev = os.environ.get("STACKWEFT_READONLY")
    os.environ["STACKWEFT_READONLY"] = "1"
    try:
        res = worker.run_task(sandbox=ctx.sandbox, system=sys, task=ctx.requirement,
                              run_id=ctx.run_id, stage="assist", level=0.6,
                              max_rounds=6, max_tokens=1600, token_budget=25_000,
                              schemas=tools.READONLY_SCHEMAS)
        text = (res.final_text or "").strip()
        if text.upper().startswith("DONE"):  # worker completion marker — not user-facing
            text = text[4:].lstrip(" \n:：-").strip()
        return text
    except Exception:  # noqa: BLE001
        return ""
    finally:
        if prev is None:
            os.environ.pop("STACKWEFT_READONLY", None)
        else:
            os.environ["STACKWEFT_READONLY"] = prev


def stage_plan(ctx: RunContext) -> dict[str, Any]:
    guide = ctx.skill.guidance("plan") or skills.FALLBACK.guidance("plan")
    spec = ctx.outputs["clarify"]["spec"]
    system = ("You are the implementation planner. Produce a minimal, cross-stack-"
              "consistent plan. " + guide)
    user = (f"Spec:\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n\n"
            f"Repo file tree:\n{_repo_overview(ctx.sandbox)}\n\n"
            'Return STRICT JSON: {"approach": str, "files_hint": [str], '
            '"test_plan": str, "steps": [str]}. JSON only.')
    res = llm.messages(system=system, msgs=[{"role": "user", "content": user}],
                       level=0.8, run_id=ctx.run_id, stage="plan",
                       purpose="plan", max_tokens=2500)
    return {"plan": _extract_json(res.text), "raw": res.text}


# ── localize (lean: grep digest + 1 pick call) ───────────────────────────────

def _grep_digest(sb: tools.Sandbox, terms: list[str], max_files: int = 25) -> str:
    """Deterministic grep over backend+frontend → compact candidate file list
    (path + hit count). Replaces a multi-round agentic read loop. Pure shell, 0 LLM."""
    lay = layout.for_sandbox(sb)
    inc, scope = lay.include_args(), lay.grep_scope()
    seen: dict[str, int] = {}
    for t in terms:
        if len(t) < 3:
            continue
        out = sb.run_shell(
            f"grep -rc {inc} -- {t!r} "
            f"{scope} 2>/dev/null | grep -v ':0$' | grep -v node_modules | head -40")
        for line in out.split("\n")[1:]:
            if ":" not in line or "exit=" in line:
                continue
            path, _, cnt = line.rpartition(":")
            if path.strip():
                seen[path.strip()] = seen.get(path.strip(), 0) + (
                    int(cnt) if cnt.strip().isdigit() else 1)
    ranked = sorted(seen.items(), key=lambda kv: -kv[1])[:max_files]
    return "\n".join(f"{p}  ({n} hits)" for p, n in ranked) or "(no grep hits)"


def _candidate_terms(ctx: RunContext) -> list[str]:
    spec = ctx.outputs.get("clarify", {}).get("spec", {})
    plan = ctx.outputs.get("plan", {}).get("plan", {})
    text = " ".join([str(spec.get("summary", "")), str(spec.get("behaviour", "")),
                     str(ctx.requirement), " ".join(plan.get("files_hint", []) or [])])
    return sorted({w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
                   if any(c.isupper() for c in w[1:]) or w.lower() in {
                       "body", "title", "slug", "author", "tags", "image", "article",
                       "comment", "user", "favorited", "model", "controller"}})[:10]


def stage_localize(ctx: RunContext) -> dict[str, Any]:
    spec = ctx.outputs["clarify"]["spec"]
    plan = ctx.outputs["plan"]["plan"]
    digest = _grep_digest(ctx.sandbox, _candidate_terms(ctx))
    system = ("You are a code-localization analyst. Given a requirement and a list of "
              "candidate files (from grep), pick the MINIMAL set of files that must "
              "change. Return STRICT JSON only: "
              '{"targets": [{"path": str, "why": str}], "notes": str}.')
    user = (f"Requirement spec:\n{json.dumps(spec, ensure_ascii=False)}\n\n"
            f"Plan hint files: {plan.get('files_hint')}\n\n"
            f"Candidate files (grep hits across backend/ and frontend/):\n{digest}\n\n"
            "Pick the real files to change (and where the test should go). JSON only.")
    res = llm.messages(system=system, msgs=[{"role": "user", "content": user}],
                       level=0.6, run_id=ctx.run_id, stage="localize",
                       purpose="localize_pick", max_tokens=1200)
    targets = _extract_json(res.text)
    if "_unparsed" in targets:
        targets = {"targets": [{"path": p, "why": "plan hint"}
                               for p in (plan.get("files_hint") or [])]}
    field_map = _build_field_map(ctx, spec)
    return {"targets_raw": res.text, "targets": targets, "field_map": field_map,
            "grep_digest": digest}


def _build_field_map(ctx: RunContext, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Grep-backed cross-stack map: for each candidate identifier, where it is
    used in backend vs frontend. Machine-checkable evidence the verify stage
    enforces."""
    sb = ctx.sandbox
    lay = layout.for_sandbox(sb)
    inc = lay.include_args()
    field_map = []
    for w in _candidate_terms(ctx)[:8]:
        # "backend"/"frontend" stay as the stable field_map keys (many consumers), but
        # we scan each layer by KIND (api≈backend, web≈frontend) over its real roots.
        be_files = _grep_layer_files(sb, w, lay, "api", inc)
        fe_files = _grep_layer_files(sb, w, lay, "web", inc)
        if be_files or fe_files:
            field_map.append({"symbol": w, "backend": be_files[:6], "frontend": fe_files[:6]})
    return field_map


def _grep_layer_files(sb: tools.Sandbox, term: str, lay: layout.Layout,
                      kind: str, inc: str) -> list[str]:
    if not lay.by_kind(kind):
        return []
    out = sb.run_shell(f"grep -rl {inc} -- {term!r} {lay.grep_scope(kind)} 2>/dev/null | head -10")
    return [l for l in out.split("\n")[1:] if l.strip() and "node_modules" not in l]


# ── generate (single-phase + two-phase) ──────────────────────────────────────

_GEN_HARD_RULES = (
    "You are the implementation worker for a real full-stack repo. Make the "
    "SMALLEST correct change, reusing existing files and following existing style. "
    "Hard rules: (1) change as few files as possible; do NOT create new components, "
    "helpers, or directories unless strictly required. (2) Add EXACTLY ONE "
    "automated test file (prefer the frontend, where the runner is wired) that "
    "proves the change; do NOT scatter multiple test files. (3) Run that test "
    "yourself with run_shell ({test_cmd}) and do not reply DONE until the WHOLE "
    "command exits 0. Import paths in a test resolve relative to the TEST file's "
    "own folder. Read files before editing. ")


def _gen_worker(ctx: RunContext, *, system: str, task: str, max_rounds: int):
    """Generate worker with DYNAMIC token budget = min(per-gen cap, remaining run
    budget). A fixed per-generate cap lets the original + repairs blow past the
    run cap; tying it to the remaining run budget makes the run-level cap hard."""
    per_gen = int(os.environ.get("STACKWEFT_GEN_TOKEN_BUDGET", "60000"))
    run_cap = int(os.environ.get("STACKWEFT_RUN_TOKEN_CAP", "120000"))
    spent = obs.run_cost_summary(ctx.run_id)["tokens_in"]
    budget = max(2000, min(per_gen, run_cap - spent))
    return worker.run_task(sandbox=ctx.sandbox, system=system, task=task,
                           run_id=ctx.run_id, stage="generate", level=0.6,
                           max_rounds=max_rounds, max_tokens=4096,
                           token_budget=budget)


def _is_cross_stack(ctx: RunContext) -> bool:
    """Decide two-phase routing on EVIDENCE, not the LLM's spec.layers guess
    (the LLM over-tags layers). True when the grep field_map shows
    at least one symbol present on BOTH backend and frontend, OR the localize
    targets list names both a backend/ and a frontend/ file."""
    fm = ctx.outputs.get("localize", {}).get("field_map") or []
    if any(f.get("backend") and f.get("frontend") for f in fm):
        return True
    tgts = ctx.outputs.get("localize", {}).get("targets", {}).get("targets", []) or []
    paths = [str(t.get("path", "")) for t in tgts]
    return layout.for_sandbox(ctx.sandbox).is_cross_stack(paths)


def _rename_branch(ctx: RunContext, field: str) -> None:
    """Once the field is known, give the git branch a meaningful name. The branch is
    created before clarify, so an all-Chinese ask (no ASCII to slugify) starts as a generic
    '<repo>/change-<id>'; rename it to '<repo>/add-<field>-<id>'. The prefix is the TARGET
    repo's name (not the tool name), so it reads as that repo's namespace. Best-effort."""
    repo = re.sub(r"[^a-z0-9]+", "-", ctx.sandbox.root.name.lower()).strip("-") or "repo"
    slug = re.sub(r"[^a-z0-9]+", "-", field.lower()).strip("-") or "field"
    new = f"{repo}/add-{slug}-{ctx.run_id[:6]}"
    if new == ctx.branch:
        return
    try:
        ctx.sandbox.run_shell(f"git branch -m {ctx.branch} {new} 2>&1 || true")
        ctx.branch = new
        obs.update_run(ctx.run_id, branch=new)
    except Exception:  # noqa: BLE001
        pass


def stage_compile(ctx: RunContext) -> dict[str, Any]:
    """Read-only compile: requirement → validated Field Flow Graph.

    Engages when the SELECTED Skill carries a slot spec (a Field-Flow Skill).
    Adding a new requirement TYPE = drop in another such Skill file; the trunk
    reads its slots, nothing is hardcoded here. Builds the graph by grepping the
    shadow anchors' REAL paths, writes the deterministic new files (correct-by-
    construction probe tests), and FAILS FAST if any path/anchor is missing — a
    bad contract never reaches generate (no degradation, no hallucinated targets)."""
    skill_path = getattr(ctx.skill, "path", "") or ""
    if not skill_path or not os.path.isfile(skill_path):
        return {"engaged": False, "reason": "selected skill has no file (generic fallback)"}
    text = open(skill_path, encoding="utf-8").read()
    try:
        fieldflow.load_skill_spec(text)
    except ValueError:
        return {"engaged": False, "reason": "selected skill has no slot spec"}
    meta = fieldflow.skill_meta(text)
    entity = meta.get("entity", "")
    # Engagement gate: the entity must be referenced in the requirement. A
    # natural-language requirement may name it in another language (e.g. 中文
    # "文章" for Article), so a Skill can declare ``entity_aliases`` and the
    # trunk accepts any of them — nothing language-specific is hardcoded here.
    aliases = [a.strip() for a in (meta.get("entity_aliases", "") or "").split(",") if a.strip()]
    candidates = [e.lower() for e in ([entity] + aliases) if e]
    req_lc = ctx.requirement.lower()
    if candidates and not any(c in req_lc for c in candidates):
        return {"engaged": False, "reason": f"entity {entity!r} (or aliases) not in requirement"}
    # Prefer the field name the clarify LLM derived from the (possibly name-less) natural-
    # language ask — so "给文章加个一句话标语" yields `tagline` without the user naming it.
    # Validate it's a plain identifier; otherwise fall back to the deterministic regex guess.
    _cf = str((ctx.outputs.get("clarify", {}).get("spec", {}) or {}).get("field_name", "") or "").strip()
    field = _cf if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,40}", _cf) else fieldflow.guess_field(ctx.requirement, meta)
    type_ = fieldflow.guess_type(ctx.requirement, field, meta)  # BOOLEAN/INTEGER/DATE/STRING
    _rename_branch(ctx, field)  # generic 'change-xxx' (all-Chinese ask) → meaningful 'add-<field>-xxx'
    want_index = bool(fieldflow.INDEX_REQ_RE.search(ctx.requirement))  # 索引/index/unique
    ir = fieldflow.build_graph(ctx.sandbox, text, field=field, type_=type_, want_index=want_index)
    obs.log_event(ctx.run_id, stage="compile", event="compile_graph",
                  payload={"field": field, "entity": entity,
                           "slots": [{"slot": n["slot"], "path": n.get("path"),
                                      "anchors": n.get("anchors")} for n in ir["slots"]],
                           "errors": ir["errors"]})
    if ir["errors"]:
        # Contract validity gate: fail fast, do NOT degrade and continue.
        raise RuntimeError("contract invalid (fail-fast): " + "; ".join(ir["errors"]))
    written = fieldflow.render_new_files(ctx.sandbox, ir)
    obs.log_event(ctx.run_id, stage="compile", event="new_files",
                  payload={"written": written})
    ir["new_files"] = written
    ir["engaged"] = True
    return ir


_SLOT_RULES = (
    "You are threading a new field into EXACTLY ONE file. Rules:\n"
    "- Make ONLY the change described. Do NOT touch any other file.\n"
    "- FIRST call read_window on the target file around the given anchor lines to "
    "see the exact current code.\n"
    "- Then use multi_edit/edit_file with `old` strings copied VERBATIM from what "
    "you read (each `old` must be unique in the file).\n"
    "- Mirror exactly how the shadow field is already handled — same locations, "
    "same style — unless the instruction says otherwise.\n"
    "- Do NOT run tests, do NOT git commit, do NOT create or delete files.\n"
    "- Reply DONE once the required evidence is present.\n\n")


def _run_slot(ctx: RunContext, node: dict[str, Any], taskir: dict[str, Any],
              max_rounds: int = 4) -> dict[str, Any]:
    """Fill ONE edit slot with a focused, small-budget worker, then verify the
    slot's evidence deterministically and retry once with the specific miss."""
    field, shadow, type_ = taskir["field"], taskir["shadow_field"], taskir["type"]
    path, anchors = node["path"], node.get("anchors", [])
    ev_patterns, min_count = node["evidence"], node["min_count"]

    def evidence_ok() -> tuple[bool, str]:
        for ev in ev_patterns:
            hits = fieldflow._grep_lines(ctx.sandbox, path, ev)
            if len(hits) < min_count:
                return False, f"pattern /{ev}/ found {len(hits)}x, need {min_count}x"
        return True, "ok"

    ok, why = evidence_ok()
    if ok:
        return {"slot": node["slot"], "ok": True, "skipped": "already satisfied"}

    before = (ctx.sandbox.root / path).read_text(encoding="utf-8", errors="replace")
    system = _SLOT_RULES
    task = (f"Add field `{field}` (Sequelize type {type_}) to ONE file, mirroring "
            f"the existing field `{shadow}`.\n\n"
            f"TARGET FILE: {path}\n"
            f"ANCHOR LINES (1-indexed; the shadow field / insertion point is here): {anchors}\n\n"
            f"INSTRUCTION: {node['instruction']}\n\n"
            f"REQUIRED EVIDENCE after your edit — these regex must each match "
            f"≥{min_count}× in {path}: {ev_patterns}\n\n"
            f"Start by reading a window around the anchor lines, then make the edit.")
    last = ""
    for _try in range(2):
        res = _gen_worker(ctx, system=system, task=task, max_rounds=max_rounds)
        ok, why = evidence_ok()
        obs.log_event(ctx.run_id, stage="generate", event="slot_filled",
                      payload={"slot": node["slot"], "path": path, "ok": ok,
                               "why": why, "rounds": res.rounds, "try": _try + 1})
        if ok:
            try:  # capture a recipe from the agentic edit via before/after diff
                after = (ctx.sandbox.root / path).read_text(encoding="utf-8", errors="replace")
                edits = recipes.edits_from_diff(before, after)
                window = _raw_window(ctx, path, node.get("anchors", []))
                # NB: surface_hash must reflect the pre-edit window — recompute from `before`.
                pre_lines = before.splitlines()
                a = (min(node["anchors"]) - 1) if node.get("anchors") else 0
                pre_win = "\n".join(pre_lines[max(0, a - 60):a + 60])
                recipes.record(skill=ctx.skill.name, slot=node["slot"],
                               repo_id=(ctx.repo_id or ctx.sandbox.root.name),
                               surface_hash=recipes.surface_hash(
                                   path, pre_win, shadow, kind=fieldflow.display_kind(field)),
                               shadow=shadow, field=field, type_=type_, edits=edits)
            except Exception:  # noqa: BLE001
                pass
            return {"slot": node["slot"], "ok": True, "rounds": res.rounds}
        last = why
        task += (f"\n\nYOUR PREVIOUS EDIT DID NOT SATISFY THE EVIDENCE: {why}. "
                 f"Re-read {path} and add the missing usage of `{field}`.")
    return {"slot": node["slot"], "ok": False, "why": last}


_SLOTJOB_RULES = (
    "You are a precise code-patch generator. You receive ONE file's current code "
    "window, a field to thread in, and the required evidence. Output STRICT JSON "
    "ONLY — no prose, no markdown fence:\n"
    '{"edits":[{"path":"<file>","old":"<verbatim substring of the shown code, '
    'unique in the file>","new":"<replacement>"}]}\n'
    "Rules: every `old` MUST be copied verbatim from the shown code and occur "
    "exactly once in the file. Make the smallest change that satisfies the required "
    "evidence; mirror the shadow field's existing style. You MAY return several "
    "edits for the same file. Do NOT touch other files, do NOT add unused "
    "components. Output ONLY the JSON object.")


def _raw_window(ctx: RunContext, path: str, anchors: list[int], pad: int = 60) -> str:
    lines = (ctx.sandbox.root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    a = (min(anchors) - 1) if anchors else 0
    lo, hi = max(0, a - pad), min(len(lines), a + pad)
    return "\n".join(lines[lo:hi])


def _run_slot_oneshot(ctx: RunContext, node: dict[str, Any], taskir: dict[str, Any],
                      retries: int = 1) -> dict[str, Any] | None:
    """Fill a slot with ONE LLM call that returns a patch, instead
    of a 4-round agentic tool loop. The orchestrator pre-reads the window and applies
    the patch; no tool catalog / no growing history is sent. Returns None if it can't
    satisfy the slot (caller falls back to the agentic worker so reliability holds)."""
    field, shadow, type_ = taskir["field"], taskir["shadow_field"], taskir["type"]
    path, anchors = node["path"], node.get("anchors", [])
    ev_patterns, min_count = node["evidence"], node["min_count"]

    def evidence_ok() -> tuple[bool, str]:
        for ev in ev_patterns:
            if len(fieldflow._grep_lines(ctx.sandbox, path, ev)) < min_count:
                return False, f"/{ev}/ < {min_count}x"
        return True, "ok"

    ok, _ = evidence_ok()
    if ok:
        return {"slot": node["slot"], "ok": True, "skipped": "already satisfied", "mode": "oneshot"}

    # Approval gate for the slot edit (recipe/deterministic/one-shot all bypass dispatch).
    # always-ask → asks before each slot edit; other modes auto-allow the 'edit' kind.
    try:
        from stackweft.platform import govern
        _ok, _why = govern.gate_action(ctx.run_id, "edit",
                                       f"编辑槽位 {node['slot']} → {path}",
                                       detail=(node.get("instruction") or "")[:140])
        if not _ok:
            obs.log_event(ctx.run_id, stage="generate", event="slot_denied",
                          payload={"slot": node["slot"], "why": _why})
            return {"slot": node["slot"], "ok": False, "denied": _why, "mode": "approval"}
    except Exception:  # noqa: BLE001
        pass

    window = _raw_window(ctx, path, anchors)
    shash = recipes.surface_hash(path, window, shadow, kind=fieldflow.display_kind(field))
    repo_id, skill = (ctx.repo_id or ctx.sandbox.root.name), ctx.skill.name

    # Try a guarded patch-recipe replay first → 0-LLM hot path. Apply, verify
    # the SAME slot evidence, roll back + fall through to the LLM on any miss.
    if os.environ.get("STACKWEFT_RECIPE", "1") == "1":
        rep = recipes.lookup(skill=skill, slot=node["slot"], repo_id=repo_id,
                             surface_hash=shash, field=field, type_=type_)
        if rep:
            before = (ctx.sandbox.root / path).read_text(encoding="utf-8", errors="replace")
            applied = ctx.sandbox.multi_edit([{"path": path, "old": e["old"], "new": e["new"]} for e in rep])
            ok, why = evidence_ok()
            obs.log_event(ctx.run_id, stage="generate", event="recipe_replay",
                          payload={"slot": node["slot"], "ok": ok, "why": why, "applied": applied[:120]})
            if ok:
                return {"slot": node["slot"], "ok": True, "mode": "recipe_replay", "llm_calls": 0}
            (ctx.sandbox.root / path).write_text(before, encoding="utf-8")  # rollback

    # P1: deterministic 0-LLM fill for low-entropy slots (column decl / list render).
    # Insert a Shadow-cloned line after the anchor; verify the SAME evidence; roll back
    # and fall through to the LLM if it doesn't satisfy (so reliability is unchanged).
    if node.get("det"):
        before = (ctx.sandbox.root / path).read_text(encoding="utf-8", errors="replace")
        if fieldflow.apply_det(ctx.sandbox, node):
            ok, why = evidence_ok()
            obs.log_event(ctx.run_id, stage="generate", event="slot_filled",
                          payload={"slot": node["slot"], "path": path, "ok": ok, "why": why,
                                   "mode": "deterministic", "llm_calls": 0})
            if ok:
                return {"slot": node["slot"], "ok": True, "mode": "deterministic", "llm_calls": 0}
            (ctx.sandbox.root / path).write_text(before, encoding="utf-8")  # rollback → LLM

    base = (f"FILE: {path}\nFIELD: `{field}` (type {type_}); mirror existing field `{shadow}`.\n"
            f"INSTRUCTION: {node['instruction']}\n"
            f"REQUIRED EVIDENCE (each regex must appear >= {min_count}x after your edit): {ev_patterns}\n\n"
            f"CURRENT CODE (verbatim — copy each `old` from here):\n```\n{window}\n```\nReturn the JSON now.")
    task, why = base, ""
    for attempt in range(retries + 1):
        res = llm.messages(system=_SLOTJOB_RULES, msgs=[{"role": "user", "content": task}],
                           level=0.6, max_tokens=2048,
                           run_id=ctx.run_id, stage="generate",
                           purpose=f"slotjob_{node['slot']}")
        patch = _extract_json(res.text)
        edits = patch.get("edits") or ([patch] if patch.get("old") else [])
        edits = [{"path": e.get("path", path), "old": e["old"], "new": e["new"]}
                 for e in edits if isinstance(e, dict) and e.get("old") is not None]
        applied = ctx.sandbox.multi_edit(edits) if edits else "no valid edits in JSON"
        ok, why = evidence_ok()
        obs.log_event(ctx.run_id, stage="generate", event="slot_filled",
                      payload={"slot": node["slot"], "path": path, "ok": ok, "why": why,
                               "mode": "oneshot", "try": attempt + 1, "applied": applied[:120]})
        if ok:
            try:  # sediment a parameterized recipe for the hot path next time
                recipes.record(skill=skill, slot=node["slot"], repo_id=repo_id,
                               surface_hash=shash, shadow=shadow, field=field,
                               type_=type_, edits=edits)
            except Exception:  # noqa: BLE001
                pass
            return {"slot": node["slot"], "ok": True, "mode": "oneshot", "tries": attempt + 1}
        task = (base + f"\n\nYOUR PREVIOUS PATCH FAILED: {why}; apply result: {applied[:160]}. "
                "Re-read the CURRENT CODE above and return corrected JSON edits.")
    return None  # signal: fall back to agentic worker


def _stage_generate_slotwise(ctx: RunContext, *, repair: bool,
                             max_rounds: int = 4) -> dict[str, Any]:
    """Slot-driven generate: the worker FILLS each verified graph node with a
    tiny focused task (read_window → edit), instead of free-roaming the repo.
    On repair, only the slots whose evidence is still missing are re-run."""
    taskir = ctx.outputs["compile"]
    nodes = fieldflow.edit_slots(taskir)
    if repair:
        ev = fieldflow.cross_stack_evidence(ctx.sandbox, taskir)
        failing = {m["slot"] for m in ev["missing_slots"]}
        if not failing:  # evidence complete but a probe still red → re-touch render/service
            failing = {"frontend_list_render", "frontend_detail_render",
                       "frontend_write_service", "frontend_editor_form", "backend_model"}
        nodes = [n for n in nodes if n["slot"] in failing]
    obs.log_event(ctx.run_id, stage="generate", event="slotwise_start",
                  payload={"repair": repair, "slots": [n["slot"] for n in nodes]})
    oneshot = os.environ.get("STACKWEFT_SLOT_ONESHOT", "1") == "1"
    results = []
    for n in nodes:
        ctl = obs.get_control(ctx.run_id)  # responsive interrupt between slots
        if ctl and ctl["action"] in ("pause", "abort"):
            obs.log_event(ctx.run_id, stage="generate", event=f"control_{ctl['action']}_midgen",
                          payload={"remaining": [x["slot"] for x in nodes[len(results):]]})
            break
        r = _run_slot_oneshot(ctx, n, taskir) if oneshot else None
        if r is None:  # one-shot couldn't satisfy the slot → agentic fallback (keeps reliability)
            if oneshot:
                obs.log_event(ctx.run_id, stage="generate", event="slot_fallback",
                              payload={"slot": n["slot"]})
            r = _run_slot(ctx, n, taskir)
        results.append(r)
    ok = all(r["ok"] for r in results)
    return {"summary": f"slotwise field={taskir['field']} "
                       + "; ".join(f"{r['slot']}={'ok' if r['ok'] else 'MISS'}" for r in results),
            "ok": ok, "slot_results": results, "rounds": sum(1 for _ in results),
            "tool_calls": 0, "two_phase": False, "slotwise": True, "field": taskir["field"]}


def stage_generate(ctx: RunContext, repair_feedback: str = "",
                   max_rounds: int = 14) -> dict[str, Any]:
    # When compile produced a validated Field Flow Graph, fill it
    # slot-by-slot (worker fills the graph, never invents it). Otherwise fall
    # back to the legacy single/two-phase generate (single-layer requirements).
    if ctx.outputs.get("compile", {}).get("engaged"):
        return _stage_generate_slotwise(ctx, repair=bool(repair_feedback),
                                        max_rounds=max(3, max_rounds // 3 + 1))
    # Route genuine cross-stack changes to two-phase (one worker doing
    # backend+frontend+tests sprawls). Decision is EVIDENCE-based (grep /
    # localize targets), NOT clarify's layers guess. Repairs stay single-phase.
    _xstack = _is_cross_stack(ctx)
    _fm = ctx.outputs.get("localize", {}).get("field_map") or []
    obs.log_event(ctx.run_id, stage="generate", event="route_decision",
                  payload={"two_phase_env": os.environ.get("STACKWEFT_TWO_PHASE", "1"),
                           "repair": bool(repair_feedback),
                           "is_cross_stack": _xstack,
                           "both_side_symbols": [f["symbol"] for f in _fm
                                                 if f.get("backend") and f.get("frontend")]})
    if (os.environ.get("STACKWEFT_TWO_PHASE", "1") == "1" and not repair_feedback
            and _xstack):
        return _stage_generate_two_phase(ctx, max_rounds=max_rounds)

    guide = ctx.skill.guidance("generate") or skills.FALLBACK.guidance("generate")
    spec = ctx.outputs["clarify"]["spec"]
    plan = ctx.outputs["plan"]["plan"]
    targets = ctx.outputs["localize"].get("targets", {})
    test_cmd = _discover_test_cmd(ctx.sandbox) or "cd frontend && npm test"
    system = _GEN_HARD_RULES.format(test_cmd=f"`{test_cmd}`") + guide
    task = (f"Spec:\n{json.dumps(spec, ensure_ascii=False)}\n\n"
            f"Plan:\n{json.dumps(plan, ensure_ascii=False)}\n\n"
            f"Target files:\n{json.dumps(targets, ensure_ascii=False)}\n\n"
            f"Test command (run this to self-verify): {test_cmd}\n\n"
            "Implement end-to-end now, then run the test command and fix until it "
            "passes. Keep frontend/backend consistent. Reply DONE only after it passes.")
    if repair_feedback:
        task += f"\n\nPREVIOUS VERIFY FAILED — fix these:\n{repair_feedback}"
    res = _gen_worker(ctx, system=system, task=task, max_rounds=max_rounds)
    return {"summary": res.final_text, "ok": res.ok, "rounds": res.rounds,
            "tool_calls": res.tool_calls, "two_phase": False}


def _stage_generate_two_phase(ctx: RunContext, *, max_rounds: int) -> dict[str, Any]:
    """Cross-stack generate split into bounded sub-phases.
    Phase A: backend model + API. Phase B: frontend + its one test, told what the
    backend now exposes. Each phase is its own bounded worker sharing the dynamic
    budget, so neither sprawls and the change lands on BOTH sides. Phase B is told
    to create the component BEFORE writing the test that imports it."""
    spec = ctx.outputs["clarify"]["spec"]
    plan = ctx.outputs["plan"]["plan"]
    targets = ctx.outputs["localize"].get("targets", {})
    fe_test = _discover_test_cmd(ctx.sandbox) or "cd frontend && npm test"
    guide = ctx.skill.guidance("generate") or skills.FALLBACK.guidance("generate")
    half = max(6, max_rounds // 2 + 1)

    # The recurring multi-layer failure is phase B changing the backend but NOT
    # wiring the field into the frontend. Fix = a CONTRACT: phase A ends with STRICT JSON
    # {field_name, api_shape}; phase B is handed the exact backend field name + the
    # exact frontend target files it must touch, and told the test must assert the
    # field renders. Direct fix, independent of role rework.
    _lay = layout.for_sandbox(ctx.sandbox)
    fe_targets = [str(t.get("path")) for t in (targets.get("targets") or [])
                  if _lay.kind_of(str(t.get("path", ""))) == "web"]

    # Backend has its own runnable test gate (model-attribute test, driver-
    # free — native sqlite3/pg don't compile here). Phase A must extend it to
    # assert the new column is defined, and run it green before finishing.
    be_test = next((l.test_cmd for l in _lay.by_kind("api") if l.test_cmd), None)
    sys_a = (_GEN_HARD_RULES.format(test_cmd=(f"`{be_test}`" if be_test else "the project test command")) +
             "THIS PHASE: BACKEND ONLY. Add the field/behaviour to the Sequelize "
             "model and make the API return/accept it. Do NOT touch frontend yet. " +
             (f"A backend test runner exists: extend backend/test/article.model.test.js "
              f"to assert the new column is defined, then run `{be_test}` until it exits 0. "
              if be_test else "") + guide)
    task_a = (f"Spec:\n{json.dumps(spec, ensure_ascii=False)}\n\n"
              f"Plan:\n{json.dumps(plan, ensure_ascii=False)}\n\n"
              f"Target files:\n{json.dumps(targets, ensure_ascii=False)}\n\n"
              + (f"Backend test command (run to self-verify): {be_test}\n\n" if be_test else "")
              + "Implement ONLY the backend part now (model + API), minimal"
              + (" and make the backend test green" if be_test else "") + ". Your FINAL "
              "message must be exactly one line of STRICT JSON describing the contract "
              'the frontend consumes, e.g.: {"field_name": "coverImage", "api_shape": '
              '"article.coverImage is a string URL"}. Output only that JSON last.')
    res_a = _gen_worker(ctx, system=sys_a, task=task_a, max_rounds=half)

    contract = _extract_json(res_a.final_text)
    backend_field = contract.get("field_name") or _guess_field(spec, ctx.requirement)
    api_shape = contract.get("api_shape", res_a.final_text[:400])
    obs.log_event(ctx.run_id, stage="generate", event="phase_a_contract",
                  payload={"backend_field": backend_field,
                           "api_shape": str(api_shape)[:200],
                           "frontend_targets": fe_targets})

    fe_target_str = "\n".join(f"- {p}" for p in fe_targets) or "(infer from grep; none listed)"
    sys_b = (_GEN_HARD_RULES.format(test_cmd=f"`{fe_test}`") +
             "THIS PHASE: FRONTEND ONLY. You MUST wire the backend field below into "
             "the listed frontend files and add ONE test that ASSERTS the field "
             "renders/appears. If you create a component, create it BEFORE the test "
             "that imports it. CRITICAL on imports: every import in your test must "
             "resolve to a file that EXISTS — use the exact relative path from the "
             "test file's own folder (run `ls` to confirm the target file before "
             "importing it). Do NOT create empty directories. If `npx vitest run` "
             "reports 'failed to load url' or 'No test files found in directory', that "
             "is YOUR bug to fix before DONE. " + guide)
    task_b = (f"Spec:\n{json.dumps(spec, ensure_ascii=False)}\n\n"
              f"BACKEND CONTRACT — the API now exposes field `{backend_field}`: {api_shape}\n\n"
              f"FRONTEND FILES YOU MUST UPDATE to consume `{backend_field}`:\n{fe_target_str}\n\n"
              f"Test command (run this to self-verify): {fe_test}\n\n"
              f"Requirements: (1) make `{backend_field}` render in the app. You MAY "
              f"either edit a listed file directly, OR create a small component AND "
              f"import/use it inside one of the listed files — but at least ONE of the "
              f"listed files above MUST end up referencing `{backend_field}` (directly "
              f"or via your new component), so the existing render path is wired. "
              f"(2) Add ONE test asserting `{backend_field}` appears. (3) Run the test "
              "command and fix until it exits 0. Reply DONE only after it passes.")
    res_b = _gen_worker(ctx, system=sys_b, task=task_b, max_rounds=half)

    return {"summary": f"[backend field={backend_field}]\n{res_a.final_text[:400]}\n\n"
                       f"[frontend targets={fe_targets}]\n{res_b.final_text[:400]}",
            "backend_field": backend_field, "frontend_targets": fe_targets,
            "ok": res_a.ok and res_b.ok,
            "rounds": res_a.rounds + res_b.rounds,
            "tool_calls": res_a.tool_calls + res_b.tool_calls,
            "two_phase": True}


def _guess_field(spec: dict[str, Any], requirement: str) -> str:
    """Fallback backend field name when phase A's JSON contract didn't parse."""
    text = " ".join([str(spec.get("summary", "")), str(requirement)])
    cands = [w for w in re.findall(r"[a-z][A-Za-z0-9]{3,}", text)
             if any(c.isupper() for c in w[1:])]
    return cands[0] if cands else "newField"


# ── verify ───────────────────────────────────────────────────────────────────

def _eslint_error_count(sb: tools.Sandbox) -> int | None:
    web = layout.for_sandbox(sb).by_kind("web")
    if not web or not (sb.root / web[0].roots[0]).is_dir():
        return None
    root = web[0].roots[0]
    exts = ",".join(e.lstrip(".") for e in web[0].exts) or "js,jsx"
    target = "src" if (sb.root / root / "src").is_dir() else "."
    out = sb.run_shell(f"cd {root} && npx eslint {target} --ext {exts} -f json 2>/dev/null",
                       timeout=300)
    body = out.split("\n", 1)[1] if out.startswith("exit=") else out
    a, b = body.find("["), body.rfind("]")
    if a < 0 or b <= a:
        return None
    try:
        data = json.loads(body[a:b + 1])
    except Exception:
        return None
    return sum(int(f.get("errorCount", 0)) for f in data)


def stage_verify(ctx: RunContext) -> dict[str, Any]:
    guide = ctx.skill.guidance("verify") or skills.FALLBACK.guidance("verify")
    results, all_ok = [], True

    # 1) Differential lint gate (None baseline → can't judge regression → pass).
    baseline = ctx.outputs.get("_lint_baseline")
    current = _eslint_error_count(ctx.sandbox)
    if current is not None:
        regressed = baseline is not None and current > baseline
        all_ok = all_ok and not regressed
        results.append({"name": "frontend_lint_diff", "passed": not regressed,
                        "out": f"baseline={baseline} current={current} "
                               f"{'REGRESSED' if regressed else 'ok'}"})
        obs.log_event(ctx.run_id, stage="verify", event="check",
                      payload={"name": "lint_diff", "baseline": baseline,
                               "current": current, "passed": not regressed})

    # 2) Test gate: suite green AND a *.test.* changed this run (filesystem
    # snapshot, not git — worker commits mid-run).
    test_cmd = _discover_test_cmd(ctx.sandbox)
    if test_cmd:
        out = ctx.sandbox.run_shell(test_cmd, timeout=480)
        suite_green = (out.startswith("exit=0") and "No test files found" not in out
                       and "passed" in out)
        changed = _changed_files(ctx)
        new_test = any(("test." in p or "spec." in p or "__tests__" in p) for p in changed)
        if not new_test:
            # Robust to a dirty/uncleaned tree where the probe file already exists from
            # a prior run, so the fs-baseline diff sees no *change*: the delivery still
            # shipped a test if compile authored a probe file that is present on disk.
            # (Without this, the gate false-fails and repair can never fix it — repair
            # only re-fills code slots, it never authors test files.)
            probes = ctx.outputs.get("compile", {}).get("new_files") or []
            new_test = any(("test." in p or "spec." in p)
                           and (ctx.sandbox.root / p).is_file() for p in probes)
        passed = suite_green and new_test
        reason = ("ok" if passed else
                  ("suite not green" if not suite_green
                   else "no new/changed test file in this run — change must add a test"))
        all_ok = all_ok and passed
        results.append({"name": "tests", "cmd": test_cmd, "passed": passed,
                        "reason": reason, "out": out[-1500:]})
        obs.log_event(ctx.run_id, stage="verify", event="check",
                      payload={"name": "tests", "passed": passed, "reason": reason})
    else:
        results.append({"name": "tests", "passed": False,
                        "out": "no runnable test script — change must ADD a test"})
        all_ok = False

    # 3) Cross-stack consistency gate (evidence-based).
    cs = _cross_stack_check(ctx)
    if cs is not None:
        all_ok = all_ok and cs["passed"]
        results.append(cs)
        obs.log_event(ctx.run_id, stage="verify", event="check",
                      payload={"name": "cross_stack", "passed": cs["passed"]})

    return {"passed": all_ok, "checks": results, "guide": guide}


def _cross_stack_check(ctx: RunContext) -> dict[str, Any] | None:
    """Cross-stack consistency, EVIDENCE-based. Hard-fail ONLY when
    field_map (grep) shows a symbol on BOTH sides and the diff changed one side
    but not the other. The "spec.layers spans both" signal is ADVISORY only —
    layers is an LLM guess that over-classifies.
    We never hard-gate on an LLM guess."""
    # When compile produced a Field Flow Graph, the gate is "every
    # must-touch slot carries the field" — much stronger than "field appears on
    # both ends" (a field could appear in a dead component). The sentinel probe
    # tests (run by the test gate) additionally prove the value crosses the stack.
    taskir = ctx.outputs.get("compile", {})
    if taskir.get("engaged"):
        ev = fieldflow.cross_stack_evidence(ctx.sandbox, taskir)
        gaps = [f"{m['slot']} ({m['path']}): " + (m.get("reason")
                or f"need {m.get('need')}x evidence, found {m.get('found')}")
                for m in ev["missing_slots"]]
        return {"name": "cross_stack", "passed": ev["passed"],
                "out": "all slots carry the field" if ev["passed"]
                else "SLOT GAPS: " + "; ".join(gaps[:8]),
                "missing_slots": ev["missing_slots"]}

    spec = ctx.outputs.get("clarify", {}).get("spec", {})
    layers = set(spec.get("layers", []) or [])
    multi = {"frontend", "backend"} <= layers
    field_map = ctx.outputs.get("localize", {}).get("field_map") or []
    if not field_map and not multi:
        return None
    changed = _changed_files(ctx)
    hard_gaps, advisories = [], []
    for fm in field_map:
        if not (fm.get("backend") and fm.get("frontend")):
            continue
        be_hit = any(any(f in c for c in changed) for f in fm["backend"])
        fe_hit = any(any(f in c for c in changed) for f in fm["frontend"])
        if be_hit and not fe_hit:
            hard_gaps.append(f"{fm['symbol']}: backend changed but its frontend usage not updated")
        if fe_hit and not be_hit:
            hard_gaps.append(f"{fm['symbol']}: frontend changed but its backend def not updated")
    if multi:
        _lay = layout.for_sandbox(ctx.sandbox)
        be_touched = any(_lay.kind_of(c) == "api" for c in changed)
        fe_touched = any(_lay.kind_of(c) == "web" for c in changed)
        if be_touched and not fe_touched:
            advisories.append("spec hints both layers but only BACKEND files changed")
        if fe_touched and not be_touched:
            advisories.append("spec hints both layers but only FRONTEND files changed")
    passed = not hard_gaps
    parts = []
    if hard_gaps:
        parts.append("GAPS: " + "; ".join(hard_gaps[:5]))
    if advisories:
        parts.append("(advisory: " + "; ".join(advisories[:3]) + ")")
    return {"name": "cross_stack", "passed": passed,
            "out": " ".join(parts) if parts else "no cross-stack gaps"}


# ── pr + rag precipitation ───────────────────────────────────────────────────

def stage_pr(ctx: RunContext) -> dict[str, Any]:
    sb = ctx.sandbox
    sb.run_shell("git add -A")
    spec = ctx.outputs.get("clarify", {}).get("spec", {})
    msg = f"feat: {str(spec.get('summary', ctx.requirement))[:72]}"
    commit = sb.run_shell(
        f"git -c user.email=stackweft@local -c user.name=StackWeft commit -m {json.dumps(msg)} || true")
    stat = sb.run_shell("git show --stat HEAD | head -50")
    if _rag is not None and ctx.outputs.get("verify", {}).get("passed"):
        try:
            targets = ctx.outputs.get("localize", {}).get("targets", {})
            paths = [t.get("path") for t in (targets.get("targets") or []) if t.get("path")]
            _rag.remember(run_id=ctx.run_id, requirement_raw=ctx.requirement,
                          skill=ctx.skill.name, spec=spec, targets=paths,
                          test_added="", outcome="passed",
                          pitfalls=_extract_pitfalls(ctx.run_id), diff_stat=stat[:300],
                          repo_id=(ctx.repo_id or ctx.sandbox.root.name))
            obs.log_event(ctx.run_id, stage="pr", event="rag_remembered", payload={})
        except Exception as e:  # noqa: BLE001
            obs.log_event(ctx.run_id, stage="pr", event="rag_remember_failed",
                          payload={"error": repr(e)})
    return {"branch": ctx.branch, "commit_msg": msg, "commit": commit, "stat": stat}


def _extract_pitfalls(run_id: str) -> list[str]:
    """Auto-mine pitfalls from this run's events so req_memory learns."""
    pitfalls: list[str] = []
    with obs.connect() as conn:
        evs = conn.execute(
            "SELECT stage,event,payload_json FROM workflow_events WHERE run_id=? "
            "ORDER BY ts", (run_id,)).fetchall()
    for e in evs:
        try:
            p = json.loads(e["payload_json"] or "{}")
        except Exception:
            p = {}
        if e["stage"] == "verify" and e["event"] == "check" and p.get("passed") is False:
            pitfalls.append(f"verify:{p.get('name')} failed at least once")
        if e["event"] == "repair_attempt":
            pitfalls.append("needed a verify→repair cycle (test didn't pass first try)")
        if e["event"] == "worker_thrash_stop":
            pitfalls.append(f"worker thrashed on {p.get('path')}; converge faster")
    loc = obs.load_stage(run_id, "localize")
    pl = obs.load_stage(run_id, "plan")
    if loc and loc["output_json"] and pl and pl["output_json"]:
        try:
            tj = json.loads(loc["output_json"]).get("targets", {})
            final = {t.get("path") for t in (tj.get("targets") or []) if t.get("path")}
            hint = set(json.loads(pl["output_json"]).get("plan", {}).get("files_hint", []))
            if final and hint and not (final & hint):
                pitfalls.append("plan file hints were wrong; real files differ — "
                                "localize/grep before trusting the plan")
        except Exception:
            pass
    seen, out = set(), []
    for p in pitfalls:
        if p not in seen:
            seen.add(p); out.append(p)
    return out[:8]


def _discover_test_cmd(sb: tools.Sandbox) -> str:
    # Layout-driven: prefer the web layer's test (where probes run), then api, then the
    # workspace-root test. Run from INSIDE the package dir via subshell (the `npm --prefix`
    # form intermittently false-fails in npm-workspaces and reddens the gate spuriously).
    lay = layout.for_sandbox(sb)
    web = [l.test_cmd for l in lay.by_kind("web") if l.test_cmd]
    api = [l.test_cmd for l in lay.by_kind("api") if l.test_cmd]
    for cmd in (*web, *api, lay.root_test_cmd):
        if cmd:
            return cmd
    return ""


# ── runner ───────────────────────────────────────────────────────────────────

_STAGE_FNS = {"clarify": stage_clarify, "plan": stage_plan, "localize": stage_localize,
              "compile": stage_compile, "generate": stage_generate,
              "verify": stage_verify, "pr": stage_pr}


def run(ctx: RunContext, *, resume: bool = True, redo_from: str | None = None,
        stop_after: str | None = None, verify_repair_rounds: int = 2,
        ask: bool = False) -> dict[str, Any]:
    # Bind the current run for the approval gate (DB-IPC; tools.dispatch reads this).
    os.environ["STACKWEFT_RUN_ID"] = ctx.run_id
    if resume:
        for st in STAGES:
            row = obs.load_stage(ctx.run_id, st)
            if row and row["status"] == "done" and row["output_json"]:
                ctx.outputs[st] = json.loads(row["output_json"])

    # Resume-proof guard: a NON-requirement (greeting / chit-chat / assist question) must
    # never be pushed down the delivery pipeline — not even if the user forces a
    # resume/continue after the clarify pause. (Resume normally skips the clarify stage, so
    # the in-loop not_a_requirement check below wouldn't re-fire.) `redo_from="clarify"`
    # (the clarify-answer flow, which folds in a real requirement) is exempt.
    _csp = (ctx.outputs.get("clarify") or {}).get("spec", {})
    if _csp.get("is_requirement") is False and redo_from != "clarify":
        obs.update_run(ctx.run_id, status="paused", stage="clarify")
        reply = _csp.get("reply") or "这还不是一个具体的改动需求——说说你想做什么，我来接。"
        return {"ok": True, "needs_requirement": True, "chat_reply": reply,
                "cost": obs.run_cost_summary(ctx.run_id), "outputs": ctx.outputs}

    start_idx = STAGES.index(redo_from) if redo_from else 0
    if redo_from:
        for st in STAGES[start_idx:]:
            ctx.outputs.pop(st, None)

    # Lint baseline (before edits) for the differential gate.
    if "_lint_baseline" not in ctx.outputs:
        row = obs.load_stage(ctx.run_id, "_lint_baseline")
        if row and row["output_json"]:
            ctx.outputs["_lint_baseline"] = json.loads(row["output_json"]).get("count")
        else:
            count = _eslint_error_count(ctx.sandbox)
            ctx.outputs["_lint_baseline"] = count
            obs.save_stage(ctx.run_id, "_lint_baseline", status="done",
                           output_obj={"count": count})
            obs.log_event(ctx.run_id, stage="setup", event="lint_baseline",
                          payload={"count": count})

    # Filesystem snapshot (before edits) for git-independent change detection.
    if "_fs_baseline" not in ctx.outputs:
        row = obs.load_stage(ctx.run_id, "_fs_baseline")
        if row and row["output_json"]:
            ctx.outputs["_fs_baseline"] = json.loads(row["output_json"]).get("snap", {})
        else:
            snap = _fs_snapshot(ctx.sandbox)
            ctx.outputs["_fs_baseline"] = snap
            obs.save_stage(ctx.run_id, "_fs_baseline", status="done",
                           output_obj={"snap": snap})
            obs.log_event(ctx.run_id, stage="setup", event="fs_baseline",
                          payload={"files": len(snap)})

    cap = int(os.environ.get("STACKWEFT_RUN_TOKEN_CAP", "120000"))

    for st in STAGES[start_idx:]:
        if resume and st in ctx.outputs and st != redo_from:
            continue
        # runtime intervention (between stages): pause / abort / append.
        ctl = obs.get_control(ctx.run_id)
        if ctl:
            obs.clear_control(ctx.run_id)
            if ctl["action"] in ("pause", "abort"):
                obs.update_run(ctx.run_id, status="paused", stage=st)
                obs.log_event(ctx.run_id, stage=st, event=f"control_{ctl['action']}", payload={})
                return {"ok": True, "control": ctl["action"], "stage": st,
                        "cost": obs.run_cost_summary(ctx.run_id), "outputs": ctx.outputs}
            if ctl["action"] == "append" and ctl.get("text"):
                ctx.requirement = f"{ctx.requirement}\n\n[追加需求] {ctl['text']}"
                obs.update_run(ctx.run_id, requirement=ctx.requirement)
                obs.log_event(ctx.run_id, stage=st, event="control_append",
                              payload={"text": ctl["text"][:200]})
                for s2 in STAGES:  # re-clarify with the appended requirement
                    ctx.outputs.pop(s2, None)
                return run(ctx, resume=False, redo_from="clarify", stop_after=stop_after,
                           verify_repair_rounds=verify_repair_rounds, ask=ask)
        spent = obs.run_cost_summary(ctx.run_id)["tokens_in"]
        if spent > cap:
            obs.log_event(ctx.run_id, stage=st, event="token_cap_abort",
                          payload={"spent": spent, "cap": cap})
            obs.update_run(ctx.run_id, status="paused", stage=st)
            return {"ok": False, "aborted": "token_cap", "spent_tokens_in": spent,
                    "cap": cap, "outputs": ctx.outputs}
        obs.update_run(ctx.run_id, stage=st, status="running")
        obs.log_event(ctx.run_id, stage=st, event="stage_start")
        obs.save_stage(ctx.run_id, st, status="pending",
                       input_obj={"requirement": ctx.requirement})
        try:
            out = (_verify_with_repair(ctx, verify_repair_rounds)
                   if st == "verify" else _STAGE_FNS[st](ctx))
            ctx.outputs[st] = out
            obs.save_stage(ctx.run_id, st, status="done", output_obj=out)
            obs.log_event(ctx.run_id, stage=st, event="stage_done",
                          payload=_event_summary(st, out))
        except Exception as e:  # noqa: BLE001
            obs.save_stage(ctx.run_id, st, status="failed", output_obj={"error": repr(e)})
            obs.log_event(ctx.run_id, stage=st, event="stage_fail", payload={"error": repr(e)})
            obs.update_run(ctx.run_id, status="failed", stage=st)
            return {"ok": False, "failed_stage": st, "error": repr(e), "outputs": ctx.outputs}
        # Interactive clarify: in ask mode, pause after clarify when it
        # raised open questions the PM hasn't answered. The cockpit surfaces them and
        # `clarify-answer` resumes. No questions (or already answered) → flow continues.
        if st == "clarify":
            spec = (out or {}).get("spec", {})
            # Rename the branch as soon as the field is known (clarify derives it), so even
            # the awaiting-clarify pause shows a meaningful 'add-<field>-<id>' branch.
            _fn = str(spec.get("field_name", "") or "").strip()
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,40}", _fn):
                _rename_branch(ctx, _fn)
            # Not a delivery requirement → don't push junk down the pipeline. If it's a
            # question/exploration, the read-only general assistant actually helps (browse
            # repo / answer); pure greeting → friendly reply. Either way pause for a real
            # requirement. Core delivery claim is untouched.
            if spec.get("is_requirement") is False:
                reply = spec.get("reply") or "请描述你想做的具体改动。"
                if spec.get("kind") == "assist":
                    ans = assist(ctx)
                    if ans:
                        reply = ans
                obs.update_run(ctx.run_id, status="paused", stage="clarify")
                obs.log_event(ctx.run_id, stage="clarify", event="not_a_requirement",
                              payload={"kind": spec.get("kind"), "reply": reply[:300]})
                return {"ok": True, "needs_requirement": True,
                        "chat_reply": reply,
                        "cost": obs.run_cost_summary(ctx.run_id), "outputs": ctx.outputs}
            # Feasibility gate (delivery only): reject the impossible, pause-to-confirm the
            # costly. We DON'T blindly run a high-cost / unworkable ask. `infeasible` stops
            # with a reason; `needs_confirm` pauses — and since clarify is already saved
            # 'done', a plain `resume` skips clarify and proceeds: resuming IS the confirm.
            feas = str(spec.get("feasibility") or "ok").lower()
            reason = (spec.get("feasibility_reason") or "").strip()
            if feas == "infeasible":
                reply = spec.get("reply") or (
                    "这个需求我没法在当前仓库里作为一次跨栈交付完成"
                    + (f"：{reason}" if reason else "。") + " 可以换个能落地的说法再试。")
                obs.update_run(ctx.run_id, status="rejected", stage="clarify")
                obs.log_event(ctx.run_id, stage="clarify", event="requirement_rejected",
                              payload={"reason": reason[:400], "reply": reply[:300]})
                return {"ok": True, "rejected": True, "reason": reason, "chat_reply": reply,
                        "cost": obs.run_cost_summary(ctx.run_id), "outputs": ctx.outputs}
            if feas == "needs_confirm":
                reply = spec.get("reply") or (
                    "这个需求能做，但代价不小（" + (reason or "改动面较大")
                    + "）。确认就继续（resume / 点确认），或把范围缩小再说。")
                obs.update_run(ctx.run_id, status="paused", stage="clarify")
                obs.log_event(ctx.run_id, stage="clarify", event="awaiting_confirm",
                              payload={"reason": reason[:400], "reply": reply[:300]})
                return {"ok": True, "awaiting_confirm": True, "confirm_reason": reason,
                        "chat_reply": reply,
                        "cost": obs.run_cost_summary(ctx.run_id), "outputs": ctx.outputs}
        if ask and st == "clarify":
            spec = (out or {}).get("spec", {})
            # multi-round: pause to ask, UNLESS we've already gone >=3 rounds
            # (cap → converge and proceed so it can't loop forever).
            rounds = 0
            try:
                with obs.connect() as _c:
                    rounds = _c.execute("SELECT COUNT(*) FROM workflow_events WHERE run_id=? "
                                        "AND event='clarify_answered'", (ctx.run_id,)).fetchone()[0]
            except Exception:  # noqa: BLE001
                pass
            if spec.get("open_questions") and rounds < 3:
                obs.update_run(ctx.run_id, status="paused", stage="clarify")
                obs.log_event(ctx.run_id, stage="clarify", event="awaiting_clarify",
                              payload={"open_questions": spec.get("open_questions"), "round": rounds + 1})
                return {"ok": True, "awaiting_clarify": True,
                        "open_questions": spec.get("open_questions"),
                        "cost": obs.run_cost_summary(ctx.run_id), "outputs": ctx.outputs}
        if stop_after and st == stop_after:
            obs.update_run(ctx.run_id, status="paused", stage=st)
            return {"ok": True, "stopped_after": st, "cost": obs.run_cost_summary(ctx.run_id),
                    "outputs": ctx.outputs}

    obs.update_run(ctx.run_id, status="done", stage="pr")
    # Skill autonomy: after a delivery, consider drafting a Skill for a recurring
    # UNCOVERED requirement type. Never writes silently — it queues a PENDING proposal
    # the user approves/vetoes. Only fires on recurrence (skill_candidates threshold).
    if os.environ.get("STACKWEFT_SKILL_AUTO", "1") == "1":
        try:
            from stackweft.platform import skillsmith
            proposals = skillsmith.auto_consider()
            if proposals:
                obs.log_event(ctx.run_id, stage="pr", event="skill_proposed",
                              payload={"proposals": proposals})
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "cost": obs.run_cost_summary(ctx.run_id), "outputs": ctx.outputs}


def _reviewer_check(ctx: RunContext) -> dict[str, Any]:
    """An INDEPENDENT reviewer (fresh context) judges whether the change really
    satisfies the requirement, beyond the deterministic gates. Returns {approved,
    reasons}. Gated by STACKWEFT_REVIEWER so we can A/B it."""
    spec = ctx.outputs.get("clarify", {}).get("spec", {})
    diff = ctx.sandbox.run_shell("git diff --stat; echo '----'; git diff --unified=2 | head -200")
    system = (f"You are an INDEPENDENT code reviewer (separate from the author). Judge "
              f"ONLY whether the diff correctly and completely satisfies the requirement. "
              f"Be strict but fair. Reply in {config.lang()} for reasons. Return STRICT "
              'JSON: {"approved": bool, "reasons": [str], "back_to": "generate"|"plan"|null}.')
    user = (f"Requirement summary: {spec.get('summary')}\nAcceptance: {spec.get('acceptance')}\n"
            f"Behaviour: {spec.get('behaviour')}\n\nDIFF:\n{diff[:3500]}\n\nJSON only.")
    res = llm.messages(system=system, msgs=[{"role": "user", "content": user}],
                       level=0.8, run_id=ctx.run_id, stage="verify",
                       purpose="reviewer", max_tokens=900)
    j = _extract_json(res.text)
    approved = bool(j.get("approved", True))
    obs.log_event(ctx.run_id, stage="verify", event="reviewer",
                  payload={"approved": approved, "reasons": (j.get("reasons") or [])[:5],
                           "back_to": j.get("back_to")})
    return {"approved": approved, "reasons": j.get("reasons") or [], "back_to": j.get("back_to")}


def _maybe_reviewer(ctx: RunContext, out: dict[str, Any]) -> dict[str, Any]:
    """If reviewer enabled and the deterministic gates passed, run the reviewer; a
    rejection injects a failed 'reviewer' check so the repair loop bounces back."""
    if os.environ.get("STACKWEFT_REVIEWER", "0") != "1" or not out.get("passed"):
        return out
    rv = _reviewer_check(ctx)
    out.setdefault("checks", []).append(
        {"name": "reviewer", "passed": rv["approved"],
         "reason": ("; ".join(rv["reasons"])[:400] or "reviewer rejected") if not rv["approved"] else "approved"})
    if not rv["approved"]:
        out["passed"] = False
        out["reviewer_back_to"] = rv.get("back_to") or "generate"
    return out


def _verify_with_repair(ctx: RunContext, rounds: int) -> dict[str, Any]:
    out = _maybe_reviewer(ctx, stage_verify(ctx))
    attempt = 0
    cap = int(os.environ.get("STACKWEFT_RUN_TOKEN_CAP", "120000"))
    while not out["passed"] and attempt < rounds:
        spent = obs.run_cost_summary(ctx.run_id)["tokens_in"]
        if spent > cap:
            obs.log_event(ctx.run_id, stage="verify", event="token_cap_abort",
                          payload={"spent": spent, "cap": cap, "attempt": attempt})
            out["aborted"] = "token_cap"
            break
        attempt += 1
        feedback = "\n".join(f"[{c['name']}] FAIL\n{c.get('reason') or c.get('out')}"
                             for c in out["checks"] if not c["passed"])
        obs.log_event(ctx.run_id, stage="verify", event="repair_attempt",
                      payload={"attempt": attempt})
        ctx.outputs["generate"] = stage_generate(ctx, repair_feedback=feedback,
                                                 max_rounds=8)
        obs.save_stage(ctx.run_id, "generate", status="done",
                       output_obj=ctx.outputs["generate"])
        out = _maybe_reviewer(ctx, stage_verify(ctx))
    out["repair_attempts"] = attempt
    return out


# JSON extraction + repair live in utils.py (shared); aliased here for call sites.
from stackweft.core.utils import json_extract as _extract_json  # noqa: E402


def _event_summary(stage: str, out: dict[str, Any]) -> dict[str, Any]:
    if stage == "verify":
        return {"passed": out.get("passed"), "repair_attempts": out.get("repair_attempts")}
    if stage == "generate":
        return {"rounds": out.get("rounds"), "tool_calls": out.get("tool_calls"),
                "two_phase": out.get("two_phase"), "slotwise": out.get("slotwise"),
                "ok": out.get("ok")}
    if stage == "compile":
        return {"engaged": out.get("engaged"), "field": out.get("field"),
                "errors": out.get("errors"), "new_files": len(out.get("new_files", []))}
    return {"keys": list(out.keys())}
