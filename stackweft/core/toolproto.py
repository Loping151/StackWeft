"""Text tool-call protocol.

The models behind this gateway do NOT return native Anthropic ``tool_use``
blocks — they emit tool calls as TEXT in their own formats (one Anthropic-XML-ish
``<function_calls><invoke name=...>``; another ``<tool_call: {json}>``), and
``tool_choice:any`` does not change that. So tools are driven over text:

* ``render_catalog(schemas)`` builds a system-prompt section that defines the
  tools and pins ONE canonical output format the model must use:

      ```tool
      {"tool": "write_file", "args": {"path": "...", "content": "..."}}
      ```

  (one fenced ``tool`` block per call; JSON is unambiguous and both models can
  produce it reliably).

* ``parse_calls(text)`` extracts calls. It accepts the canonical fenced form
  AND defensively parses the two native formats above, so we still capture a
  call if the model falls back to its habit.

Returns a list of ``{"name","args","raw"}`` plus the text with tool blocks
stripped (what we log as the assistant's prose).
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCED_TOOL_RE = re.compile(r"```tool\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_ANGLE_TOOL_RE = re.compile(r"<tool_call:\s*(\{.*?\})\s*>", re.DOTALL)
_KIMI_INVOKE_RE = re.compile(
    r'<invoke name="([^"]+)">(.*?)</invoke>', re.DOTALL)
_KIMI_PARAM_RE = re.compile(
    r'<parameter name="([^"]+)">(.*?)</parameter>', re.DOTALL)


def render_catalog(schemas: list[dict[str, Any]]) -> str:
    lines = [
        "## Tools",
        "You have NO native tools here. To act, emit a fenced code block tagged "
        "`tool` containing a single JSON object. Emit one block per call; you may "
        "emit several blocks in one reply. A separate system will run them and "
        "return results, then you continue.",
        "",
        "Format (exactly):",
        "```tool",
        '{"tool": "<name>", "args": { ... }}',
        "```",
        "",
        "When the task is fully complete and verified, reply with NO tool blocks "
        "and start your message with DONE.",
        "",
        "Available tools:",
    ]
    for s in schemas:
        props = s.get("input_schema", {}).get("properties", {})
        req = s.get("input_schema", {}).get("required", [])
        argdesc = ", ".join(
            f"{k}{'*' if k in req else ''}:{v.get('type','any')}"
            for k, v in props.items()
        )
        lines.append(f"- **{s['name']}**({argdesc}) — {s['description']}")
    return "\n".join(lines)


def parse_calls(text: str) -> tuple[list[dict[str, Any]], str]:
    """Return (calls, prose_without_tool_blocks)."""
    calls: list[dict[str, Any]] = []
    stripped = text

    # 1) Canonical fenced ```tool blocks.
    for m in _FENCED_TOOL_RE.finditer(text):
        obj = _try_json(m.group(1))
        if obj and "tool" in obj:
            calls.append({"name": obj["tool"], "args": obj.get("args", {}),
                          "raw": m.group(0)})
    stripped = _FENCED_TOOL_RE.sub("", stripped)

    # 2) glm native: <tool_call: {"name":..,"arguments":..}>
    if not calls:
        for m in _ANGLE_TOOL_RE.finditer(text):
            obj = _try_json(m.group(1))
            if obj and ("name" in obj):
                calls.append({"name": obj["name"],
                              "args": obj.get("arguments", obj.get("args", {})),
                              "raw": m.group(0)})
        stripped = _ANGLE_TOOL_RE.sub("", stripped)

    # 3) Anthropic-style: <function_calls><invoke name="x"><parameter ...>...
    if not calls:
        for m in _KIMI_INVOKE_RE.finditer(text):
            name = m.group(1)
            args = {k: _coerce(v.strip()) for k, v in _KIMI_PARAM_RE.findall(m.group(2))}
            calls.append({"name": name, "args": args, "raw": m.group(0)})
        stripped = re.sub(r"<function_calls>.*?</function_calls>", "", stripped,
                          flags=re.DOTALL)
        stripped = _KIMI_INVOKE_RE.sub("", stripped)

    return calls, stripped.strip()


_ATTEMPT_MARKERS = ("```tool", "<tool_call", "<function_calls", "<invoke",
                    '"tool":', '{"tool"', "write_file", "run_shell", "edit_file")


def looks_like_failed_attempt(text: str, parsed: list[dict[str, Any]]) -> bool:
    """True if the text looks like it WANTED to call a tool but we parsed none.

    Fail-closed signal: when this is true the worker should re-prompt
    for correct formatting rather than treat the turn as prose/DONE — that
    misread is how a single malformed emission strands a run mid-change.
    """
    if parsed:
        return False
    low = text.lower()
    if low.lstrip().startswith("done"):
        return False
    # A tool-ish marker present but nothing parsed → malformed attempt.
    return any(m.lower() in low for m in _ATTEMPT_MARKERS)


def _try_json(s: str) -> dict[str, Any] | None:
    s = s.strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:
        # Tolerate trailing commas / minor noise: grab outermost {...}.
        a, b = s.find("{"), s.rfind("}")
        if a >= 0 and b > a:
            try:
                v = json.loads(s[a:b + 1])
                return v if isinstance(v, dict) else None
            except Exception:
                return None
        return None


def _coerce(v: str) -> Any:
    """Some models return parameters as strings; try to JSON-decode obvious values."""
    if v in ("true", "false"):
        return v == "true"
    try:
        return json.loads(v)
    except Exception:
        return v
