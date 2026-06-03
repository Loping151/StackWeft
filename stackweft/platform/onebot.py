"""OneBot v11 interface (RESERVED / claimed — not a live integration).

StackWeft exposes its "communication / confirmation" surface over the OneBot v11
protocol so a chat app (QQ / 飞书 / 微信 via a OneBot impl) could drive a delivery and
receive progress / confirmation-point / approval messages. This module is the
translation layer + an inbound command dispatcher; it is intentionally NOT wired to a
real OneBot WS/HTTP endpoint (see ``connect`` — that's the reserved hook). It IS live
and testable: feed it an OneBot v11 message event and it returns an OneBot reply, and
it renders a run's state as OneBot messages.

Stdlib only. No nonebot dependency (we speak the wire shape directly).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from stackweft.core import obs

# Directory containing the ``stackweft`` package, so a spawned subprocess can
# run ``python3 -m stackweft.cli`` without an installed package.
_PKG_ROOT = str(Path(__file__).resolve().parents[2])

# What we declare supported (the "claim"). OneBot v11 message events in, text replies out.
CLAIM = {
    "protocol": "OneBot",
    "version": "11",
    "status": "reserved",  # interface live + testable; real transport is a TODO hook
    "post_types": ["message"],
    "message_types": ["private", "group"],
    "commands": {
        "交付 <需求> / deliver <req>": "start a delivery run",
        "状态 / status": "current run status",
        "确认 <答复> / answer <text>": "answer the open confirmation point",
        "批准 / approve, 拒绝 / deny": "resolve a pending approval gate",
        "帮助 / help": "this list",
    },
    "outbound": ["progress", "confirm_point", "approval_request", "result"],
}


def capabilities() -> dict[str, Any]:
    return CLAIM


# ── wire helpers (OneBot v11) ─────────────────────────────────────────────────

def _event_text(event: dict) -> str:
    """Pull the plain text out of a OneBot v11 message event (raw_message or the
    message segment array)."""
    if event.get("raw_message"):
        return str(event["raw_message"]).strip()
    parts = []
    msg = event.get("message")
    if isinstance(msg, list):
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(seg.get("data", {}).get("text", ""))
    elif isinstance(msg, str):
        parts.append(msg)
    return "".join(parts).strip()


def _reply(text: str, **extra: Any) -> dict[str, Any]:
    """OneBot v11 HTTP quick-reply shape (the bot frontend sends this `reply` back)."""
    return {"reply": text, "at_sender": False, **extra}


def render_run(run_id: str | None = None) -> str:
    """Render a run's current state as a OneBot-friendly text message (outbound)."""
    from stackweft.report import viz
    g = viz.gather(run_id)
    if not g.get("run_id"):
        return "还没有交付任务。发「交付 <需求>」开始。"
    rid = g["run_id"][:8]
    if g.get("needs_requirement"):
        return g.get("chat_reply") or "请描述一个具体的改动需求。"
    if g.get("awaiting_approval"):
        pa = g.get("pending_approval") or {}
        return f"[{rid}] 需要批准：{pa.get('label','一个操作')}。回「批准」或「拒绝」。"
    if g.get("awaiting_clarify"):
        qs = "；".join(g.get("open_questions") or []) or "(无问题文本)"
        return f"[{rid}] 需要你确认：{qs}。回「确认 <答复>」。"
    v = g.get("verify") or {}
    vt = ("✅ 验证通过" if v.get("passed") else "❌ 验证未过" if v.get("passed") is False else "进行中")
    return (f"[{rid}] {g.get('stage')} · {g.get('status')} · {vt}\n"
            f"分支 {g.get('branch')} · {g.get('calls')} 次调用 · {g.get('tokens_in',0)} tokens_in")


# ── inbound: OneBot message event → StackWeft action + OneBot reply ───────────

def _spawn(args: list[str]) -> None:
    subprocess.Popen(["python3", "-m", "stackweft.cli", *args], cwd=_PKG_ROOT,
                     env={**os.environ, "PYTHONPATH": _PKG_ROOT},
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def inbound(event: dict) -> dict[str, Any]:
    """Handle one OneBot v11 message event → dispatch to StackWeft + return a reply.
    No real bot needed: POST an event to /api/onebot (or `sw onebot --event ...`)."""
    if event.get("post_type") != "message":
        return {"reply": "", "ignored": f"post_type={event.get('post_type')}"}
    text = _event_text(event)
    low = text.lower()

    def strip_cmd(*prefixes: str) -> str:
        for p in prefixes:
            if text.startswith(p):
                return text[len(p):].strip()
            if low.startswith(p.lower()):
                return text[len(p):].strip()
        return ""

    if not text or low in ("帮助", "help", "?", "？"):
        cmds = "\n".join(f"· {k} — {v}" for k, v in CLAIM["commands"].items())
        return _reply("StackWeft（OneBot 接入·预留）。可用指令：\n" + cmds)

    if text.startswith(("交付", "需求")) or low.startswith("deliver"):
        req = strip_cmd("交付", "需求", "deliver")
        if not req:
            return _reply("用法：交付 <你的需求>")
        _spawn(["run", req, "--ask"])
        return _reply(f"已收到，开始交付：{req[:60]}…\n稍后发「状态」查看进度。")

    if low in ("状态", "status"):
        return _reply(render_run())

    if text.startswith(("批准",)) or low in ("approve", "ok", "同意"):
        rid = _latest_rid()
        if rid:
            obs.decide_approval(rid, "approved")
            return _reply(f"[{rid[:8]}] 已批准，继续执行。")
        return _reply("没有待批准的操作。")

    if text.startswith(("拒绝",)) or low in ("deny", "no", "否决"):
        rid = _latest_rid()
        if rid:
            obs.decide_approval(rid, "denied")
            return _reply(f"[{rid[:8]}] 已拒绝该操作。")
        return _reply("没有待批准的操作。")

    if text.startswith(("确认", "答复")) or low.startswith("answer"):
        ans = strip_cmd("确认", "答复", "answer")
        rid = _latest_rid()
        if rid and ans:
            _spawn(["clarify-answer", rid, ans])
            return _reply(f"[{rid[:8]}] 已提交确认，继续推进。")
        return _reply("用法：确认 <答复>（需有待确认的任务）")

    # default: treat free text as a new requirement
    _spawn(["run", text, "--ask"])
    return _reply(f"已作为新需求开始交付：{text[:60]}…")


def _latest_rid() -> str | None:
    from stackweft.report import viz
    g = viz.gather()
    return g.get("run_id")


# ── reserved transport hook ───────────────────────────────────────────────────

def connect(*_args: Any, **_kwargs: Any):
    """RESERVED: wire a real OneBot v11 transport here (reverse-WS / HTTP POST from a
    QQ/feishu/wechat OneBot implementation → call ``inbound`` per event; push
    ``render_run`` on progress). Intentionally unimplemented — StackWeft only *claims*
    OneBot support; the live integration is out of scope by design."""
    raise NotImplementedError(
        "OneBot transport is reserved; use inbound()/render_run() via /api/onebot to test.")
