"""Single-agent worker — agentic loop over the text tool protocol.

The gateway's models don't emit native tool_use, so tools are driven over text:
inject the catalog into the system prompt, call the model, parse tool calls from
the reply (toolproto), execute against the sandbox, feed results back, repeat
until DONE or a budget is hit.

Hardening:
* token_budget: a hard per-loop token ceiling.
* _trim_history: bound context growth without losing edit state — keep the task
  + last 12 msgs verbatim, only elide large old "Tool results:" turns.
* thrash detection by PATH: same file rewritten >=4x in the last 5 mutations -> stop.
* fail-closed parsing: a tool-shaped reply that parsed to nothing is re-prompted,
  never mistaken for DONE.
* end-of-budget pressure in the final rounds.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from stackweft.core import llm, obs, toolproto
from stackweft.engine import tools

_ARG_ALIASES = {"file_path": "path", "filepath": "path", "filename": "path",
                "cmd": "command", "old_str": "old", "new_str": "new",
                "old_string": "old", "new_string": "new"}

# Keep recent messages verbatim and only elide large old tool-result turns;
# eliding content the worker still needs forces re-reads, so lean loose — this is
# a safety net against unbounded growth, not a token-squeeze knob.
_KEEP_RECENT_MSGS = 12
_ELIDE_OVER_CHARS = 4000


def _normalize_args(args: dict[str, Any]) -> dict[str, Any]:
    return {_ARG_ALIASES.get(k, k): v for k, v in args.items()}


def _trim_history(msgs: list[dict[str, Any]]) -> None:
    """Elide OLDER tool-result turns; keep task + the most recent verbatim. Safe
    because the repo on disk is the source of truth (the model can re-read).
    Measured (val_final): without aggressive enough eliding, generate context
    grew 3k→28k/round (99k total). We keep the last few turns and elide every
    earlier tool-result over a modest size so cost stays roughly flat per round."""
    if len(msgs) <= _KEEP_RECENT_MSGS + 1:
        return
    cutoff = len(msgs) - _KEEP_RECENT_MSGS
    for i in range(1, cutoff):
        c = msgs[i].get("content")
        if (isinstance(c, str) and len(c) > _ELIDE_OVER_CHARS
                and c.startswith("Tool results:")):
            msgs[i]["content"] = (c[:300] + f"\n…[elided {len(c)-300} chars of an "
                                  "earlier tool result; re-read the file if needed]")


@dataclass
class WorkerResult:
    ok: bool
    final_text: str
    rounds: int
    tool_calls: int
    transcript: list[dict[str, Any]] = field(default_factory=list)


def run_task(
    *, sandbox: tools.Sandbox, system: str, task: str,
    run_id: str | None = None, stage: str = "generate",
    level: float = 0.7, prefer: str | None = None,
    max_rounds: int = 24, max_tokens: int = 4096,
    token_budget: int | None = None,
    schemas: list[dict[str, Any]] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> WorkerResult:
    full_system = system + "\n\n" + toolproto.render_catalog(schemas or tools.SCHEMAS)
    msgs: list[dict[str, Any]] = [{"role": "user", "content": task}]
    transcript: list[dict[str, Any]] = []
    total_tool_calls = 0
    idle_prose = 0
    spent_tokens = 0
    recent_mutations: list[str] = []

    def emit(event: str, payload: dict[str, Any]) -> None:
        if run_id:
            obs.log_event(run_id, stage=stage, event=event, payload=payload)
        if on_event:
            on_event(event, payload)

    for rnd in range(1, max_rounds + 1):
        if token_budget is not None and spent_tokens >= token_budget:
            emit("worker_token_cap", {"round": rnd, "spent": spent_tokens,
                                      "budget": token_budget})
            return WorkerResult(ok=False, final_text="(stopped: worker token budget)",
                                rounds=rnd, tool_calls=total_tool_calls,
                                transcript=transcript)
        pressure = ""
        if rnd > max_rounds - 3:
            pressure = ("\n\n[SYSTEM] Only %d round(s) left. Run your test NOW; if it "
                        "passes reply DONE, else make the single most important fix."
                        % (max_rounds - rnd + 1))
        _trim_history(msgs)
        result = llm.messages(
            system=full_system + pressure, msgs=msgs, level=level, prefer=prefer,
            max_tokens=max_tokens, run_id=run_id, stage=stage,
            purpose=f"worker_round_{rnd}")
        spent_tokens += result.tokens_in + result.tokens_out
        calls, prose = toolproto.parse_calls(result.text)
        transcript.append({"round": rnd, "prose": prose[:300], "ncalls": len(calls)})

        if not calls:
            failed_attempt = toolproto.looks_like_failed_attempt(result.text, calls)
            if not failed_attempt and (prose.upper().startswith("DONE") or idle_prose >= 1):
                emit("worker_done", {"round": rnd, "text": prose[:400]})
                return WorkerResult(ok=True, final_text=prose, rounds=rnd,
                                    tool_calls=total_tool_calls, transcript=transcript)
            idle_prose += 1
            msgs.append({"role": "assistant", "content": result.text or "(no output)"})
            nudge = ("Your last message looked like a tool call but was not valid. Emit "
                     'EXACTLY a fenced ```tool block with {"tool": "...", "args": {...}}.'
                     if failed_attempt else
                     "You emitted no tool block. To act, emit a ```tool block as "
                     "specified. If truly finished, reply starting with DONE.")
            emit("worker_reprompt", {"round": rnd, "failed_attempt": failed_attempt})
            msgs.append({"role": "user", "content": nudge})
            continue

        idle_prose = 0
        msgs.append({"role": "assistant", "content": result.text})
        result_lines = []
        for call in calls:
            total_tool_calls += 1
            name = call["name"]
            args = _normalize_args(call.get("args", {}))
            output = tools.dispatch(sandbox, name, args)
            if name in ("write_file", "edit_file", "multi_edit"):
                recent_mutations.append(str(args.get("path") or args.get("edits")))
            emit("tool_call", {"round": rnd, "name": name,
                               "args": _trunc(args), "output": output[:600]})
            result_lines.append(f"[{name}] -> {output}")
        if len(recent_mutations) >= 5:
            top = Counter(recent_mutations[-5:]).most_common(1)[0][1]
            if top >= 4:
                emit("worker_thrash_stop", {"round": rnd})
                return WorkerResult(ok=False,
                                    final_text="(stopped: thrashing on same file)",
                                    rounds=rnd, tool_calls=total_tool_calls,
                                    transcript=transcript)
        msgs.append({"role": "user",
                     "content": "Tool results:\n" + "\n\n".join(result_lines) +
                     "\n\nContinue, or reply starting with DONE if finished."})

    emit("worker_budget_exhausted", {"max_rounds": max_rounds})
    return WorkerResult(ok=False, final_text="(round budget exhausted)",
                        rounds=max_rounds, tool_calls=total_tool_calls,
                        transcript=transcript)


def _trunc(obj: Any, limit: int = 300) -> Any:
    s = str(obj)
    return s if len(s) <= limit else s[:limit] + "…"
