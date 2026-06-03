#!/usr/bin/env python3
"""StackWeft delivery benchmark — reproducible, model-held-constant.

Compares two METHODS on the same multi-round requirement set, same repo, same
model tier, scored by the SAME deterministic verifier (StackWeft's counterexample
sentinel probes — the field's value must reach the model + API payload + the
list/detail DOM):

  * weft     — StackWeft's compiled Field-Flow pipeline (+ WeftRecipe reuse).
  * baseline — a Claude-Code-style FREE agentic loop on the SAME model ("glm-as-
               claude"): same tools (read/grep/edit/run), no field-flow / no recipe.

Token accounting is StackWeft's per-call ledger (the gateway's reported usage), so
both arms are measured identically. Pass it ``--key-weft``/``--key-base`` to route
each arm through a distinct gateway key (then the New-API site shows per-arm totals).

Reproduce:  python3 bench/run_bench.py --fields subtitle:text coverImage:image \\
                                       summary:text subtitle:text
Each task resets the target repo to the clean scaffold first.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from stackweft.core import obs                      # noqa: E402
from stackweft.core.config import STACKWEFT_HOME    # noqa: E402
from stackweft.engine import fieldflow, tools, worker  # noqa: E402

HOME = _ROOT
REPO = os.environ.get("STACKWEFT_DEMO_REPO", f"{HOME}/experiments/conduit")
SCAFFOLD = os.environ.get("STACKWEFT_DEMO_BASE", "58634fd")
# field-flow skill lives in the real (machine) skills dir, not the repo
SKILL = str(STACKWEFT_HOME / "skills" / "add-article-field.md")


def reset_repo() -> None:
    subprocess.run(["bash", "-c",
        f"cd {REPO} && git checkout {SCAFFOLD} -q && git reset --hard {SCAFFOLD} -q && "
        f"git clean -fdq frontend backend && git checkout backend/test backend/vitest.config.js "
        f"frontend/vitest.config.js 2>/dev/null; true"], check=False)


def _probe_slots(field: str):
    """Render ONLY the probe test files for a field (the shared scorer) into the repo."""
    sb = tools.Sandbox(REPO)
    ir = fieldflow.build_graph(sb, open(SKILL, encoding="utf-8").read(), field=field)
    written = []
    for n in ir["slots"]:
        if n["kind"] == "new_file" and str(n.get("template", "")).startswith("test_"):
            body = fieldflow._TEMPLATES[n["template"]].format(
                field=field, type=ir["type"], sentinel=ir["sentinel"], index_up="")
            sb.write_file(n["path"], body)
            written.append(n["path"])
    return written, ir["sentinel"]


def _run(cmd: str, cwd: str, timeout: int = 150) -> tuple[bool, str]:
    try:
        r = subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr)
        return (r.returncode == 0), out[-500:]
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"


def verify(field: str) -> dict:
    """Shared scorer: drop the sentinel probes in, run them. Pass ⇒ the field truly
    reached model + payload + DOM, however the arm produced the change."""
    written, _ = _probe_slots(field)
    fl = field.lower()
    be_ok, _ = _run(f"cd backend && npx vitest run test/article.{fl}.test.js", REPO, 120)
    fe_ok, _ = _run(
        f"cd frontend && npx vitest run "
        f"src/components/ArticlesPreview/ArticlesPreview.{fl}.test.jsx "
        f"src/services/setArticle.{fl}.test.js", REPO, 150)
    return {"passed": bool(be_ok and fe_ok), "backend": be_ok, "frontend": fe_ok, "probes": len(written)}


def tokens_for(run_id: str) -> dict:
    c = obs.run_cost_summary(run_id)
    # "fair time" = sum of SUCCESSFUL LLM-call latency (the model's actual response time);
    # excludes subprocess startup, file IO, test runs, and gateway stalls/retries (errored
    # calls are dropped). This is the time metric to compare, not noisy wall-clock.
    import sqlite3
    con = sqlite3.connect(str(obs.DB_PATH))
    llm_ms = con.execute("SELECT COALESCE(SUM(latency_ms),0) FROM llm_calls "
                         "WHERE run_id=? AND status='ok'", (run_id,)).fetchone()[0]
    con.close()
    return {"calls": c["calls"], "tokens_in": c["tokens_in"], "tokens_out": c["tokens_out"],
            "tokens_total": c["tokens_in"] + c["tokens_out"], "llm_ms": int(llm_ms)}


def arm_weft(field: str, kind: str, key: str = "") -> dict:
    reset_repo()
    req = REQS[(field, kind)]
    t = time.time()
    env = {**os.environ, "PYTHONPATH": HOME}
    if key:
        env["STACKWEFT_TASK_KEY"] = key  # route this arm through a distinct gateway key
    p = subprocess.run(["python3", "-m", "stackweft.cli", "run", req],
                       cwd=HOME, capture_output=True, text=True, timeout=900, env=env)
    rid = ""
    for line in p.stdout.splitlines():
        if line.startswith("run_id="):
            rid = line.split("=", 1)[1].strip()
    v = verify(field)
    return {"arm": "weft", "field": field, "run_id": rid, "secs": round(time.time() - t, 1),
            **tokens_for(rid), "verify": v}


_BASE_SYS = (
    "You are an autonomous full-stack engineer working in a React + Express + Sequelize "
    "monorepo (frontend/ + backend/). Deliver the user's change end-to-end: edit the real "
    "files so the new field is (1) defined on the Article Sequelize model, (2) accepted in "
    "create/update controllers and returned in the article API payload, (3) sent by the "
    "frontend setArticle service, (4) rendered on the article list card AND the detail page. "
    "Use the tools to read, grep, edit and run. Keep going until done; be efficient.")


def arm_baseline(field: str, kind: str, key: str = "") -> dict:
    reset_repo()
    req = REQS[(field, kind)]
    if key:  # route this arm through a distinct gateway key (New-API site shows its total)
        os.environ["STACKWEFT_TASK_KEY"] = key
        from stackweft.core import config
        config.reload()
    sb = tools.Sandbox(REPO)
    rid = obs.create_run(title=f"bench-base {field}", requirement=req, repo_path=REPO, branch="bench-base")
    t = time.time()
    try:
        worker.run_task(sandbox=sb, system=_BASE_SYS, task=req, run_id=rid, stage="generate",
                        level=0.8, max_rounds=24, max_tokens=4096, token_budget=120_000)
    except Exception as e:  # noqa: BLE001
        obs.log_event(rid, stage="generate", event="bench_base_error", payload={"err": repr(e)})
    v = verify(field)
    return {"arm": "baseline", "field": field, "run_id": rid, "secs": round(time.time() - t, 1),
            **tokens_for(rid), "verify": v}


# requirement text per (field, kind) — same wording feeds both arms
REQS = {
    ("subtitle", "text"): "给文章增加 subtitle 副标题字段，创建和编辑可填写，文章列表卡片和详情页都展示出来。",
    ("coverImage", "image"): "为文章增加 coverImage 封面图字段，创建和编辑可填写，列表卡片和详情页展示这张图。",
    ("summary", "text"): "给文章增加 summary 摘要字段，前后端打通，列表卡片和详情页展示摘要。",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fields", nargs="+", default=["subtitle:text", "coverImage:image", "summary:text", "subtitle:text"])
    ap.add_argument("--arms", nargs="+", default=["weft", "baseline"])
    ap.add_argument("--key-weft", default="", help="gateway key for the weft arm (New-API per-arm total)")
    ap.add_argument("--key-base", default="", help="gateway key for the baseline arm")
    ap.add_argument("--out", default=f"{HOME}/bench/results.json")
    ap.add_argument("--model", default="", help="force BOTH arms onto one model (fair, e.g. GLM-5.1)")
    args = ap.parse_args()

    if args.model:  # uniform model: both tiers → this model, for both arms (weft subprocess inherits env)
        os.environ["STACKWEFT_KIMI_MODEL"] = args.model
        os.environ["STACKWEFT_GLM_MODEL"] = args.model
        from stackweft.core import config
        config.reload()
        print(f"uniform model = {args.model} (both arms, all tiers)")

    rounds = [(f.split(":")[0], f.split(":")[1]) for f in args.fields]
    results = []
    for i, (field, kind) in enumerate(rounds, 1):
        for arm in args.arms:
            print(f"[{i}/{len(rounds)}] {arm} :: {field} ({kind}) ...", flush=True)
            try:
                r = (arm_weft(field, kind, args.key_weft) if arm == "weft"
                     else arm_baseline(field, kind, args.key_base))
                r["round"] = i
            except Exception as e:  # noqa: BLE001
                r = {"arm": arm, "field": field, "round": i, "error": repr(e), "verify": {"passed": False}}
            results.append(r)
            print(f"    → pass={r.get('verify',{}).get('passed')} "
                  f"tokens={r.get('tokens_total')} calls={r.get('calls')} {r.get('secs')}s", flush=True)

    # per-arm totals
    totals = {}
    for arm in args.arms:
        rs = [r for r in results if r["arm"] == arm]
        totals[arm] = {
            "passed": sum(1 for r in rs if r.get("verify", {}).get("passed")),
            "of": len(rs),
            "tokens_total": sum(r.get("tokens_total") or 0 for r in rs),
            "calls": sum(r.get("calls") or 0 for r in rs),
            "llm_ms": sum(r.get("llm_ms") or 0 for r in rs),
            "secs": round(sum(r.get("secs") or 0 for r in rs), 1),
        }
    out = {"rounds": [f"{f}:{k}" for f, k in rounds], "model": args.model or "default-routing",
           "results": results, "totals": totals}
    open(args.out, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
    print("\n=== TOTALS ===")
    for arm, t in totals.items():
        print(f"  {arm:9} pass {t['passed']}/{t['of']} | tokens {t['tokens_total']:,} | "
              f"calls {t['calls']} | LLM {t['llm_ms']/1000:.0f}s | wall {t['secs']}s")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
