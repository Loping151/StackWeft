"""Skill autonomy (closed loop) — AI drafts / updates Skills, with built-in
versioning, human approve/veto, and one-click rollback.

Design:
* Every applied Skill change snapshots the FULL .md into ``skill_version`` (the
  rollback target) with author (ai|human) + reason + timestamp.
* AI never writes a Skill silently: it ``propose()``s a change onto a pending
  queue (``skill_change``); the UI surfaces it and the user approve/veto/later.
* A user asking "make me a skill for X" goes through the SAME draft→propose flow.
* Generality guard: drafting targets a requirement *type*, not a one-off; the
  candidate detector (rag.skill_candidates) only fires on recurrence.

Honesty discipline: versions/changes are real DB rows; the changelog reads them.
Stdlib + the shared obs DB + one LLM call to draft.
"""

from __future__ import annotations

from pathlib import Path

from stackweft.core import obs
from stackweft.engine import skills

SKILLS_DIR = skills.SKILLS_DIR


def _path(name: str) -> Path:
    return SKILLS_DIR / f"{name}.md"


def current_content(name: str) -> str | None:
    p = _path(name)
    return p.read_text(encoding="utf-8") if p.is_file() else None


def _valid_skill(content: str) -> tuple[bool, str]:
    """A drafted Skill must parse as frontmatter + (for field-flow skills) a slot
    spec. We don't require slots (some skills are guidance-only), but if a ```json
    block is present it must be valid + have a name."""
    try:
        from stackweft.engine import fieldflow
        meta = fieldflow.skill_meta(content)
        if not meta.get("name"):
            return False, "frontmatter missing name"
        if "```json" in content:
            spec = fieldflow.load_skill_spec(content)  # raises on bad JSON
            if not isinstance(spec.get("slots"), list):
                return False, "slot spec has no slots[]"
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"invalid skill: {e!r}"


# ── versioning ────────────────────────────────────────────────────────────────

def record_version(name: str, content: str, *, author: str, reason: str) -> int:
    with obs.connect() as conn:
        v = (conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM skill_version WHERE name=?",
                          (name,)).fetchone()[0])
        conn.execute("INSERT INTO skill_version(id,name,version,content,author,reason,ts) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (obs.new_id(), name, v, content, author, reason, obs.now_ms()))
    return v


def history(name: str) -> list[dict]:
    with obs.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT version,author,reason,ts,length(content) AS size FROM skill_version "
            "WHERE name=? ORDER BY version DESC", (name,))]


def version_content(name: str, version: int) -> str | None:
    with obs.connect() as conn:
        r = conn.execute("SELECT content FROM skill_version WHERE name=? AND version=?",
                         (name, version)).fetchone()
        return r["content"] if r else None


def changelog(limit: int = 40) -> list[dict]:
    with obs.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT name,version,author,reason,ts FROM skill_version ORDER BY ts DESC LIMIT ?",
            (limit,))]


def _write_and_snapshot(name: str, content: str, *, author: str, reason: str) -> int:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    _path(name).write_text(content, encoding="utf-8")
    return record_version(name, content, author=author, reason=reason)


def rollback(name: str, version: int) -> dict:
    """Restore a historical version's content (recorded as a NEW version so the
    timeline stays append-only and the rollback itself is traceable)."""
    content = version_content(name, version)
    if content is None:
        return {"ok": False, "error": f"no version {version} for {name}"}
    v = _write_and_snapshot(name, content, author="human", reason=f"rollback to v{version}")
    return {"ok": True, "name": name, "restored_from": version, "new_version": v}


# ── pending change queue (AI/human proposals) ─────────────────────────────────

def propose(name: str, op: str, content: str, *, reason: str, author: str = "ai") -> dict:
    ok, why = _valid_skill(content)
    if not ok:
        return {"ok": False, "error": why}
    cid = obs.new_id()
    with obs.connect() as conn:
        # collapse an existing pending proposal for the same skill (latest wins)
        conn.execute("UPDATE skill_change SET status='superseded' WHERE name=? AND status='pending'",
                     (name,))
        conn.execute("INSERT INTO skill_change(id,name,op,content,reason,author,status,ts) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (cid, name, op, content, reason, author, "pending", obs.now_ms()))
    return {"ok": True, "change_id": cid, "name": name, "op": op}


def pending_changes() -> list[dict]:
    with obs.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id,name,op,reason,author,ts,length(content) AS size FROM skill_change "
            "WHERE status='pending' ORDER BY ts DESC")]


def get_change(change_id: str) -> dict | None:
    with obs.connect() as conn:
        r = conn.execute("SELECT * FROM skill_change WHERE id=?", (change_id,)).fetchone()
        return dict(r) if r else None


def approve(change_id: str) -> dict:
    c = get_change(change_id)
    if not c or c["status"] != "pending":
        return {"ok": False, "error": "no such pending change"}
    v = _write_and_snapshot(c["name"], c["content"], author=c["author"],
                            reason=c["reason"] or f"{c['op']} (approved)")
    with obs.connect() as conn:
        conn.execute("UPDATE skill_change SET status='approved', ts=? WHERE id=?",
                     (obs.now_ms(), change_id))
    return {"ok": True, "name": c["name"], "op": c["op"], "version": v}


def veto(change_id: str) -> dict:
    with obs.connect() as conn:
        n = conn.execute("UPDATE skill_change SET status='vetoed', ts=? WHERE id=? AND status='pending'",
                         (obs.now_ms(), change_id)).rowcount
    return {"ok": bool(n)}


# ── drafting (the one LLM call) ───────────────────────────────────────────────

_DRAFT_SYS = (
    "You author a reusable StackWeft Skill as a single Markdown file. A Skill captures "
    "a requirement TYPE (not one instance) so the engine can deliver it across a stack. "
    "Output ONLY the .md content. Structure:\n"
    "1) YAML-ish frontmatter between --- lines: name (kebab-case), intent, description, "
    "match: [keywords], priority, and for field-add skills: shadow_field, entity, "
    "entity_aliases, default_type.\n"
    "2) Optionally a ```json block: {\"intent\":..., \"slots\":[{slot,layer,kind,glob,"
    "shadow_anchor,evidence,min_count,instruction|template,...}], \"hard_gates\":[...]}.\n"
    "3) Guidance prose with ## sections (e.g. ## clarify, ## plan).\n"
    "Keep it GENERAL (parameterize entity/field). Mirror the shape of an existing skill. "
    "No real product/company names in comments. Reply with the markdown only, no fences "
    "around the whole thing."
)


def draft_skill(brief: str, *, existing: str | None = None, example: str | None = None) -> str | None:
    """Draft (or revise) a Skill .md via one LLM call. Returns content or None."""
    try:
        from stackweft.core import llm
    except Exception:  # noqa: BLE001
        return None
    if example is None:
        ex = _path("add-article-field")
        example = ex.read_text(encoding="utf-8") if ex.is_file() else ""
    parts = [f"TASK: {'revise the existing Skill' if existing else 'author a new Skill'} for "
             f"this requirement type:\n{brief}\n"]
    if example:
        parts.append(f"\nFORMAT REFERENCE (an existing skill — mirror its structure, do NOT copy "
                     f"its domain):\n{example[:3500]}")
    if existing:
        parts.append(f"\nCURRENT VERSION TO REVISE:\n{existing[:3500]}")
    parts.append("\nReturn the complete .md content now.")
    user = "".join(parts)
    # Draft on the EXECUTION tier (kimi). The reasoning tier (GLM) spends its output
    # budget on hidden thinking and returns empty text (stop_reason=max_tokens), so it's
    # wrong for generation. Retry a few times since the gateway can blip empty.
    for _ in range(4):
        try:
            res = llm.messages(system=_DRAFT_SYS, msgs=[{"role": "user", "content": user}],
                               level=0.6, stage="skillsmith", purpose="draft_skill", max_tokens=2600)
        except Exception:  # noqa: BLE001
            continue
        content = (res.text or "").strip()
        if content.startswith("```"):  # strip an accidental wrapping fence
            content = content.split("\n", 1)[-1]
            if content.rstrip().endswith("```"):
                content = content.rstrip()[:-3]
        content = content.strip()
        if content and "---" in content:  # got a real draft
            return content
    return None


def _slug(brief: str) -> str:
    import re
    m = re.search(r"[a-z][a-z0-9-]{2,}", brief.lower())
    return (m.group(0) if m else "skill") + "-skill"


def request(brief: str, *, author: str = "ai") -> dict:
    """User (or AI) asks for a new Skill → draft → propose (pending approval)."""
    content = draft_skill(brief)
    if not content:
        return {"ok": False, "error": "draft failed (LLM)"}
    try:
        from stackweft.engine import fieldflow
        name = fieldflow.skill_meta(content).get("name") or _slug(brief)
    except Exception:  # noqa: BLE001
        name = _slug(brief)
    op = "update" if _path(name).is_file() else "create"
    return propose(name, op, content, reason=f"user requested: {brief[:120]}", author=author)


def auto_consider(min_count: int = 2) -> list[dict]:
    """Auto path: turn recurring uncovered requirement TYPES (rag.skill_candidates)
    into drafted, PENDING Skill proposals. Never writes silently. Skips a type that
    already has a pending proposal. Returns the proposals created."""
    try:
        from stackweft.platform import rag
    except Exception:  # noqa: BLE001
        return []
    cands = rag.skill_candidates(min_count=min_count)
    if not cands:
        return []
    existing_pending = {c["name"] for c in pending_changes()}
    out = []
    for cand in cands[:2]:  # cap per pass to bound LLM cost
        brief = (f"A requirement type that recurred {cand['count']} times via the generic "
                 f"fallback (no specialized skill yet). Examples: {cand.get('examples')}. "
                 f"Shape signature: {cand.get('shape')}.")
        content = draft_skill(brief)
        if not content:
            continue
        try:
            from stackweft.engine import fieldflow
            name = fieldflow.skill_meta(content).get("name") or _slug(brief)
        except Exception:  # noqa: BLE001
            name = _slug(brief)
        if name in existing_pending:
            continue
        r = propose(name, "update" if _path(name).is_file() else "create", content,
                    reason=f"auto: recurring type ×{cand['count']}", author="ai")
        if r.get("ok"):
            out.append({"name": name, "change_id": r["change_id"], "count": cand["count"]})
    return out
