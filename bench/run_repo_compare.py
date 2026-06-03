#!/usr/bin/env python3
"""Cross-repo generality comparison — StackWeft vs a free coding agent on ANY repo.

The main bench (run_bench.py) is Conduit-only and scores with the sentinel-probe test
suite. For a *different* repo that may lack runnable test infra (e.g. a shallow clone
with no `npm install`), this harness scores with a **static cross-stack coverage** check
instead: after the change, does the new field actually appear across the api-layer model,
the api-layer write path, AND a web-layer render — discovered via the layout, not
hardcoded dirs. Tokens come from StackWeft's per-call ledger (both arms measured the
same). Hold the model constant with --model (e.g. GLM-5.1).

  python3 bench/run_repo_compare.py --repo /tmp/genrepo --entity Product --field subtitle \
      --requirement "给 Product 商品增加 subtitle 副标题字段，列表和详情页展示" --model GLM-5.1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/loping/stackweft")
from stackweft.core import obs                                   # noqa: E402
from stackweft.engine import layout, tools, worker               # noqa: E402

HOME = "/home/loping/stackweft"


def _sh(cmd: str, cwd: str, timeout: int = 120) -> str:
    try:
        r = subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def reset(repo: str) -> None:
    _sh("git checkout . 2>/dev/null; git clean -fdq 2>/dev/null; true", repo)


def static_coverage(repo: str, field: str) -> dict:
    """Layout-aware static cross-stack coverage of the new field, from the git diff's
    ADDED lines. A field is 'threaded' when it shows up in the api model + an api write
    path + at least one web render — that's the cross-stack contract, test-runner-free."""
    sb = tools.Sandbox(repo)
    lay = layout.for_sandbox(sb)
    diff = _sh("git --no-pager diff -U0", repo, 60)
    added: dict[str, list[str]] = {}
    cur = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
            added[cur] = []
        elif line.startswith("+") and not line.startswith("+++") and cur:
            added[cur].append(line[1:])
    hit = {"api_model": False, "api_write": False, "web_render": False, "files": []}
    for path, lines in added.items():
        if not any(field in ln for ln in lines):
            continue
        hit["files"].append(path)
        kind = lay.kind_of(path)
        low = path.lower()
        if kind == "api" and "model" in low:
            hit["api_model"] = True
        elif kind == "api":
            hit["api_write"] = True
        elif kind == "web":
            hit["web_render"] = True
    hit["layers"] = sum(1 for k in ("api_model", "api_write", "web_render") if hit[k])
    hit["threaded"] = hit["api_model"] and hit["api_write"] and hit["web_render"]
    return hit


def tokens_for(run_id: str) -> dict:
    import sqlite3
    c = obs.run_cost_summary(run_id)
    con = sqlite3.connect(str(obs.DB_PATH))
    llm_ms = con.execute("SELECT COALESCE(SUM(latency_ms),0) FROM llm_calls WHERE run_id=? "
                         "AND status='ok'", (run_id,)).fetchone()[0]
    con.close()
    return {"calls": c["calls"], "tokens_total": c["tokens_in"] + c["tokens_out"],
            "tokens_in": c["tokens_in"], "tokens_out": c["tokens_out"], "llm_ms": int(llm_ms)}


def arm_weft(repo: str, requirement: str, field: str, key: str) -> dict:
    reset(repo)
    t = time.time()
    env = {**os.environ, "PYTHONPATH": HOME, "STACKWEFT_SKILL_AUTO": "0"}
    if key:
        env["STACKWEFT_TASK_KEY"] = key
    p = subprocess.run(["python3", "-m", "stackweft.cli", "run", requirement,
                        "--repo", repo, "--stop-after", "generate"],
                       cwd=HOME, capture_output=True, text=True, timeout=1200, env=env)
    rid = next((l.split("=", 1)[1].strip() for l in p.stdout.splitlines()
                if l.startswith("run_id=")), "")
    engaged = "engaged=False" not in p.stdout and "engaged\": false" not in p.stdout
    return {"arm": "weft", "run_id": rid, "secs": round(time.time() - t, 1),
            **tokens_for(rid), "coverage": static_coverage(repo, field)}


_BASE_SYS = (
    "You are an autonomous full-stack engineer. Add the requested NEW FIELD to the "
    "{entity} entity and thread it END-TO-END across this repo's real files: (1) the ORM "
    "model/schema, (2) the create/update write path (controller/route/service) so it is "
    "persisted and returned, (3) the frontend input form, and (4) the list and detail "
    "render. Discover the repo's own conventions by reading files. Use the tools to read, "
    "grep, edit. Keep going until done; be efficient.")


def arm_baseline(repo: str, requirement: str, field: str, entity: str, key: str,
                 budget: int) -> dict:
    reset(repo)
    if key:
        os.environ["STACKWEFT_TASK_KEY"] = key
        from stackweft.core import config
        config.reload()
    sb = tools.Sandbox(repo)
    rid = obs.create_run(title=f"repocmp-base {field}", requirement=requirement,
                         repo_path=repo, branch="repocmp-base")
    t = time.time()
    try:
        worker.run_task(sandbox=sb, system=_BASE_SYS.format(entity=entity), task=requirement,
                        run_id=rid, stage="generate", level=0.8, max_rounds=28,
                        max_tokens=4096, token_budget=budget)
    except Exception as e:  # noqa: BLE001
        obs.log_event(rid, stage="generate", event="repocmp_base_error", payload={"err": repr(e)})
    return {"arm": "baseline", "run_id": rid, "secs": round(time.time() - t, 1),
            **tokens_for(rid), "coverage": static_coverage(repo, field)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--entity", required=True)
    ap.add_argument("--field", required=True)
    ap.add_argument("--requirement", required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--baseline-budget", type=int, default=150_000)
    ap.add_argument("--key-weft", default="")
    ap.add_argument("--key-base", default="")
    ap.add_argument("--out", default=f"{HOME}/bench/results_repocmp.json")
    args = ap.parse_args()
    if args.model:
        os.environ["STACKWEFT_KIMI_MODEL"] = os.environ["STACKWEFT_GLM_MODEL"] = args.model
        from stackweft.core import config
        config.reload()
        print(f"uniform model = {args.model}")
    print(f"repo={args.repo} entity={args.entity} field={args.field}")
    print("[1/2] weft ...", flush=True)
    weft = arm_weft(args.repo, args.requirement, args.field, args.key_weft)
    print(f"    tokens={weft['tokens_total']} calls={weft['calls']} coverage={weft['coverage']}", flush=True)
    print("[2/2] baseline (free agent) ...", flush=True)
    base = arm_baseline(args.repo, args.requirement, args.field, args.entity,
                        args.key_base, args.baseline_budget)
    print(f"    tokens={base['tokens_total']} calls={base['calls']} coverage={base['coverage']}", flush=True)
    out = {"repo": args.repo, "entity": args.entity, "field": args.field,
           "model": args.model or "default", "weft": weft, "baseline": base}
    open(args.out, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
