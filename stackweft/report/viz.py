"""Self-contained HTML report for a run (observability + Field Flow Graph).

No server, no build: renders one static .html from the SQLite obs DB so the
demo can SHOW the pipeline, the Field Flow Graph (slots red/green), and the
per-call token/latency/cost. Stdlib only.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3

from stackweft.core import obs
from stackweft.engine import workflow


def _rows(conn: sqlite3.Connection, sql: str, *args) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, args).fetchall()


def _latest_run_id(conn: sqlite3.Connection) -> str | None:
    r = _rows(conn, "SELECT id FROM workflow_runs ORDER BY ts_created DESC LIMIT 1")
    return r[0]["id"] if r else None


def debug_json(run_id: str | None = None) -> dict:
    """Raw per-call LLM trace + events + WeftGraph/TaskIR for the hidden debug drawer."""
    with obs.connect() as conn:
        if run_id:
            m = _rows(conn, "SELECT id FROM workflow_runs WHERE id LIKE ? ORDER BY ts_created DESC LIMIT 1", run_id + "%")
            rid = m[0]["id"] if m else None
        else:
            rid = _latest_run_id(conn)
        if not rid:
            return {"run_id": None}
        calls = [dict(r) for r in _rows(conn, "SELECT stage,purpose,api_model,tokens_in,tokens_out,"
                 "latency_ms,status FROM llm_calls WHERE run_id=? ORDER BY id", rid)]
        events = [dict(r) for r in _rows(conn, "SELECT stage,event,payload_json FROM workflow_events "
                  "WHERE run_id=? ORDER BY id", rid)]
        cg = _rows(conn, "SELECT output_json FROM stage_state WHERE run_id=? AND stage='compile'", rid)
    weftgraph = json.loads(cg[0]["output_json"]) if (cg and cg[0]["output_json"]) else {}
    return {"run_id": rid, "llm_calls": calls,
            "events": [{"stage": e["stage"], "event": e["event"],
                        "payload": (e["payload_json"] or "")[:300]} for e in events],
            "weftgraph": {"field": weftgraph.get("field"), "errors": weftgraph.get("errors"),
                          "slots": [{"slot": n["slot"], "path": n.get("path"),
                                     "anchors": n.get("anchors")} for n in weftgraph.get("slots", [])]}}


def learning_rows() -> list[dict]:
    """Per-WeftRun learning metrics (cold/warm/hot) for the reuse-acceleration dashboard.

    cold = generate used LLM, no recipe replays; hot = all slots replayed (0 LLM in
    generate); warm = mixed. Reads only the obs DB."""
    rows: list[dict] = []
    with obs.connect() as conn:
        runs = _rows(conn, "SELECT id,title,status,branch,ts_created FROM workflow_runs ORDER BY ts_created")
        for r in runs:
            rid = r["id"]
            gen = _rows(conn, "SELECT COUNT(*) n, COALESCE(SUM(tokens_in),0) ti FROM llm_calls "
                              "WHERE run_id=? AND stage='generate'", rid)[0]
            tot = _rows(conn, "SELECT COALESCE(SUM(tokens_in),0) ti, COALESCE(SUM(tokens_out),0) to_ "
                              "FROM llm_calls WHERE run_id=?", rid)[0]
            evs = _rows(conn, "SELECT event FROM workflow_events WHERE run_id=? AND "
                              "event IN ('recipe_replay','slot_fallback','slot_filled','compile_graph')", rid)
            replays = sum(1 for e in evs if e["event"] == "recipe_replay")
            fallbacks = sum(1 for e in evs if e["event"] == "slot_fallback")
            engaged = any(e["event"] == "compile_graph" for e in evs)
            vrow = _rows(conn, "SELECT output_json FROM stage_state WHERE run_id=? AND stage='verify'", rid)
            v = json.loads(vrow[0]["output_json"]) if (vrow and vrow[0]["output_json"]) else {}
            if not engaged:
                continue  # only field-flow runs have a learning curve
            kind = ("hot" if (replays > 0 and gen["n"] == 0)
                    else "warm" if replays > 0 else "cold")
            rows.append({
                "run": rid[:8], "title": (r["title"] or "")[:26], "kind": kind,
                "total_in": tot["ti"], "total_out": tot["to_"],
                "gen_calls": gen["n"], "gen_in": gen["ti"],
                "recipe_replays": replays, "fallbacks": fallbacks,
                "repairs": v.get("repair_attempts"), "verify": v.get("passed"),
            })
    return rows


def build_learning_html() -> str:
    rows = learning_rows()
    maxgen = max((r["gen_in"] for r in rows), default=1) or 1

    def esc(x):
        return html.escape(str(x))
    trs = ""
    for r in rows:
        bar = int(220 * r["gen_in"] / maxgen)
        color = {"cold": "#e74c3c", "warm": "#e8a33d", "hot": "#2ecc71"}[r["kind"]]
        # 可读性：hot 行 generate 0-LLM，显示成带语义的 "—（0-LLM）" 而非光秃秃的 0
        hot0 = r["kind"] == "hot"
        gc = ("<span title='0-LLM 热路径：全部 WeftRecipe 重放'>— <small>0-LLM</small></span>"
              if hot0 and not r["gen_calls"] else str(r["gen_calls"]))
        gi = ("<span title='generate 阶段未消耗 token（配方重放）'>—</span>"
              if hot0 and not r["gen_in"] else f"{r['gen_in']:,}")
        trs += (f"<tr><td>{esc(r['run'])}</td><td>{esc(r['title'])}</td>"
                f"<td><span class='k' style='background:{color}'>{r['kind']}</span></td>"
                f"<td class='num'>{r['total_in']:,}</td><td class='num'>{gc}</td>"
                f"<td class='num'>{gi}</td>"
                f"<td><div class='bar' style='width:{bar}px;background:{color}'></div></td>"
                f"<td class='num'>{r['recipe_replays']}</td><td class='num'>{r['fallbacks']}</td>"
                f"<td class='num'>{r['repairs']}</td>"
                f"<td>{'✅' if r['verify'] else '❌' if r['verify'] is not None else '—'}</td></tr>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>StackWeft · WeftRecipe 复用加速</title>
<style>body{{font:14px -apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e6e6e6;margin:0}}
.wrap{{max-width:1000px;margin:0 auto;padding:24px}} h1{{font-size:19px}}
table{{width:100%;border-collapse:collapse}} th,td{{padding:6px 8px;border-bottom:1px solid #222;text-align:left}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}} .num{{text-align:right}}
.k{{padding:1px 8px;border-radius:10px;color:#06121f;font-weight:700;font-size:12px}}
.bar{{height:12px;border-radius:6px;min-width:2px}} .muted{{color:#789}}</style></head>
<body><div class="wrap"><h1>StackWeft · WeftRecipe 复用加速曲线（cold · warm · hot）</h1>
<p class="muted">同 Skill + 同仓库：cold=首次(LLM 生成) → warm=部分配方命中 → hot=全部配方重放(generate 0-LLM)。数字来自 SQLite。</p>
<table><tr><th>run</th><th>需求</th><th>kind</th><th>total in</th><th>gen calls</th><th>gen in</th>
<th>gen-token bar</th><th>replays</th><th>fallback</th><th>repairs</th><th>verify</th></tr>{trs}</table>
<p class="muted">gen in = generate 阶段 input token；hot 行应为 0（全部 WeftRecipe 重放，仍过 WeftGate）。</p>
</div></body></html>"""


def gather(run_id: str | None = None) -> dict:
    """JSON-able snapshot of a run for the web tier (live polling)."""
    with obs.connect() as conn:
        if run_id:
            m = _rows(conn, "SELECT id FROM workflow_runs WHERE id LIKE ? ORDER BY ts_created DESC LIMIT 1",
                      run_id + "%")
            rid = m[0]["id"] if m else None
        else:
            rid = _latest_run_id(conn)
        if not rid:
            return {"run_id": None}
        run = _rows(conn, "SELECT * FROM workflow_runs WHERE id=?", rid)[0]
        calls = _rows(conn, "SELECT stage,tokens_in,tokens_out,latency_ms FROM llm_calls WHERE run_id=?", rid)
        stages = {st: (_rows(conn, "SELECT status FROM stage_state WHERE run_id=? AND stage=?", rid, st)
                       or [{"status": "-"}])[0]["status"] for st in workflow.STAGES}
        evs = _rows(conn, "SELECT event,payload_json FROM workflow_events WHERE run_id=? ORDER BY id", rid)
        vrow = _rows(conn, "SELECT output_json FROM stage_state WHERE run_id=? AND stage='verify'", rid)
        crow = _rows(conn, "SELECT output_json FROM stage_state WHERE run_id=? AND stage='clarify'", rid)
    cspec = (json.loads(crow[0]["output_json"]).get("spec", {}) if (crow and crow[0]["output_json"]) else {})
    open_qs = cspec.get("open_questions") or []
    answers = cspec.get("answers") or {}
    not_req = cspec.get("is_requirement") is False
    awaiting_clarify = bool(run["status"] == "paused" and run["stage"] == "clarify"
                            and open_qs and not answers)
    slots, filled = [], {}
    replays = det_slots = 0
    assist_reply = None  # the read-only assistant's answer (overrides spec.reply for display)
    feas_reply = None     # feasibility gate's reply (reject / confirm)
    feas_reason = None
    for e in evs:
        if e["event"] == "not_a_requirement":
            try:
                assist_reply = json.loads(e["payload_json"]).get("reply") or assist_reply
            except Exception:  # noqa: BLE001
                pass
        if e["event"] in ("requirement_rejected", "awaiting_confirm"):
            try:
                _p = json.loads(e["payload_json"])
                feas_reply = _p.get("reply") or feas_reply
                feas_reason = _p.get("reason") or feas_reason
            except Exception:  # noqa: BLE001
                pass
        if e["event"] == "compile_graph":
            slots = [{"slot": n["slot"], "path": n.get("path")}
                     for n in json.loads(e["payload_json"]).get("slots", [])]
        if e["event"] == "recipe_replay":
            replays += 1
        if e["event"] == "slot_filled":
            p = json.loads(e["payload_json"]); filled[p["slot"]] = p["ok"]
            if p.get("mode") == "deterministic":
                det_slots += 1
    verify = json.loads(vrow[0]["output_json"]) if (vrow and vrow[0]["output_json"]) else {}
    for s in slots:
        s["ok"] = filled.get(s["slot"])
    wall_ms = max(0, (run["ts_updated"] or 0) - (run["ts_created"] or 0))
    latency_ms = sum(c["latency_ms"] or 0 for c in calls)
    ap = obs.get_approval(rid)
    pending = ap if (ap and ap.get("decision") == "pending") else None
    try:
        from stackweft.platform import govern
        approval_mode = govern.approval_mode()
    except Exception:  # noqa: BLE001
        approval_mode = "allow-all"
    return {"run_id": rid, "status": run["status"], "stage": run["stage"],
            "approval_mode": approval_mode,
            "awaiting_approval": bool(pending),
            "pending_approval": ({"kind": pending["kind"], "label": pending["label"],
                                  "detail": pending["detail"]} if pending else None),
            "requirement": run["requirement"], "branch": run["branch"],
            "tokens_in": sum(c["tokens_in"] or 0 for c in calls),
            "tokens_out": sum(c["tokens_out"] or 0 for c in calls),
            "calls": len(calls), "stages": stages, "slots": slots,
            "wall_ms": wall_ms, "latency_ms": latency_ms,
            "recipe_replays": replays, "det_slots": det_slots,
            "open_questions": open_qs, "answers": answers, "awaiting_clarify": awaiting_clarify,
            "needs_requirement": bool(not_req and run["status"] == "paused" and run["stage"] == "clarify"),
            "rejected": run["status"] == "rejected",
            "awaiting_confirm": bool(cspec.get("feasibility") == "needs_confirm"
                                     and run["status"] == "paused" and run["stage"] == "clarify"),
            "feasibility": cspec.get("feasibility") or "ok",
            "confirm_reason": feas_reason,
            "chat_reply": ((assist_reply or cspec.get("reply")) if not_req
                           else (feas_reply or cspec.get("reply"))
                           if (run["status"] == "rejected"
                               or cspec.get("feasibility") in ("needs_confirm", "infeasible"))
                           else None),
            "handoffs": handoffs(rid, stages),
            "verify": {"passed": verify.get("passed"),
                       "checks": {ch["name"]: ch["passed"] for ch in verify.get("checks", [])}}}


# Agent roles for the relay animation. The handoff wording is SYNTHESIZED FROM REAL
# stage outputs (not fake decoration) — each line reflects this run's actual data.
_ROLES = {"clarify": "需求分析员", "plan": "规划员", "localize": "定位员",
          "compile": "编织员", "generate": "实现员", "verify": "验收员", "pr": "交付员"}


def handoffs(run_id: str, stages: dict | None = None) -> list[dict]:
    """Per-stage handoff messages (from_role → to_role + wording derived from the
    stage's real output). Powers the agent-relay animation; 0 extra LLM."""
    order = workflow.STAGES
    out: list[dict] = []
    souts: dict[str, tuple] = {}
    with obs.connect() as conn:
        for st in order:
            r = _rows(conn, "SELECT output_json,status FROM stage_state WHERE run_id=? AND stage=?", run_id, st)
            souts[st] = (json.loads(r[0]["output_json"]) if (r and r[0]["output_json"]) else {},
                         r[0]["status"] if r else "-")
        events = _rows(conn, "SELECT event,payload_json FROM workflow_events WHERE run_id=? AND "
                       "event IN ('compile_graph','slot_filled','recipe_replay','slot_fallback')", run_id)
    cg = next((json.loads(e["payload_json"]) for e in events if e["event"] == "compile_graph"), {})
    replays = sum(1 for e in events if e["event"] == "recipe_replay")
    for st in order:
        o, status = souts[st]
        if status != "done":
            continue
        to = order[order.index(st) + 1] if order.index(st) + 1 < len(order) else None
        role, nxt = _ROLES[st], (_ROLES.get(to) if to else "完成")
        if st == "clarify":
            sp = o.get("spec", {})
            if sp.get("is_requirement") is False:
                # Greeting / non-requirement: the reply is surfaced once via `chat_reply`;
                # emitting it as a handoff too would double it in the chat. Skip it here.
                continue
            text = (f"需求是「{(sp.get('summary') or '')[:40]}」，"
                    f"层次 {sp.get('layers') or '?'}。交给规划员。")
        elif st == "plan":
            fh = (o.get("plan", {}) or {}).get("files_hint") or []
            text = f"方案就绪，预计触及 {len(fh)} 处文件，交给定位员核实。"
        elif st == "localize":
            tg = (o.get("targets", {}) or {}).get("targets") or []
            text = f"定位到 {len(tg)} 个相关文件，交给编织员产变更图。"
        elif st == "compile":
            if cg.get("field"):
                text = f"WeftGraph 就绪：字段 `{cg.get('field')}`、{len(cg.get('slots', []))} 个落点，按图交给实现员。"
            else:
                text = "本次走通用路径（无字段流图），交给实现员。"
        elif st == "generate":
            sr = o.get("slot_results") or []
            ok = sum(1 for r in sr if r.get("ok"))
            text = (f"{ok}/{len(sr)} 个落点已接" + (f"（{replays} 个走配方重放·0-LLM）" if replays else "")
                    + "，交给验收员跑闸门。") if sr else "改动完成，交给验收员。"
        elif st == "verify":
            chk = {c["name"]: c["passed"] for c in o.get("checks", [])}
            text = ("测试" + ("✓" if chk.get("tests") else "✗") + "、跨栈一致" +
                    ("✓" if chk.get("cross_stack") else "✗") + "。" +
                    ("全绿，交给交付员提交。" if o.get("passed") else "未全绿，打回实现员定点修复。"))
        else:
            text = "分支已提交、证据包就绪。交付完成。"
        out.append({"stage": st, "from": role, "to": nxt, "text": text})
    return out


def _git(repo: str, *args: str) -> str:
    import subprocess
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True,
                              text=True, timeout=20).stdout
    except Exception:  # noqa: BLE001
        return ""


def _render_patch(patch: str, max_lines: int = 120) -> str:
    """Unified-diff → +add/−del/@@hunk colored HTML."""
    lines = patch.split("\n")
    out, shown = [], lines[:max_lines]
    for ln in shown:
        if ln.startswith("@@"):
            cls = "hunk"
        elif ln.startswith(("+++", "---", "diff ", "index ", "new file", "deleted file", "rename ", "similarity ")):
            cls = "meta"
        elif ln.startswith("+"):
            cls = "add"
        elif ln.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        out.append(f'<div class="dl {cls}">{html.escape(ln) if ln else "&nbsp;"}</div>')
    if len(lines) > len(shown):
        out.append(f'<div class="dl more">… 本文件 diff 省略 {len(lines)-len(shown)} 行</div>')
    return f'<div class="diff">{"".join(out)}</div>'


def _commit_html(repo_path: str, branch: str) -> str:
    """Render the delivered git COMMIT directly — header + files-changed + per-file
    diff, GitHub-style. Programmatic, no AI."""
    if not repo_path or not branch:
        return '<p class="muted">（无分支信息）</p>'
    base = "main" if _git(repo_path, "rev-parse", "--verify", "main").strip() else "58634fd"
    rng = f"{base}...{branch}"
    meta = _git(repo_path, "log", "-1", branch, "--format=%h%x1f%an%x1f%ad%x1f%s", "--date=short").strip()
    sha, author, date, subj = (meta.split("\x1f") + ["", "", "", ""])[:4] if meta else ("", "", "", "")
    numstat = [l for l in _git(repo_path, "diff", "--numstat", rng).splitlines() if l.strip()]
    full = _git(repo_path, "diff", "--no-color", rng)
    patches: dict[str, str] = {}
    cur, buf = None, []
    for ln in full.splitlines():
        if ln.startswith("diff --git"):
            if cur:
                patches[cur] = "\n".join(buf)
            buf = [ln]
            m = re.search(r" b/(.+)$", ln)
            cur = m.group(1) if m else ln
        else:
            buf.append(ln)
    if cur:
        patches[cur] = "\n".join(buf)
    if not numstat:
        # no diff vs base → nothing was delivered (e.g. a chat / clarify / assist run, or a
        # run that didn't reach generate). Don't render the scaffold commit as a fake change.
        return '<p class="muted">（本次无代码变更——咨询/澄清类对话，或尚未进入交付）</p>'
    adds = sum(int(x.split("\t")[0]) for x in numstat if x.split("\t")[0].isdigit())
    dels = sum(int(x.split("\t")[1]) for x in numstat if x.split("\t")[1].isdigit())
    header = (f'<div class="commit"><div class="commit-h">'
              f'<span class="csha mono">{html.escape(sha)}</span> <b>{html.escape(subj)}</b></div>'
              f'<div class="commit-m">{html.escape(author)} · {html.escape(date)} · '
              f'{len(numstat)} files <span class="add">+{adds}</span> <span class="del">−{dels}</span>'
              f' · <span class="mono">{html.escape(branch)}</span></div></div>')
    blocks = []
    for row in numstat[:30]:
        a, d, fn = (row.split("\t") + ["", "", ""])[:3]
        blocks.append(
            f'<div class="file-row"><span class="fname mono">{html.escape(fn)}</span>'
            f'<span class="fdelta"><span class="add">+{html.escape(a)}</span> '
            f'<span class="del">−{html.escape(d)}</span></span></div>'
            + (_render_patch(patches.get(fn, "")) if patches.get(fn) else ""))
    return header + '<div class="files">' + "".join(blocks) + "</div>"


def build_html(run_id: str | None = None) -> tuple[str, str]:
    with obs.connect() as conn:
        if run_id:
            m = _rows(conn, "SELECT id FROM workflow_runs WHERE id LIKE ? ORDER BY ts_created DESC LIMIT 1",
                      run_id + "%")
            rid = m[0]["id"] if m else None
        else:
            rid = _latest_run_id(conn)
        if not rid:
            return "", "<h1>no runs</h1>"
        run = _rows(conn, "SELECT * FROM workflow_runs WHERE id=?", rid)[0]
        calls = _rows(conn, "SELECT * FROM llm_calls WHERE run_id=? ORDER BY id", rid)
        evs = _rows(conn, "SELECT stage,event,payload_json FROM workflow_events "
                          "WHERE run_id=? ORDER BY id", rid)
        stages = {st: (_rows(conn, "SELECT status FROM stage_state WHERE run_id=? AND stage=?",
                             rid, st) or [{"status": "-"}])[0]["status"]
                  for st in workflow.STAGES}

    tin = sum(c["tokens_in"] or 0 for c in calls)
    tout = sum(c["tokens_out"] or 0 for c in calls)
    cost = sum(c["cost_usd"] or 0 for c in calls)
    lat = sum(c["latency_ms"] or 0 for c in calls)

    # Field Flow Graph: slots from compile_graph; green/red from latest verify cross_stack.
    graph_slots, missing = [], set()
    for e in evs:
        if e["event"] == "compile_graph":
            graph_slots = json.loads(e["payload_json"]).get("slots", [])
    with obs.connect() as conn:
        vrow = _rows(conn, "SELECT output_json FROM stage_state WHERE run_id=? AND stage='verify'", rid)
    verify = json.loads(vrow[0]["output_json"]) if (vrow and vrow[0]["output_json"]) else {}
    for ch in verify.get("checks", []):
        if ch.get("name") == "cross_stack":
            missing = {m["slot"] for m in ch.get("missing_slots", [])}

    def esc(x) -> str:
        return html.escape(str(x))

    # pipeline
    pipe = "".join(
        f'<div class="st {("ok" if stages[s]=="done" else "run" if stages[s]=="running" else "na")}">'
        f'{esc(s)}<br><small>{esc(stages[s])}</small></div>'
        + ('<div class="arrow">→</div>' if i < len(workflow.STAGES)-1 else '')
        for i, s in enumerate(workflow.STAGES))

    # field flow graph
    if graph_slots:
        cards = []
        for n in graph_slots:
            slot = n.get("slot", "")
            green = slot not in missing and bool(verify.get("checks"))
            cls = "node green" if green else ("node red" if slot in missing else "node")
            cards.append(
                f'<div class="{cls}"><b>{esc(slot)}</b>'
                f'<div class="path">{esc(n.get("path") or "(new file)")}</div>'
                f'<div class="anch">anchors: {esc(n.get("anchors"))}</div></div>')
        graph = '<div class="graph">' + "".join(cards) + '</div>'
    else:
        graph = '<p class="muted">(no Field Flow Graph — compile did not engage for this run)</p>'

    # the delivered commit, rendered GitHub-style (programmatic, no AI)
    diff_html = _commit_html(run["repo_path"], run["branch"])

    # calls table
    trs = "".join(
        f"<tr><td>{esc(c['stage'])}</td><td>{esc(c['purpose'])}</td>"
        f"<td>{esc(c['api_model'])}</td><td class='num'>{esc(c['tokens_in'])}</td>"
        f"<td class='num'>{esc(c['tokens_out'])}</td><td class='num'>{esc(c['latency_ms'])}</td></tr>"
        for c in calls)

    vstatus = verify.get("passed")
    vbadge = ("PASS" if vstatus else "FAIL" if vstatus is not None else "—")
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>StackWeft · {esc(rid[:12])}</title><style>
body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px}}
h1{{font-size:20px}} h2{{font-size:15px;color:#9ad;margin-top:28px;border-bottom:1px solid #283;padding-bottom:4px}}
.req{{background:#1a1d24;padding:12px 14px;border-radius:8px;color:#cdd}}
.kpis{{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0}}
.kpi{{background:#1a1d24;border-radius:8px;padding:10px 16px;min-width:110px}}
.kpi b{{display:block;font-size:20px;color:#fff}} .kpi span{{color:#89a;font-size:12px}}
.badge{{padding:2px 10px;border-radius:12px;font-weight:700}}
.PASS,.ok b{{}} .pass{{background:#1c7c46;color:#fff}} .fail{{background:#a33;color:#fff}}
.pipe{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.st{{background:#222732;border-radius:8px;padding:8px 12px;text-align:center;min-width:78px}}
.st.ok{{background:#173d2a;border:1px solid #1c7c46}} .st.run{{background:#3a3413;border:1px solid #aa3}}
.st.na{{opacity:.5}} .arrow{{color:#567}}
.graph{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}}
.node{{background:#222732;border-radius:8px;padding:10px;border-left:4px solid #567}}
.node.green{{border-left-color:#2ecc71}} .node.red{{border-left-color:#e74c3c}}
.node .path{{color:#8ab;font-size:12px;word-break:break-all}} .node .anch{{color:#678;font-size:11px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid #222}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}} .muted{{color:#678}}
.commit{{background:#11151c;border:1px solid #283;border-radius:8px;padding:10px 12px;margin-bottom:8px}}
.commit-h .csha{{color:#5bd}} .commit-m{{color:#89a;font-size:12px;margin-top:3px}}
.file-row{{display:flex;justify-content:space-between;background:#161a21;border:1px solid #222;border-radius:6px 6px 0 0;padding:5px 10px;margin-top:10px;font-size:12.5px}}
.fname{{color:#cdd}} .add{{color:#3fb950}} .del{{color:#f85149}}
.diff{{font:11.5px/1.45 ui-monospace,Menlo,Consolas,monospace;background:#0b0d11;border:1px solid #222;border-top:0;border-radius:0 0 6px 6px;overflow:auto;max-height:420px}}
.diff .dl{{padding:0 10px;white-space:pre-wrap;word-break:break-all}}
.dl.meta{{color:#9ab;background:#11151c}} .dl.hunk{{color:#5bd;background:#10212b}}
.dl.add{{color:#7fe0a0;background:rgba(63,185,80,.10)}} .dl.del{{color:#ff9b9b;background:rgba(248,81,73,.10)}}
.dl.ctx{{color:#9aa}} .dl.more{{color:#678}}
</style></head><body><div class="wrap">
<h1>StackWeft 跨栈交付报告 · <span class="muted">{esc(rid)}</span></h1>
<div class="req"><b>需求：</b>{esc(run['requirement'])}</div>
<div class="kpis">
<div class="kpi"><b>{esc(run['status'])}</b><span>run status</span></div>
<div class="kpi"><b class="badge {'pass' if vstatus else 'fail' if vstatus is not None else ''}">{vbadge}</b><span>verify</span></div>
<div class="kpi"><b>{tin:,}</b><span>input tokens</span></div>
<div class="kpi"><b>{tout:,}</b><span>output tokens</span></div>
<div class="kpi"><b>{len(calls)}</b><span>LLM calls</span></div>
<div class="kpi"><b>${cost:.3f}</b><span>cost</span></div>
<div class="kpi"><b>{lat/1000:.1f}s</b><span>llm latency</span></div>
<div class="kpi"><b>{esc(run['branch'])}</b><span>git branch</span></div>
</div>
<h2>Pipeline</h2><div class="pipe">{pipe}</div>
<h2>代码变更 · git diff （程序化追踪，不依赖 AI）</h2>{diff_html}
<h2>Field Flow Graph （槽位证据：绿=已接字段，红=缺口）</h2>{graph}
<h2>LLM calls （可观测：每调用 token / 延迟）</h2>
<table><tr><th>stage</th><th>purpose</th><th>model</th><th>tok_in</th><th>tok_out</th><th>ms</th></tr>{trs}</table>
<p class="muted">数字均来自 SQLite 实时计账，不可手填</p>
</div></body></html>"""
    return rid, page
