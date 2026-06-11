<div align="center">

<img src="assets/icon.png" width="120" alt="StackWeft icon">

# StackWeft

**A "super‑individual" for full‑stack delivery** — turn a plain‑language request into a verifiable, cross‑stack change in a real repo, on the smallest token budget.

`Approachable · Intuitive · Faster every use · Cheaper every use`

<sub>**English** · [中文](README.md)</sub>

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![deps](https://img.shields.io/badge/runtime%20deps-stdlib%20only-success)](#)
[![state](https://img.shields.io/badge/state-SQLite-003B57?logo=sqlite&logoColor=white)](#)
[![protocol](https://img.shields.io/badge/IM-OneBot-5865F2)](#ecosystem)
[![Paper](https://img.shields.io/badge/Paper-PDF-B31B1B?logo=latex&logoColor=white)](assets/stackweft-paper.pdf)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<img src="assets/header.png" width="100%" alt="StackWeft">

</div>

---

## Why StackWeft

It's for seasoned full‑stack developers — and just as much for everyday builders with ideas but little code. The positioning is deliberate: **not a general coding tool, not a do‑everything Agent framework. It targets one domain — shipping changes to full‑stack projects — and aims to be a "super‑individual" at the best possible cost.**

- **vs. Claude Code / Codex** — capable, but session‑based. Without hand‑curated project memory, a new session must re‑read a lot of code for even a tiny change; reusing an old session drags in unrelated context. High friction, high cost, poor reuse.
- **vs. general agents like OpenClaw / Hermes** — everything lives in one session, so dead context piles up and sessions can't be switched cleanly. More general, but reuse is worse and the bill is higher.
- **StackWeft's answer** — you talk to a single **"individual"**: a conversational agent flow with **no session concept**. It neither re‑stuffs redundant prompts nor relies on manual memory — it pulls prior requirements from a database and **finds or creates a skill to reuse high‑value workflows.**

StackWeft is a bet on the *low‑token era*. When the high‑token party winds down, what lasts are tools that are genuinely efficient and cost‑effective — not token furnaces.

## Faster and cheaper the more you use it

- **Trivial work becomes tooling.** Additive changes — adding a field and wiring it front‑to‑back (a read‑count field, a `coverImage`, a comment `likeCount`, a `status` draft flag, an `updatedAt`) — are distilled into **parameterized patches** that edit code directly: **zero LLM calls, zero redundant context.**
- **Complex changes become skills.** Reusable parts (cross‑stack call paths, etc.) are lifted into a **skill** the AI replays faster next time.
- Cheaper = the same type of change on the same repo costs less each time; faster = high‑value workflows are sedimented and reused.

## No repo to specify · live multi‑repo switching

The database is keyed by a **hash of the repo location** you mention in plain language — no manual repo argument; just say it. You can **switch repos on the fly** and handle several repos and requests from one conversation.

## Ecosystem

- The communication interface speaks the **OneBot** standard protocol, so it can plug into Feishu / WeChat and other chat surfaces or embeddable web widgets.
- It runs as a **CLI**, and can act as a **subagent** for other agents — see below.

### Use it as a skill from Claude Code / Codex

StackWeft can be packaged as a **skill** so a host agent delegates the whole "full‑stack field / small‑feature delivery" job: the host gives one sentence, StackWeft compiles the change sites, fills the slots, runs counter‑example probes, opens a branch/PR, and hands back a structured result. The contract is two commands — `sw run "<request>" [--repo <path>]` (exit 0 = passed / 1 = not) and `sw json --debug` for the structured result. Ready‑made SKILL.md / AGENTS.md snippets are in [`integrations/`](integrations/).

## How it works

A request lands in a real full‑stack repo through a multi‑stage pipeline; each stage is a focused agent, and only the hard parts escalate to a free‑form agent:

<img src="assets/method.png" width="100%" alt="StackWeft method: one request → clarify/plan/localize/compile/generate/verify/pr; a Field Flow Graph weaves the new field through frontend/backend/DB; generate degrades recipe-reuse (0-LLM) → deterministic template → LLM; verify gates with counter-example probes">

- **compile (the core)** — from an existing "shadow field," locate its real cross‑stack sites and clone the new field's slots + anchors across frontend / backend / DB into a read‑only **Field Flow Graph**; an incomplete contract fails fast.
- **generate** — fill slot by slot, degrading by how much is known: **parameterized patch / recipe reuse (0‑LLM) → LLM only when needed**; each slot is checked against evidence and rolled back if it doesn't hold.
- **verify** — Lint + unit tests + **counter‑example `sentinel` probes** (a sentinel value must actually reach the DOM / API payload), with targeted repair on failure.

Every event is persisted in SQLite — pause / resume / interrupt / append, and survive unexpected termination with restart + recovery.

## Results

**Same model (GLM‑5.1)**, same repo, same **counter‑example probe scoring** — comparing two *methods*: the StackWeft pipeline vs. a free agentic loop (Claude‑Code‑style). Three requests (`subtitle` text → `coverImage` image → `subtitle` repeat):

| Method | Pass | Total tokens | LLM calls | LLM time* | Wall |
|---|---|---|---|---|---|
| **StackWeft pipeline** | **3/3** | **19,655** | 15 | **229s** | 260s |
| coding agent (free loop) | 3/3 | 309,391 | 53 | 405s | 416s |

**Three identical, verifiable deliveries on the same model — StackWeft used ~15.7× fewer tokens** (19.7k vs 309k) and was ~1.8× faster on fair LLM time. Both pass the same scorer, so this is a **harness‑design efficiency gap**: the free agent can get it right, but long agentic context + per‑step reasoning make it volatile (91k–124k per round, up to 24 calls); StackWeft holds ~6.5k/round and ~5 calls (compile is read‑only grep, generate fills verified slots in one shot, repeats hit the WeftRecipe hot path, some slots **0‑LLM**).

> *LLM time = sum of successful LLM‑call latencies (excludes subprocess spin‑up, file IO, test runs, and gateway hiccups/retries) — a fairer clock. Accounting uses StackWeft's per‑call meter on the paid GLM‑5.1 API, cross‑checked against the gateway's per‑key usage log down to the token (weft 19,655 / baseline 309,391). Reproduce: `python3 bench/run_bench.py --model GLM-5.1`; details in [`bench/BENCH.md`](bench/BENCH.md).

### Model coverage

Tested and tuned across a range of frontier models — all of the following complete a full delivery:

| MiMo-2.5-Pro | DeepSeek-V4-Pro | Kimi-K2.6 | MiniMax-M3 | Qwen3.7-Max | GLM-5.1 |
|:---:|:---:|:---:|:---:|:---:|:---:|
| ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

## Quick start

```bash
# one-time install: create ~/.stackweft, write the secrets template, put sw on PATH
bash install.sh && source ~/.bashrc
# then fill the model gateway URL + key into ~/.stackweft/secrets.env

# defaults to the repo of the current directory (cd into your target and just run)
cd ~/projects/myapp
sw run "add a subtitle to the article detail page, editable when editing a post"

# vague is fine — it asks one question on anything unclear before touching code
sw run "add a SKU to products"

# another repo: name the path in the sentence, or use --repo
sw run "in the shop project, add a draft status to orders"
sw run "add a SKU to products" --repo ~/projects/shop

sw json --debug                   # last run's stages / tokens
sw control <run_id> pause|abort|append|resume|confirm   # live intervention (confirm = OK a costly request)

node web/server.js                # web UI → http://localhost:7878
```

## Layout

```
stackweft/   pure-stdlib Python engine (core infra · engine delivery · platform · report)
web/         dependency-free Node static server + single-file UI
skills/      capability library (one .md per requirement type; AI can draft, version, roll back)
integrations/ package StackWeft as a skill/subagent for Claude Code / Codex
assets/      README art (icon / banner)
```

## Config

- After `install.sh`, config and data live in `$STACKWEFT_HOME` (default `~/.stackweft`): `secrets.env` (gateway URL + key), `data/` (SQLite), `logs/`.
- Tiered model routing — pick the cheapest sufficient model, fall back on error; pure `urllib` to the gateway, no third‑party runtime SDK.
- Natural language first, default 中文 (`STACKWEFT_LANG`).

## Credits

Thanks to **ByteDance** for supporting this project.

Avatars are generated with **[DiceBear](https://www.dicebear.com/)** (`thumbs` style), bundled offline in `web/assets/avatar.js`.

## License

[MIT](LICENSE) © 2026 Loping151
