"""Anthropic Messages client + automatic per-call accounting.

The gateway speaks the Anthropic Messages protocol, but the models behind it do
not emit native ``tool_use`` blocks — they return tool calls as text. So this
client deliberately does not pass a ``tools`` param; tool-calling is handled one
layer up via the text protocol in ``toolproto.py`` (catalog injected into the
system prompt, calls parsed out of the reply text). This module just builds the
request, POSTs, parses text + usage, records the call, and falls back along the
model ladder on error.

Stdlib only (``urllib``). Every call is timed and written to ``obs.llm_calls``.
A reasoning-tier model spends output tokens on hidden reasoning first, so callers
wanting text must give generous ``max_tokens``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from stackweft.core import config, obs


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    tool_calls: list[dict[str, Any]]
    raw_content: list[dict[str, Any]]
    stop_reason: str
    tokens_in: int
    tokens_out: int
    model_id: str


def _estimate_cost(spec: config.ModelSpec, tin: int, tout: int) -> float:
    return (tin / 1_000_000) * spec.cost_in + (tout / 1_000_000) * spec.cost_out


def _post(spec: config.ModelSpec, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    if spec.wire_api == "responses":  # OpenAI Responses API; CODEX_URL already ends in /v1
        url = f"{spec.base_url.rstrip('/')}/responses"
        headers = {"Authorization": f"Bearer {spec.api_key}", "content-type": "application/json",
                   "accept": "text/event-stream"}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        return _read_responses_stream(req, timeout)  # this gateway requires stream=true (SSE)
    url = f"{spec.base_url}/v1/messages"  # Anthropic Messages
    headers = {"x-api-key": spec.api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _read_responses_stream(req: urllib.request.Request, timeout: float) -> dict[str, Any]:
    """Consume the Responses SSE stream → reassemble {output_text, usage, status}."""
    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    status = ""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]" or not payload:
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type", "")
            if etype == "response.output_text.delta":
                text_parts.append(ev.get("delta", "") or "")
            elif etype in ("response.completed", "response.incomplete", "response.failed"):
                r = ev.get("response", {}) or {}
                usage = r.get("usage", usage) or usage
                status = r.get("status", etype)
                if not text_parts:  # no deltas seen → pull final output text
                    for item in r.get("output", []) or []:
                        if item.get("type") == "message":
                            for blk in item.get("content", []) or []:
                                if blk.get("type") in ("output_text", "text"):
                                    text_parts.append(blk.get("text", "") or "")
    return {"output_text": "".join(text_parts), "usage": usage, "status": status}


def _build_body(spec: config.ModelSpec, *, system: str, msgs: list[dict[str, Any]],
                tools, tool_choice, max_tokens: int, temperature: float | None) -> dict[str, Any]:
    if spec.wire_api == "responses":
        body: dict[str, Any] = {"model": spec.api_model, "instructions": system,
                                "input": msgs, "max_output_tokens": max_tokens,
                                "stream": True}  # this gateway requires SSE streaming
        if temperature is not None:
            body["temperature"] = temperature
        return body
    body = {"model": spec.api_model, "max_tokens": max_tokens, "system": system, "messages": msgs}
    if tools:
        body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
    if temperature is not None:
        body["temperature"] = temperature
    return body


def messages(
    *, system: str, msgs: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | None = None,
    level: float = 0.7, prefer: str | None = None,
    max_tokens: int = 4096, temperature: float | None = None, timeout: float = 240.0,
    run_id: str | None = None, stage: str | None = None, purpose: str | None = None,
) -> LLMResult:
    """One Anthropic Messages call with fallback across the level ladder."""
    if not config.REGISTRY:
        raise LLMError("no models configured (check secrets.env)")
    primary = config.for_level(level, prefer=prefer)
    order = [primary] + [s for s in config.REGISTRY if s.id != primary.id]

    last_err = ""
    for spec in order:
        body = _build_body(spec, system=system, msgs=msgs, tools=tools,
                           tool_choice=tool_choice, max_tokens=max_tokens, temperature=temperature)
        started = time.time()
        try:
            data = _post(spec, body, timeout)
            latency_ms = int((time.time() - started) * 1000)
            result = _parse(data, spec)
            obs.record_llm_call(
                run_id=run_id, stage=stage, purpose=purpose, model_id=spec.id,
                api_model=spec.api_model, tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=_estimate_cost(spec, result.tokens_in, result.tokens_out),
                latency_ms=latency_ms, status="ok",
            )
            return result
        except urllib.error.HTTPError as e:
            latency_ms = int((time.time() - started) * 1000)
            last_err = f"{spec.id} HTTP {e.code}: {e.read().decode('utf-8','replace')[:400]}"
        except Exception as e:  # noqa: BLE001
            latency_ms = int((time.time() - started) * 1000)
            last_err = f"{spec.id}: {e!r}"
        obs.record_llm_call(
            run_id=run_id, stage=stage, purpose=purpose, model_id=spec.id,
            api_model=spec.api_model, tokens_in=None, tokens_out=None, cost_usd=None,
            latency_ms=latency_ms, status="error", error=last_err,
        )
    raise LLMError(f"all models failed; last={last_err}")


def _parse(data: dict[str, Any], spec: config.ModelSpec) -> LLMResult:
    if spec.wire_api == "responses":
        text_parts: list[str] = []
        for item in data.get("output", []) or []:  # output items; skip reasoning items
            if item.get("type") == "message":
                for blk in item.get("content", []) or []:
                    if blk.get("type") in ("output_text", "text"):
                        text_parts.append(blk.get("text", ""))
        if not text_parts and isinstance(data.get("output_text"), str):
            text_parts.append(data["output_text"])  # some gateways add this convenience field
        usage = data.get("usage", {}) or {}
        return LLMResult(
            text="\n".join(text_parts).strip(), tool_calls=[],
            raw_content=data.get("output", []) or [], stop_reason=data.get("status", ""),
            tokens_in=int(usage.get("input_tokens", 0) or 0),
            tokens_out=int(usage.get("output_tokens", 0) or 0), model_id=spec.id)
    content = data.get("content", []) or []
    text_parts, tool_calls = [], []
    for block in content:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({"id": block.get("id"), "name": block.get("name"),
                               "input": block.get("input", {})})
    usage = data.get("usage", {}) or {}
    return LLMResult(
        text="\n".join(text_parts).strip(), tool_calls=tool_calls, raw_content=content,
        stop_reason=data.get("stop_reason", ""),
        tokens_in=int(usage.get("input_tokens", 0) or 0),
        tokens_out=int(usage.get("output_tokens", 0) or 0), model_id=spec.id,
    )
