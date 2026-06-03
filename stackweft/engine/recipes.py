"""Patch recipe memory — make same-repo/same-skill changes faster.

A *recipe* is NOT a raw diff. It is a field-PARAMETERIZED transform (the concrete
field name/type are replaced by {field}/{type}) plus a SURFACE HASH guard (a hash
of the slot's file path + the code window around its anchor + the shadow field).
On a later run we replay a recipe ONLY when the surface hash still matches; we
apply it, then run the SAME slot-evidence check, and roll back + fall back to the
LLM if anything is off. Memory reduces "how to change", never "is it correct".

Stdlib + the shared obs SQLite DB.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any

from stackweft.core import obs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS patch_recipe (
  id            TEXT PRIMARY KEY,
  skill         TEXT NOT NULL,
  slot          TEXT NOT NULL,
  repo_id       TEXT NOT NULL,
  surface_hash  TEXT NOT NULL,
  shadow_field  TEXT,
  edits_template_json TEXT NOT NULL,
  stats_json    TEXT,
  ts_created    INTEGER,
  ts_updated    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_recipe_lookup
  ON patch_recipe(skill, slot, repo_id, surface_hash);
"""


def _ensure(conn) -> None:
    conn.executescript(_SCHEMA)


def surface_hash(rel_path: str, window: str, shadow: str, kind: str = "") -> str:
    # ``kind`` (image vs text display) segregates recipes: the anchor window is the
    # same shadow line for any field, so without it a text field would replay an
    # image-shaped patch (and vice versa). Same kind → same recipe → warm replay.
    h = hashlib.sha1()
    h.update(rel_path.encode()); h.update(b"\n")
    h.update(window.encode()); h.update(b"\n")
    h.update((shadow or "").encode()); h.update(b"\n")
    h.update((kind or "").encode())
    return h.hexdigest()[:16]


def _abstract(text: str, field: str, type_: str) -> str:
    """Replace the concrete field/type tokens with placeholders so the recipe
    applies to any field. field first (longer, more specific), then type."""
    out = text.replace(field, "{field}")
    if type_:
        out = out.replace(f"DataTypes.{type_}", "DataTypes.{type}")
    return out


def _concretize(text: str, field: str, type_: str) -> str:
    return text.replace("{field}", field).replace("{type}", type_)


def edits_from_diff(before: str, after: str, *, max_hunks: int = 6) -> list[dict[str, str]]:
    """Extract minimal context-anchored {old,new} edits from a before/after pair
    (used to capture recipes from the agentic worker, which doesn't return edits).
    Insertions are anchored on the preceding line so `old` is non-empty + unique."""
    b, a = before.splitlines(keepends=True), after.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(a=b, b=a, autojunk=False)
    edits: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            anchor = b[i1 - 1] if i1 > 0 else ""
            old = anchor
            new = anchor + "".join(a[j1:j2])
        else:  # replace / delete
            old = "".join(b[i1:i2])
            new = "".join(a[j1:j2])
        if old:
            edits.append({"old": old, "new": new})
        if len(edits) >= max_hunks:
            break
    return edits


def record(*, skill: str, slot: str, repo_id: str, surface_hash: str,
           shadow: str, field: str, type_: str, edits: list[dict[str, Any]]) -> None:
    """Store a field-parameterized recipe (idempotent on the lookup key)."""
    tmpl = [{"old": _abstract(e["old"], field, type_),
             "new": _abstract(e["new"], field, type_)} for e in edits if e.get("old")]
    if not tmpl:
        return
    ts = obs.now_ms() if hasattr(obs, "now_ms") else 0
    with obs.connect() as conn:
        _ensure(conn)
        row = conn.execute("SELECT id,stats_json FROM patch_recipe WHERE skill=? AND slot=? "
                           "AND repo_id=? AND surface_hash=?",
                           (skill, slot, repo_id, surface_hash)).fetchone()
        stats = {"hits": 0, "records": 1}
        if row:
            try:
                old_stats = json.loads(row["stats_json"] or "{}")
                stats["records"] = old_stats.get("records", 0) + 1
                stats["hits"] = old_stats.get("hits", 0)
            except Exception:
                pass
            conn.execute("UPDATE patch_recipe SET edits_template_json=?, stats_json=?, ts_updated=? WHERE id=?",
                         (json.dumps(tmpl), json.dumps(stats), ts, row["id"]))
        else:
            conn.execute("INSERT INTO patch_recipe(id,skill,slot,repo_id,surface_hash,shadow_field,"
                         "edits_template_json,stats_json,ts_created,ts_updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (obs.new_id(), skill, slot, repo_id, surface_hash, shadow,
                          json.dumps(tmpl), json.dumps(stats), ts, ts))


def lookup(*, skill: str, slot: str, repo_id: str, surface_hash: str,
           field: str, type_: str) -> list[dict[str, str]] | None:
    """Return concrete edits for replay, or None if no guarded recipe matches."""
    with obs.connect() as conn:
        _ensure(conn)
        row = conn.execute("SELECT id,edits_template_json,stats_json FROM patch_recipe "
                           "WHERE skill=? AND slot=? AND repo_id=? AND surface_hash=?",
                           (skill, slot, repo_id, surface_hash)).fetchone()
        if not row:
            return None
        try:
            stats = json.loads(row["stats_json"] or "{}")
            stats["hits"] = stats.get("hits", 0) + 1
            conn.execute("UPDATE patch_recipe SET stats_json=? WHERE id=?",
                         (json.dumps(stats), row["id"]))
        except Exception:
            pass
        tmpl = json.loads(row["edits_template_json"])
    return [{"old": _concretize(e["old"], field, type_),
             "new": _concretize(e["new"], field, type_)} for e in tmpl]
