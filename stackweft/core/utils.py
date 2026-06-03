"""Shared helpers (kept out of the stage engine for reuse + tidiness)."""

from __future__ import annotations

import html
import json
import re
from typing import Any


def json_repair(s: str) -> str:
    """Best-effort repair of common LLM JSON breakage (stdlib only, offline):
    smart quotes, // and /* */ comments, trailing commas, and truncated tails
    (auto-close an open string, then balance ] and })."""
    s = (s.replace("“", '"').replace("”", '"')
         .replace("‘", "'").replace("’", "'").replace("﻿", ""))
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"(^|[^:])//[^\n]*", r"\1", s)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    if s.count('"') % 2:
        s = s + '"'
    db = s.count("[") - s.count("]")
    dc = s.count("{") - s.count("}")
    if db > 0:
        s = s + "]" * db
    if dc > 0:
        s = re.sub(r",\s*$", "", s.rstrip()) + "}" * dc
    return s


def json_extract(text: str) -> dict[str, Any]:
    """Pull a JSON object out of an LLM reply (handles code fences, surrounding
    prose, and malformed JSON via json_repair). Returns {'_unparsed': ...} on failure."""
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
        if m:
            t = m.group(1)
    s, e = t.find("{"), t.rfind("}")
    cand = t[s:e + 1] if (s >= 0 and e > s) else (t[s:] if s >= 0 else t)
    for attempt in (cand, json_repair(cand)):
        try:
            return json.loads(attempt)
        except Exception:  # noqa: BLE001
            continue
    return {"_unparsed": text[:1000]}


def esc(value: Any) -> str:
    """HTML-escape for server-rendered fragments."""
    return html.escape("" if value is None else str(value))
