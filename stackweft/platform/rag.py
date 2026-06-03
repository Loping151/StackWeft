"""Requirement-memory RAG: precipitate past requirements, retrieve similar ones.

Stores one distilled record per delivered requirement and retrieves the most
similar ones to inject into clarify/plan/localize — cheaply (summary + skill +
targets + pitfalls only, never raw transcripts).

Embeddings: if the gateway exposes ``/v1/embeddings`` we use it (with a
content-hash cache so we never pay twice); if not, we degrade to a tiny BM25-ish
keyword score. At single-user scale brute-force cosine in pure Python is plenty —
no chromadb/sqlite-vec dependency. Storage piggybacks on the obs SQLite DB.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from stackweft.core import config, obs

EMBED_MODEL = "text-embedding-3-small"

_RAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS req_memory (
    id TEXT PRIMARY KEY, ts INTEGER NOT NULL, requirement_raw TEXT, skill TEXT,
    summary TEXT, layers TEXT, targets TEXT, test_added TEXT, outcome TEXT,
    pitfalls TEXT, diff_stat TEXT, embed_text TEXT, embed_vec TEXT, repo_id TEXT
);
CREATE TABLE IF NOT EXISTS embed_cache (
    text_hash TEXT PRIMARY KEY, model TEXT, vec TEXT, ts INTEGER
);
"""


def _ensure() -> None:
    with obs.connect() as conn:
        conn.executescript(_RAG_SCHEMA)
        # idempotent migration: add repo_id to a pre-existing table (memories are
        # repo-scoped so a pitfall from repo A never bleeds into repo B).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(req_memory)")}
        if "repo_id" not in cols:
            conn.execute("ALTER TABLE req_memory ADD COLUMN repo_id TEXT")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed(text: str, *, run_id: str | None = None) -> list[float] | None:
    _ensure()
    h = _hash(EMBED_MODEL + "\n" + text)
    with obs.connect() as conn:
        row = conn.execute("SELECT vec FROM embed_cache WHERE text_hash=?", (h,)).fetchone()
        if row:
            return json.loads(row["vec"])
    if not config.REGISTRY:
        return None
    spec = config.REGISTRY[0]
    body = json.dumps({"model": EMBED_MODEL, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{spec.base_url}/v1/embeddings", data=body,
        headers={"Authorization": f"Bearer {spec.api_key}", "content-type": "application/json"},
        method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            vec = json.loads(resp.read().decode("utf-8"))["data"][0]["embedding"]
    except Exception as e:  # noqa: BLE001
        obs.record_llm_call(run_id=run_id, stage="rag", purpose="embed",
                            model_id="embed", api_model=EMBED_MODEL, tokens_in=None,
                            tokens_out=None, cost_usd=None,
                            latency_ms=int((time.time() - started) * 1000),
                            status="error", error=repr(e))
        return None
    with obs.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO embed_cache (text_hash,model,vec,ts) "
                     "VALUES (?,?,?,?)", (h, EMBED_MODEL, json.dumps(vec), obs.now_ms()))
    obs.record_llm_call(run_id=run_id, stage="rag", purpose="embed", model_id="embed",
                        api_model=EMBED_MODEL, tokens_in=None, tokens_out=None,
                        cost_usd=None, latency_ms=int((time.time() - started) * 1000),
                        status="ok")
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _tok(text: str) -> list[str]:
    import re
    return re.findall(r"[a-zA-Z0-9_]+", text.lower()) + re.findall(r"[一-鿿]", text)


def _bm25(q: list[str], doc: list[str]) -> float:
    if not doc:
        return 0.0
    from collections import Counter
    dc = Counter(doc)
    return sum(dc.get(t, 0) for t in set(q)) / (1 + math.log(1 + len(doc)))


@dataclass
class MemoryHit:
    id: str
    score: float
    summary: str
    skill: str
    targets: list[str]
    pitfalls: list[str]


def remember(*, run_id: str, requirement_raw: str, skill: str, spec: dict[str, Any],
             targets: list[str], test_added: str, outcome: str,
             pitfalls: list[str], diff_stat: str, repo_id: str = "") -> None:
    _ensure()
    summary = str(spec.get("summary", requirement_raw))[:500]
    embed_text = " | ".join([summary, str(spec.get("behaviour", ""))[:300], skill,
                             " ".join(targets)])
    vec = embed(embed_text, run_id=run_id)
    with obs.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO req_memory (id,ts,requirement_raw,skill,summary,"
            "layers,targets,test_added,outcome,pitfalls,diff_stat,embed_text,embed_vec,repo_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, obs.now_ms(), requirement_raw, skill, summary,
             json.dumps(spec.get("layers", []), ensure_ascii=False),
             json.dumps(targets, ensure_ascii=False), test_added, outcome,
             json.dumps(pitfalls, ensure_ascii=False), diff_stat, embed_text,
             json.dumps(vec) if vec else None, repo_id or None))


_GENERIC_SKILLS = {"generic-fullstack-change", "", None}


def _shape(row) -> str:
    """Generalized requirement TYPE signature — strips instance specifics (field
    name etc.), keyed by the matched skill + the layer set. Two 'add a URL field'
    requests collapse to ONE shape, so recurrence is judged at the TYPE level."""
    try:
        layers = "+".join(sorted(json.loads(row["layers"] or "[]")))
    except Exception:
        layers = ""
    return f"{row['skill'] or 'generic'}::{layers or '?'}"


def skill_candidates(min_count: int = 2) -> list[dict[str, Any]]:
    """Decide where authoring a (GENERAL) Skill is worth it — NOT to record a done
    task. A candidate = a requirement TYPE that (a) recurred >= min_count times and
    (b) was handled by the GENERIC fallback (i.e. no specialized skill yet). Types
    already covered by a specific skill are skipped (the skill exists). The author
    should generalize (parameterize entity/field, shadow-clone style), not transcribe
    one instance."""
    _ensure()
    with obs.connect() as conn:
        rows = conn.execute("SELECT skill,layers,summary,requirement_raw FROM req_memory").fetchall()
    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[_shape(r)].append(r)
    out = []
    for shape, items in groups.items():
        skill = shape.split("::")[0]
        covered = skill not in {"generic", *_GENERIC_SKILLS}
        if covered or len(items) < min_count:
            continue  # specific skill already exists, or one-off → no new skill
        out.append({
            "shape": shape, "count": len(items), "covered": covered,
            "examples": [(r["summary"] or r["requirement_raw"] or "")[:60] for r in items[:4]],
            "suggestion": (f"已出现 {len(items)} 次同类型(走通用回退)需求 → 值得写一个 GENERAL Skill："
                           f"参数化实体/字段(影子克隆式)、声明槽位+验证器，覆盖该 TYPE 而非单个实例。"),
        })
    return sorted(out, key=lambda c: -c["count"])


def retrieve(query: str, *, k: int = 3, run_id: str | None = None,
             repo_id: str | None = None) -> list[MemoryHit]:
    _ensure()
    with obs.connect() as conn:
        if repo_id:  # repo-scoped: never surface another repo's memories
            rows = conn.execute("SELECT * FROM req_memory WHERE repo_id=?", (repo_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM req_memory").fetchall()
    if not rows:
        return []
    qvec = embed(query, run_id=run_id)
    qtok = _tok(query)
    scored = []
    for r in rows:
        if qvec and r["embed_vec"]:
            s = _cosine(qvec, json.loads(r["embed_vec"]))
        else:
            s = _bm25(qtok, _tok(r["embed_text"] or r["summary"] or ""))
        scored.append((s, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [MemoryHit(id=r["id"], score=round(s, 4), summary=r["summary"] or "",
                      skill=r["skill"] or "", targets=json.loads(r["targets"] or "[]"),
                      pitfalls=json.loads(r["pitfalls"] or "[]"))
            for s, r in scored[:k]]


def render_for_prompt(hits: list[MemoryHit]) -> str:
    if not hits:
        return ""
    lines = ["## Similar past requirements (for grounding; may differ):"]
    for h in hits:
        lines.append(f"- [{h.skill}] {h.summary}")
        if h.targets:
            lines.append(f"    files: {', '.join(h.targets[:6])}")
        if h.pitfalls:
            lines.append(f"    pitfalls: {'; '.join(h.pitfalls[:3])}")
    return "\n".join(lines)
