"""Observability + persistence (SQLite, stdlib only).

Two jobs:
* Per-AI-call accounting: model, tokens, latency, cost, status.
* Workflow event log + stage checkpoints: every run/stage persisted so a run can
  be paused / inspected / resumed / redone from any stage.

One DB at ``$STACKWEFT_HOME/data/stackweft.db``.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from stackweft.core.config import STACKWEFT_HOME

DB_PATH = STACKWEFT_HOME / "data" / "stackweft.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY, ts INTEGER NOT NULL, run_id TEXT, stage TEXT,
    purpose TEXT, model_id TEXT, api_model TEXT, tokens_in INTEGER,
    tokens_out INTEGER, cost_usd REAL, latency_ms INTEGER, status TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY, ts_created INTEGER NOT NULL, ts_updated INTEGER NOT NULL,
    title TEXT, requirement TEXT, status TEXT, stage TEXT, repo_path TEXT,
    branch TEXT, meta_json TEXT
);

CREATE TABLE IF NOT EXISTS workflow_events (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ts INTEGER NOT NULL, stage TEXT,
    event TEXT, payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON workflow_events(run_id);

CREATE TABLE IF NOT EXISTS stage_state (
    run_id TEXT NOT NULL, stage TEXT NOT NULL, ts INTEGER NOT NULL, status TEXT,
    input_json TEXT, output_json TEXT, PRIMARY KEY (run_id, stage)
);

CREATE TABLE IF NOT EXISTS run_control (
    run_id TEXT PRIMARY KEY, action TEXT, text TEXT, ts INTEGER
);

CREATE TABLE IF NOT EXISTS approval (
    run_id TEXT PRIMARY KEY, kind TEXT, label TEXT, detail TEXT,
    decision TEXT, ts INTEGER
);

CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT);

-- Skill autonomy: full-content version snapshots (rollback target) + a pending
-- change queue (AI/human proposals awaiting approve/veto). Honesty: real records.
CREATE TABLE IF NOT EXISTS skill_version (
    id TEXT PRIMARY KEY, name TEXT, version INTEGER, content TEXT,
    author TEXT, reason TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS skill_change (
    id TEXT PRIMARY KEY, name TEXT, op TEXT, content TEXT, reason TEXT,
    author TEXT, status TEXT, ts INTEGER
);
"""


def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        r = conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO setting(key,value) VALUES(?,?)", (key, value))


def set_control(run_id: str, action: str, text: str = "") -> None:
    """Runtime intervention channel (DB = IPC): another process sets pause/abort/
    append/resume; the running engine polls get_control at safe points."""
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO run_control(run_id,action,text,ts) VALUES(?,?,?,?)",
                     (run_id, action, text, now_ms()))


def get_control(run_id: str) -> dict | None:
    with connect() as conn:
        r = conn.execute("SELECT action,text FROM run_control WHERE run_id=?", (run_id,)).fetchone()
        return {"action": r["action"], "text": r["text"]} if r else None


def clear_control(run_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM run_control WHERE run_id=?", (run_id,))


# ── approval channel (human-in-the-loop gate; DB = IPC, same as run_control) ──

def set_approval_pending(run_id: str, kind: str, label: str, detail: str = "") -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO approval(run_id,kind,label,detail,decision,ts) "
                     "VALUES(?,?,?,?,?,?)", (run_id, kind, label, detail, "pending", now_ms()))


def get_approval(run_id: str) -> dict | None:
    with connect() as conn:
        r = conn.execute("SELECT kind,label,detail,decision FROM approval WHERE run_id=?",
                         (run_id,)).fetchone()
        return dict(r) if r else None


def decide_approval(run_id: str, decision: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE approval SET decision=?, ts=? WHERE run_id=?",
                     (decision, now_ms(), run_id))


def clear_approval(run_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM approval WHERE run_id=?", (run_id,))


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_llm_call(
    *, run_id: str | None, stage: str | None, purpose: str | None,
    model_id: str, api_model: str, tokens_in: int | None, tokens_out: int | None,
    cost_usd: float | None, latency_ms: int, status: str, error: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO llm_calls (id,ts,run_id,stage,purpose,model_id,api_model,"
            "tokens_in,tokens_out,cost_usd,latency_ms,status,error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id(), now_ms(), run_id, stage, purpose, model_id, api_model,
             tokens_in, tokens_out, cost_usd, latency_ms, status, error),
        )


def create_run(*, title: str, requirement: str, repo_path: str, branch: str) -> str:
    run_id = new_id()
    ts = now_ms()
    with connect() as conn:
        conn.execute(
            "INSERT INTO workflow_runs (id,ts_created,ts_updated,title,requirement,"
            "status,stage,repo_path,branch,meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, ts, ts, title, requirement, "running", "clarify", repo_path,
             branch, "{}"),
        )
    return run_id


def update_run(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["ts_updated"] = now_ms()
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE workflow_runs SET {cols} WHERE id=?",
                     (*fields.values(), run_id))


def get_run(run_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def log_event(run_id: str, *, stage: str, event: str,
              payload: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO workflow_events (id,run_id,ts,stage,event,payload_json) "
            "VALUES (?,?,?,?,?,?)",
            (new_id(), run_id, now_ms(), stage, event,
             json.dumps(payload or {}, ensure_ascii=False)),
        )


def save_stage(run_id: str, stage: str, *, status: str,
               input_obj: Any = None, output_obj: Any = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO stage_state (run_id,stage,ts,status,input_json,output_json) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,stage) DO UPDATE SET "
            "ts=excluded.ts,status=excluded.status,input_json=excluded.input_json,"
            "output_json=excluded.output_json",
            (run_id, stage, now_ms(), status,
             json.dumps(input_obj, ensure_ascii=False) if input_obj is not None else None,
             json.dumps(output_obj, ensure_ascii=False) if output_obj is not None else None),
        )


def load_stage(run_id: str, stage: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM stage_state WHERE run_id=? AND stage=?",
                           (run_id, stage)).fetchone()
    return dict(row) if row else None


def run_cost_summary(run_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(tokens_in),0) tin, "
            "COALESCE(SUM(tokens_out),0) tout, COALESCE(SUM(cost_usd),0) cost, "
            "COALESCE(SUM(latency_ms),0) lat FROM llm_calls WHERE run_id=?", (run_id,),
        ).fetchone()
    return {"calls": row["n"], "tokens_in": row["tin"], "tokens_out": row["tout"],
            "cost_usd": round(row["cost"], 6), "latency_ms_total": row["lat"]}
