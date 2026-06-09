"""Configuration: credentials + model registry + level routing.

Credentials are read from ``$STACKWEFT_HOME/secrets.env`` (never hard-coded).
Each model carries a ``level`` in [0, 1]; a caller asks for a level and the
cheapest sufficient model is chosen, falling back along the ladder. The endpoint
speaks the Anthropic Messages protocol at ``/v1/messages``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

STACKWEFT_HOME = Path(os.environ.get("STACKWEFT_HOME", str(Path.home() / ".stackweft")))
SECRETS_PATH = STACKWEFT_HOME / "secrets.env"
# Fallback: credentials bundled with the repo (dev/demo). Used when the home secrets file
# doesn't exist yet — so the web demo / a fresh checkout works without sourcing env first.
_BUNDLED_SECRETS = Path(__file__).resolve().parents[2] / "secrets.env"


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _load_secrets() -> dict[str, str]:
    # prefer $STACKWEFT_HOME/secrets.env; fall back to the repo-bundled one
    return _load_env_file(SECRETS_PATH) or _load_env_file(_BUNDLED_SECRETS)


_ENV = _load_secrets()


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key) or _ENV.get(key) or default


def lang() -> str:
    """User-facing language for all AI <-> user communication. Default 中文;
    override via STACKWEFT_LANG."""
    return _get("STACKWEFT_LANG", "中文")


@dataclass(frozen=True)
class ModelSpec:
    """One registered model. ``cost_*`` are USD per 1M tokens."""

    id: str
    api_model: str
    base_url: str          # messages: POST {base}/v1/messages ; responses: POST {base}/responses
    api_key: str
    level: float
    cost_in: float = 0.0
    cost_out: float = 0.0
    is_reasoning: bool = False  # burns output tokens on hidden reasoning
    wire_api: str = "messages"  # "messages" (Anthropic) | "responses" (OpenAI Responses)


def _build_registry() -> list[ModelSpec]:
    base = _get("STACKWEFT_BASE", "https://your-gateway")
    key = _get("STACKWEFT_TASK_KEY")
    specs = [
        # execution tier — cheap and clean for structured calls
        ModelSpec(
            id="kimi", api_model=_get("STACKWEFT_KIMI_MODEL", "kimi-for-coding"),
            base_url=base, api_key=key, level=0.6,
            cost_in=float(_get("STACKWEFT_KIMI_COST_IN", "0.15")),
            cost_out=float(_get("STACKWEFT_KIMI_COST_OUT", "0.60")),
            is_reasoning=False,
        ),
        # reasoning tier — stronger, spends output tokens thinking
        ModelSpec(
            id="glm", api_model=_get("STACKWEFT_GLM_MODEL", "GLM-5.1"),
            base_url=base, api_key=key, level=0.8,
            cost_in=float(_get("STACKWEFT_GLM_COST_IN", "0.60")),
            cost_out=float(_get("STACKWEFT_GLM_COST_OUT", "2.20")),
            is_reasoning=True,
        ),
    ]
    # optional top tier via the OpenAI Responses wire API (base URL already
    # includes /v1, Bearer auth); used only when explicitly preferred or as a
    # last fallback.
    codex_url, codex_key = _get("CODEX_URL"), _get("CODEX_KEY") or key
    if codex_url and codex_key:
        specs.append(ModelSpec(
            id="codex", api_model=_get("CODEX_MODEL", "gpt-5.5"),
            base_url=codex_url, api_key=codex_key, level=0.9,
            cost_in=float(_get("CODEX_COST_IN", "1.25")),
            cost_out=float(_get("CODEX_COST_OUT", "10.0")),
            is_reasoning=True, wire_api=_get("CODEX_WIRE_API", "responses"),
        ))
    return [s for s in specs if s.api_key]


REGISTRY: list[ModelSpec] = _build_registry()


def reload() -> None:
    """Rebuild the registry from the current environment + secrets file."""
    global REGISTRY, _ENV
    _ENV = _load_secrets()
    REGISTRY = _build_registry()


def by_id(model_id: str) -> ModelSpec:
    for s in REGISTRY:
        if s.id == model_id:
            return s
    raise KeyError(f"unknown model id: {model_id!r}; have {[s.id for s in REGISTRY]}")


def for_level(level: float, prefer: str | None = None) -> ModelSpec:
    """Cheapest model with ``spec.level >= level``; fallback to strongest.

    ``STACKWEFT_PREFER_MODEL`` pins routing to one model id (e.g. ``codex``) for
    every call when the caller didn't pass an explicit ``prefer`` — set it to run
    the whole pipeline on a single model, unset it to restore tiered routing."""
    if not REGISTRY:
        raise RuntimeError(f"no models configured — set credentials in {SECRETS_PATH}")
    prefer = prefer or _get("STACKWEFT_PREFER_MODEL") or None
    eligible = sorted((s for s in REGISTRY if s.level >= level), key=lambda s: s.level)
    pool = eligible or sorted(REGISTRY, key=lambda s: -s.level)
    if prefer:
        # honour the preference even if it sits below the requested level
        for s in (*pool, *REGISTRY):
            if s.id == prefer:
                return s
    return pool[0]


def registry_summary() -> str:
    if not REGISTRY:
        return "(empty registry)"
    return ", ".join(f"{s.id}:{s.api_model}@L{s.level}" for s in REGISTRY)
