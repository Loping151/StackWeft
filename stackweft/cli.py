"""Command-line interface: drive and inspect end-to-end runs.

    python -m stackweft.cli run "<requirement>" [--repo <path>] [--stop-after STAGE] [--interactive]
    python -m stackweft.cli resume <run_id> [--redo-from STAGE] [--stop-after STAGE]
    python -m stackweft.cli status <run_id>
    python -m stackweft.cli dashboard
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from stackweft.core import config, obs
from stackweft.engine import skills, tools, workflow
from stackweft.platform import projects

_TOOL_ROOT = Path(__file__).resolve().parents[1]  # the StackWeft repo itself


def _logs_dir() -> Path:
    d = config.STACKWEFT_HOME / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _git_toplevel(path: str | Path) -> Path | None:
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:  # noqa: BLE001
        return None


def _new_branch(sb: tools.Sandbox, requirement: str) -> str:
    # Prefix branches with the TARGET repo's name (not the tool name) so it's clear they
    # live in that repo's namespace, e.g. `conduit/add-tagline-<id>`.
    repo = re.sub(r"[^a-z0-9]+", "-", sb.root.name.lower()).strip("-") or "repo"
    slug = re.sub(r"[^a-z0-9]+", "-", requirement.lower())[:32].strip("-") or "change"
    branch = f"{repo}/{slug}-{obs.new_id()[:6]}"
    sb.run_shell(f"git checkout -b {branch} 2>&1 || git checkout {branch}")
    return branch


def cmd_run(args: argparse.Namespace) -> int:
    # No --repo → default to the current directory; a path named in the request wins.
    repo = args.repo
    if not repo:
        proj = projects.resolve_workspace(args.requirement, default_cwd=os.getcwd())
        if not proj:
            print("no workspace resolved — pass --repo or name a directory in the request"); return 1
        repo = proj["repo_path"]
        print(f"workspace={proj['id']} ({proj['name']}) repo={repo} "
              f"git={proj['is_git']} repo_id={proj['repo_id']}")
    # never deliver into StackWeft's own repo (git-root match catches subdirs too)
    if (_git_toplevel(repo) or Path(repo).resolve()) == (_git_toplevel(_TOOL_ROOT) or _TOOL_ROOT):
        print("refusing to deliver into StackWeft's own repo — cd into your target "
              "project (the current directory is the default) or pass --repo <path>.")
        return 1
    # Non-git folder → initialise a baseline so branch/commit/diff/PR work.
    if not projects.is_git_repo(repo):
        if projects.ensure_git_baseline(repo):
            print(f"[intake] {repo} is not a git repo → initialised a baseline (git init + first commit)")
    sb = tools.Sandbox(repo)
    branch = _new_branch(sb, args.requirement)
    run_id = obs.create_run(title=args.requirement[:60], requirement=args.requirement,
                            repo_path=str(sb.root), branch=branch)
    print(f"run_id={run_id}\nbranch={branch}", flush=True)  # emit early — UI waits on this
    # A (generality): first time we see a repo, auto-profile it into a repo-specific
    # field-flow skill (deterministic scan + LOW-tier LLM only as a fallback; stats land
    # under stage='profile'). Later runs reuse the cached skill. Graceful no-op on
    # unsupported shapes → falls back to the bundled/generic skill.
    _rid_token = ""
    try:
        from stackweft.platform import profiler
        _rid_token = profiler.repo_token(sb)
        made = profiler.ensure_repo_skill(sb, run_id=run_id)
        if made:
            print(f"[init] profiled this repo → skill {made}")
    except Exception:  # noqa: BLE001
        pass
    skill = skills.select(args.requirement, repo_id=_rid_token)
    print(f"skill={skill.name} ({skill.path})")
    print(f"models: {config.registry_summary()}")
    ctx = workflow.RunContext(
        run_id=run_id, sandbox=sb, requirement=args.requirement, skill=skill,
        branch=branch, repo_id=projects.repo_identity(str(sb.root)),
        interactive=args.interactive,
        ask=(lambda q: input(f"[clarify] {q}\n> ")) if args.interactive else None)
    result = workflow.run(ctx, stop_after=args.stop_after, ask=getattr(args, "ask", False))
    _report(run_id, result)
    return 0 if result.get("ok") else 1


def cmd_init_repo(args: argparse.Namespace) -> int:
    """Profile a repo and synthesize a repo-specific field-flow Skill (Option A).
    Deterministic scan first; LOW-tier LLM only to disambiguate. Idempotent."""
    from stackweft.platform import profiler
    repo = args.repo or os.getcwd()
    sb = tools.Sandbox(repo)
    try:
        r = profiler.init_repo(sb, level=args.level, write=not args.dry_run)
    except profiler.ProfileError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)); return 1
    p = r["profile"]
    print(f"profiled {repo}")
    print(f"  entity={p['entity']}  shadow={p['shadow_field']}  framework={p['framework']}")
    for k, v in p["globs"].items():
        print(f"  {k:24} {v}")
    print(f"skill={r['skill_name']}" + (f"  → {r['path']}" if r.get("path") else "  (dry-run, not written)"))
    if args.dry_run:
        print("\n--- synthesized skill ---\n" + r["content"])
    return 0


def cmd_clarify_answer(args: argparse.Namespace) -> int:
    """Multi-round: fold the PM's answer into the requirement as confirmed context,
    then RE-CLARIFY (not skip to plan). The clarify agent may ask a deeper question →
    pause again → repeat until satisfied (bounded by a round cap in workflow.run)."""
    run = obs.get_run(args.run_id)
    if not run:
        print(f"no such run: {args.run_id}"); return 1
    row = obs.load_stage(args.run_id, "clarify")
    out = json.loads(row["output_json"]) if (row and row["output_json"]) else {"spec": {}}
    qs = out.get("spec", {}).get("open_questions") or []
    qtxt = "；".join(qs[:5]) if qs else "(PM 补充)"
    req2 = f"{run['requirement']}\n\n[已确认 Q&A] 问：{qtxt}\n答：{args.answer}"
    obs.update_run(args.run_id, requirement=req2)
    obs.log_event(args.run_id, stage="clarify", event="clarify_answered",
                  payload={"answer": args.answer[:300]})
    sb = tools.Sandbox(run["repo_path"])
    ctx = workflow.RunContext(run_id=args.run_id, sandbox=sb, requirement=req2,
                              skill=skills.select(req2), branch=run["branch"],
                              repo_id=projects.repo_identity(str(sb.root)))
    result = workflow.run(ctx, resume=True, redo_from="clarify", ask=True)  # re-clarify, multi-round
    _report(args.run_id, result)
    return 0 if result.get("ok") else 1


def cmd_clarify_ask(args: argparse.Namespace) -> int:
    """Follow-up Q&A with the clarify agent (shared run context) — async, does NOT
    submit the confirmation point. The PM may not understand a term; this answers it."""
    from stackweft.core import llm
    run = obs.get_run(args.run_id)
    if not run:
        print(json.dumps({"error": "no such run"})); return 1
    row = obs.load_stage(args.run_id, "clarify")
    spec = (json.loads(row["output_json"]).get("spec", {}) if (row and row["output_json"]) else {})
    oqs = spec.get("open_questions") or []
    system = (f"你是需求澄清助手，用 {config.lang()} 简洁、具体地回答。下面给你这次交付的需求理解(spec)"
              "和一组【待确认问题】。PM 正在对这些待确认点追问——可能是不懂某个名词、想让你解释"
              "这些问题为什么要问、或想听你的建议。请**直接解释/回答他的追问，结合待确认问题的具体"
              "内容给出实质答案**，必要时给出你推荐的默认取值。不要回复“没有更多补充/没有补充”这类"
              "空话，也不要新增新的待确认问题。")
    user = (f"需求：{run['requirement']}\n\n待确认问题：\n"
            + "\n".join(f"{i+1}. {q}" for i, q in enumerate(oqs))
            + f"\n\nspec：{json.dumps(spec, ensure_ascii=False)}\n\nPM 追问：{args.question}")
    # Execution tier (kimi): a short explanation. The reasoning tier (GLM) spends its
    # output budget on hidden thinking and often returns EMPTY text at small max_tokens.
    res = llm.messages(system=system, msgs=[{"role": "user", "content": user}],
                       level=0.6, run_id=args.run_id, stage="clarify",
                       purpose="clarify_ask", max_tokens=900)
    answer = (res.text or "").strip() or "（这几条待确认点都是为了把口径定清楚，回我一句你的选择或'用默认'即可。）"
    obs.log_event(args.run_id, stage="clarify", event="clarify_ask",
                  payload={"q": args.question[:200], "a": answer[:400]})
    print(json.dumps({"answer": answer}, ensure_ascii=False))
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    """Set a runtime intervention on a running run: pause / abort / append / resume.
    The engine polls this at stage + slot boundaries."""
    if args.action in ("resume", "confirm"):
        # `confirm` = the user OK'd a costly (needs_confirm) requirement. Mechanically it's
        # a resume: clarify is already saved 'done', so the pipeline skips the gate and
        # proceeds. We just log the confirmation for the audit trail.
        obs.clear_control(args.run_id)
        run = obs.get_run(args.run_id)
        if not run:
            print(json.dumps({"error": "no such run"})); return 1
        if args.action == "confirm":
            obs.log_event(args.run_id, stage="clarify", event="feasibility_confirmed",
                          payload={"by": "user"})
        sb = tools.Sandbox(run["repo_path"])
        ctx = workflow.RunContext(run_id=args.run_id, sandbox=sb, requirement=run["requirement"],
                                  skill=skills.select(run["requirement"]), branch=run["branch"],
                                  repo_id=projects.repo_identity(str(sb.root)))
        workflow.run(ctx, resume=True)  # continue from where it paused
        print(json.dumps({"ok": True, "action": args.action})); return 0
    if args.action in ("approve", "deny"):  # resolve a pending human-approval gate
        obs.decide_approval(args.run_id, "approved" if args.action == "approve" else "denied")
        print(json.dumps({"ok": True, "action": args.action})); return 0
    if args.action == "mode":  # set the global approval mode (run-id ignored)
        from stackweft.platform import govern
        ok = govern.set_approval_mode(args.text or "")
        print(json.dumps({"ok": ok, "mode": args.text})); return 0
    if args.action == "abort":
        # Abort must take effect even when the run is PAUSED (no loop polling control) —
        # e.g. aborting while awaiting a clarify confirmation. Mark it terminal NOW and
        # clear any pending state so the cockpit stops showing待确认/批准 points.
        obs.set_control(args.run_id, "abort", "")  # a live loop also returns at next boundary
        try:
            obs.clear_approval(args.run_id)
        except Exception:  # noqa: BLE001
            pass
        run = obs.get_run(args.run_id)
        obs.update_run(args.run_id, status="aborted", stage=(run["stage"] if run else "clarify"))
        print(json.dumps({"ok": True, "action": "abort"})); return 0
    obs.set_control(args.run_id, args.action, args.text or "")
    print(json.dumps({"ok": True, "action": args.action})); return 0


def cmd_resume(args: argparse.Namespace) -> int:
    run = obs.get_run(args.run_id)
    if not run:
        print(f"no such run: {args.run_id}"); return 1
    sb = tools.Sandbox(run["repo_path"])
    ctx = workflow.RunContext(run_id=args.run_id, sandbox=sb,
                              requirement=run["requirement"],
                              skill=skills.select(run["requirement"]),
                              branch=run["branch"],
                              repo_id=projects.repo_identity(str(sb.root)))
    result = workflow.run(ctx, resume=True, redo_from=args.redo_from,
                          stop_after=args.stop_after)
    _report(args.run_id, result)
    return 0 if result.get("ok") else 1


def cmd_status(args: argparse.Namespace) -> int:
    run = obs.get_run(args.run_id)
    if not run:
        print(f"no such run: {args.run_id}"); return 1
    print(json.dumps(run, ensure_ascii=False, indent=2))
    for st in workflow.STAGES:
        row = obs.load_stage(args.run_id, st)
        print(f"  {st:10} {row['status'] if row else '-'}")
    _cost(args.run_id)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    with obs.connect() as conn:
        runs = conn.execute("SELECT id,title,status,stage FROM workflow_runs "
                            "ORDER BY ts_created DESC LIMIT 30").fetchall()
    for r in runs:
        c = obs.run_cost_summary(r["id"])
        print(f"{r['id'][:12]} {r['status']:8} {r['stage']:9} {r['title'][:34]:34} "
              f"[tok={c['tokens_in']}/{c['tokens_out']} calls={c['calls']}]")
    return 0


def cmd_viz(args: argparse.Namespace) -> int:
    from stackweft.report import viz
    rid, page = viz.build_html(args.run_id)
    if not rid:
        print("no runs"); return 1
    out = args.out or str(_logs_dir() / f"viz_{rid[:12]}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {out}")
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    from stackweft.report import viz
    out = args.out or str(_logs_dir() / "weftlearn.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(viz.build_learning_html())
    print(f"wrote {out}")
    return 0


def cmd_json(args: argparse.Namespace) -> int:
    from stackweft.report import viz
    fn = viz.debug_json if getattr(args, "debug", False) else viz.gather
    print(json.dumps(fn(args.run_id), ensure_ascii=False))
    return 0


def _report(run_id: str, result: dict) -> None:
    print("\n=== RESULT ===")
    print(json.dumps({k: v for k, v in result.items() if k != "outputs"},
                     ensure_ascii=False, indent=2)[:2000])
    _cost(run_id)


def _cost(run_id: str) -> None:
    c = obs.run_cost_summary(run_id)
    print(f"[obs] calls={c['calls']} tokens={c['tokens_in']}/{c['tokens_out']} "
          f"latency_total={c['latency_ms_total']}ms")


def cmd_skills_suggest(args: argparse.Namespace) -> int:
    """Surface recurring requirement TYPES that warrant a GENERAL skill (not one-offs)."""
    from stackweft.platform import rag
    cands = rag.skill_candidates(min_count=args.min)
    if not cands:
        print("暂无候选：同类需求未达复现阈值，或都已被专门 Skill 覆盖（一次性任务不写 skill）。")
        return 0
    for c in cands:
        print(f"\n● TYPE {c['shape']}  ×{c['count']}\n  {c['suggestion']}")
        for ex in c["examples"]:
            print(f"    - {ex}")
    return 0


def cmd_govern(args: argparse.Namespace) -> int:
    from stackweft.platform import govern
    role = args.role
    caps = govern.capabilities(project_role=role)
    print(f"project_role={role} capabilities:")
    for c in sorted(caps):
        print(f"  + {c}")
    print("denied (not in role):")
    for c in govern.CAPABILITIES:
        if c not in caps:
            print(f"  - {c}")
    return 0


def cmd_project_add(args: argparse.Namespace) -> int:
    aliases = [a.strip() for a in (args.alias or "").split(",") if a.strip()]
    p = projects.add(args.id, args.name, args.repo, aliases, args.branch)
    fp = projects.fingerprint(p["repo_path"])
    print(f"registered project {p['id']} → {p['repo_path']} [head={fp['head_sha']} tree={fp['tree_hash']}]")
    print(f"now run:  ./stackweft run \"<需求，含别名之一即自动选中>\"   或  --repo {p['repo_path']}")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    for pr in projects.list_projects():
        fp = projects.fingerprint(pr["repo_path"])
        print(f"{pr['id']:10} {pr['name']:16} {pr['repo_path']}  [head={fp['head_sha']} tree={fp['tree_hash']}]")
    return 0


def cmd_skill_request(args: argparse.Namespace) -> int:
    from stackweft.platform import skillsmith
    print(json.dumps(skillsmith.request(args.brief, author="ai"), ensure_ascii=False))
    return 0


def cmd_skill_changes(args: argparse.Namespace) -> int:
    from stackweft.platform import skillsmith
    print(json.dumps(skillsmith.pending_changes(), ensure_ascii=False, indent=2))
    return 0


def cmd_skill_approve(args: argparse.Namespace) -> int:
    from stackweft.platform import skillsmith
    print(json.dumps(skillsmith.approve(args.change_id), ensure_ascii=False))
    return 0


def cmd_skill_veto(args: argparse.Namespace) -> int:
    from stackweft.platform import skillsmith
    print(json.dumps(skillsmith.veto(args.change_id), ensure_ascii=False))
    return 0


def cmd_skill_history(args: argparse.Namespace) -> int:
    from stackweft.platform import skillsmith
    print(json.dumps(skillsmith.history(args.name), ensure_ascii=False, indent=2))
    return 0


def cmd_skill_rollback(args: argparse.Namespace) -> int:
    from stackweft.platform import skillsmith
    print(json.dumps(skillsmith.rollback(args.name, args.version), ensure_ascii=False))
    return 0


def cmd_skill_changelog(args: argparse.Namespace) -> int:
    from stackweft.platform import skillsmith
    print(json.dumps(skillsmith.changelog(), ensure_ascii=False, indent=2))
    return 0


def cmd_skill_consider(args: argparse.Namespace) -> int:
    from stackweft.platform import skillsmith
    print(json.dumps(skillsmith.auto_consider(min_count=args.min), ensure_ascii=False))
    return 0


def cmd_onebot(args: argparse.Namespace) -> int:
    from stackweft.platform import onebot
    if args.caps or not args.event:
        print(json.dumps(onebot.capabilities(), ensure_ascii=False)); return 0
    try:
        event = json.loads(args.event)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"bad event JSON: {e}"})); return 1
    print(json.dumps(onebot.inbound(event), ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stackweft")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run"); pr.add_argument("requirement")
    pr.add_argument("--repo", default=None)
    pr.add_argument("--stop-after", default=None, choices=workflow.STAGES)
    pr.add_argument("--interactive", action="store_true")
    pr.add_argument("--ask", action="store_true", help="pause after clarify if open questions")
    pr.set_defaults(fn=cmd_run)
    pir = sub.add_parser("init-repo", help="profile a repo → synthesize a field-flow skill (A)")
    pir.add_argument("--repo", default=None)
    pir.add_argument("--level", type=float, default=0.6, help="model level for scan assist (default 0.6, low tier)")
    pir.add_argument("--dry-run", action="store_true", help="print the profile + skill, don't write")
    pir.set_defaults(fn=cmd_init_repo)
    pck = sub.add_parser("clarify-ask"); pck.add_argument("run_id"); pck.add_argument("question")
    pck.set_defaults(fn=cmd_clarify_ask)
    pctl = sub.add_parser("control"); pctl.add_argument("run_id")
    pctl.add_argument("action", choices=["pause", "abort", "append", "resume", "confirm",
                                         "approve", "deny", "mode"])
    pctl.add_argument("text", nargs="?", default=""); pctl.set_defaults(fn=cmd_control)
    pca = sub.add_parser("clarify-answer"); pca.add_argument("run_id"); pca.add_argument("answer")
    pca.set_defaults(fn=cmd_clarify_answer)
    prs = sub.add_parser("resume"); prs.add_argument("run_id")
    prs.add_argument("--redo-from", default=None, choices=workflow.STAGES)
    prs.add_argument("--stop-after", default=None, choices=workflow.STAGES)
    prs.set_defaults(fn=cmd_resume)
    ps = sub.add_parser("status"); ps.add_argument("run_id"); ps.set_defaults(fn=cmd_status)
    pd = sub.add_parser("dashboard"); pd.set_defaults(fn=cmd_dashboard)
    pv = sub.add_parser("viz"); pv.add_argument("run_id", nargs="?", default=None)
    pv.add_argument("--out", default=None); pv.set_defaults(fn=cmd_viz)
    pj = sub.add_parser("json"); pj.add_argument("run_id", nargs="?", default=None)
    pj.add_argument("--debug", action="store_true"); pj.set_defaults(fn=cmd_json)
    pl = sub.add_parser("learn"); pl.add_argument("--out", default=None)
    pl.set_defaults(fn=cmd_learn)
    pp = sub.add_parser("projects"); pp.set_defaults(fn=cmd_projects)
    ppa = sub.add_parser("project-add"); ppa.add_argument("id"); ppa.add_argument("name")
    ppa.add_argument("repo"); ppa.add_argument("--alias", default=""); ppa.add_argument("--branch", default="main")
    ppa.set_defaults(fn=cmd_project_add)
    pss = sub.add_parser("skills-suggest"); pss.add_argument("--min", type=int, default=2)
    pss.set_defaults(fn=cmd_skills_suggest)
    pg = sub.add_parser("govern"); pg.add_argument("--role", default="developer")
    pg.set_defaults(fn=cmd_govern)
    # ── skill autonomy (draft/version/rollback/approve) ──
    psr = sub.add_parser("skill-request"); psr.add_argument("brief")
    psr.set_defaults(fn=cmd_skill_request)
    psc = sub.add_parser("skill-changes"); psc.set_defaults(fn=cmd_skill_changes)
    psa = sub.add_parser("skill-approve"); psa.add_argument("change_id")
    psa.set_defaults(fn=cmd_skill_approve)
    psv = sub.add_parser("skill-veto"); psv.add_argument("change_id")
    psv.set_defaults(fn=cmd_skill_veto)
    psh = sub.add_parser("skill-history"); psh.add_argument("name")
    psh.set_defaults(fn=cmd_skill_history)
    psb = sub.add_parser("skill-rollback"); psb.add_argument("name"); psb.add_argument("version", type=int)
    psb.set_defaults(fn=cmd_skill_rollback)
    pscl = sub.add_parser("skill-changelog"); pscl.set_defaults(fn=cmd_skill_changelog)
    psco = sub.add_parser("skill-consider"); psco.add_argument("--min", type=int, default=2)
    psco.set_defaults(fn=cmd_skill_consider)
    pob = sub.add_parser("onebot")  # OneBot v11 interface (reserved): handle one event
    pob.add_argument("--event", default="", help="OneBot v11 message event JSON")
    pob.add_argument("--caps", action="store_true", help="print the OneBot capability claim")
    pob.set_defaults(fn=cmd_onebot)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
