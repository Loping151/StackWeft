# StackWeft Delivery Benchmark

A reproducible, **model-held-constant** comparison of two delivery *methods* on the
same multi-round requirement set, same repo, scored by the **same** verifier.

## What it compares

| arm | method | model |
|---|---|---|
| **StackWeft** | StackWeft's compiled Field-Flow pipeline (compile→slots→sentinel probes) + WeftRecipe reuse | GLM (reason) + kimi (exec), as routed |
| **Claude Code** | a Claude-Code-style agentic loop on the same model: same tools (read/grep/edit/run), no field-flow, no recipe ("glm-as-claude") | GLM (0.8) |

The point is to isolate the **methodology**, not the model. Claude Code here is a real,
fair agent: identical sandbox tools, identical requirement text, the strong model.

## Fair scoring (same for both arms)

After each arm produces its change, the **same counterexample sentinel probes** are
dropped in and run — the field's value must reach (1) the Sequelize model, (2) the
API payload (`setArticle`), and (3) the list-card **and** detail-page DOM. A run is
`passed` only if backend + frontend probes are green. The probes check the *value
reaching the DOM/payload* (e.g. `img[src=PROBE]` / text content), not any
StackWeft-specific class — so any correct wiring passes, whoever produced it.

Each task **resets the repo to the clean scaffold** (`58634fd`) first, so rounds are
independent except for StackWeft's cross-run WeftRecipe memory (its "越做越快" — a
repeated field in a later round should show the warm 0-LLM path; Claude Code has no
such memory, so it pays full cost every time).

## Metrics

Per task and per arm: `passed`, `calls` (LLM calls), `tokens_in/out/total`, wall `secs`.
Token accounting is StackWeft's per-call ledger, which records the **gateway's reported
usage** for every call (both arms log to the same `llm_calls` table) — so the two arms
are measured identically.

### Ground-truth via the New-API site (optional)
Pass `--key-weft <k>` / `--key-base <k>` to route each arm through a **distinct gateway
key**; then the New-API admin site's usage log shows the per-arm token total
independently (cross-check of the local ledger). Creating those keys requires New-API
admin auth (see the run notes); never touches other users' data.

## Reproduce

```bash
# default rounds: subtitle(text), coverImage(image), summary(text), subtitle(repeat→warm)
python3 bench/run_bench.py
# custom rounds / single arm:
python3 bench/run_bench.py --fields subtitle:text coverImage:image --arms weft baseline
```
Output: a per-round table on stdout + `bench/results.json` (full per-task records + per-arm totals).

## Results (2026-06-04, uniform GLM-5.1 — fair)

**Both arms run the same single model, GLM-5.1, at every stage** (`--model GLM-5.1`, so
the weft pipeline's `generate` no longer drops to the cheaper exec-tier model — it pays
GLM's reasoning cost just like Claude Code). This is the model-held-constant, apples-
to-apples version. Rounds: `subtitle`(text) → `coverImage`(image) → `subtitle`(repeat).
Both arms routed through dedicated New-API keys (`weftbench-A`=weft, `weftbench-B`=base).
Raw records in `bench/results_glm.json`.

| method (all glm-5.1) | pass | total tok | input | output | LLM calls | LLM time* | wall |
|---|---|---|---|---|---|---|---|
| **StackWeft** (compiled pipeline) | **3/3** | **19,655** | 8,933 | 10,722 | 15 | **229 s** | 260 s |
| **Claude Code** | 3/3 | **309,391** | 294,946 | 14,445 | 53 | 405 s | 416 s |

\*LLM time = sum of *successful* LLM-call latency (`SUM(latency_ms) WHERE status='ok'`),
excluding subprocess startup, file IO, test runs, and gateway stalls/retries. It's the
fair time metric — wall-clock is noisy because both arms hit the same shared gateway.

### Session / memory policy
Every round is an **independent session** for both arms — a fresh context (the coding
agent starts from an empty conversation each round; nothing carries over) and the repo is
**reset to the clean scaffold** (`58634fd`) first. The *only* thing that persists across
rounds is StackWeft's WeftRecipe memory, which lives in **SQLite, not in a conversation**.
That is the whole point: Claude Code's "memory" only lives inside one session, so a new
session (the realistic "new task / new day" case) starts cold and pays full cost every
time; StackWeft's reuse survives across separate runs because it's in the DB. Round 3
(`subtitle` repeated) is where this shows — StackWeft hot-replays the recipe and drops to
5,890 tokens, while Claude Code pays its usual ~91k.

**Same 3 verified changes, same model — StackWeft used ~1/15.7 the tokens** (19.7k vs
309k) and ~1.8× less LLM time. Both arms passed the identical sentinel-probe scorer, so
this is purely methodology efficiency, not correctness: Claude Code reaches the same
result but burns ~16× the tokens (long agentic context + GLM reasoning per step), highly
variable (91k–124k/round, up to 24 calls), while the compiled pipeline is steady
(~6.5k/round, 5 calls — compile is read-only grep, generate fills verified slots one-shot,
the repeat round replays the WeftRecipe so several slots are 0-LLM).

Per round (input / output / total tokens · LLM calls):

| round | StackWeft | Claude Code |
|---|---|---|
| 1 subtitle (text) | 2,850 / 3,906 / 6,756 · 5 | 115,984 / 7,594 / 123,578 · 5 |
| 2 coverImage (image) | 3,275 / 3,734 / 7,009 · 5 | 90,838 / 3,535 / 94,373 · 24 |
| 3 subtitle (repeat, recipe hot-replay) | 2,808 / 3,082 / **5,890** · 5 | 88,124 / 3,316 / 91,440 · 24 |

### Why 15.7× tokens but only ~1.8× time (it's not parallelism)
The pipeline runs **serially** — no concurrent subagents (slots loop in `for n in nodes`).
The token-vs-time decoupling is an input/output asymmetry:

| method | input tok | output tok | out share | calls | LLM time |
|---|---|---|---|---|---|
| StackWeft | 8,933 | 10,722 | 55 % | 15 | 229 s |
| Claude Code | **294,946** | 14,445 | **4.7 %** | 53 | 405 s |

The 15.7× token gap is almost entirely **input** (295k vs 9k = 33×), while **output tokens
are comparable** (14.4k vs 10.7k = 1.35×). Prompt input is prefilled in parallel on the
GPU — fast in time, full price in billing — so Claude Code re-feeding its giant agentic
context every round explodes tokens without exploding time. The genuinely time-expensive
work is *output decode* (serial, per-token) plus *round-trip count*: output is similar
across arms, so the time gap is driven mainly by 53 vs 15 round-trips. Net: tokens are
dominated by cheap-to-prefill input context; time by output decode + call count — the two
are decoupled.

### Ground-truth cross-check (New-API site)
The New-API `logs` table, summed per token (post-run minus the pre-run baseline of this
bench), matches the local `obs` ledger **to the token**:
StackWeft 41,550 − 21,895 = **19,655**; Claude Code 577,517 − 268,126 = **309,391** (call
delta 53, also exact). So the local accounting equals the gateway's billed usage.

> The earlier 2026-06-03 run (weft 21,458 vs baseline 268,118, ~12.5×) used the default
> routing where weft's `generate` ran on the cheaper exec-tier model. Holding the model
> constant (this run) actually *widens* the gap to ~15.7×, because Claude Code's extra
> tokens are reasoning-heavy GLM tokens, while StackWeft's structure caps how much
> context ever reaches the model.

### A different repo (mk48, 2026-06-05, uniform GLM-5.1)

The same StackWeft-vs-Claude Code comparison, run on a structurally different repo:
`mk48/fullstack-crud-react-node-postgresql` — `server/`+`webclient/` roots, `.js`
components, `sequelize.define` + object-form fields, **config/reducer UI**, entity
`Product` / table `product`. Task: add a `subtitle` field to Product, threaded across the
stack. Harness: `bench/run_repo_compare.py --repo <r> --entity Product --field subtitle
--model GLM-5.1`.

**Scoring = static cross-stack coverage** (the shallow clone has no test runner): the new
field must appear in the api-layer model + an api write path + a web render, all
**layout-discovered** (not hardcoded dirs). Tokens from StackWeft's per-call ledger.

| method (glm-5.1) | tokens | calls | LLM time | cross-stack | field-flow engaged |
|---|---|---|---|---|---|
| **StackWeft** (cold, first encounter) | 40,412 | 14 | 250 s | 3/3 ✓ | ✓ |
| **StackWeft** (warm, recipe reuse) | **5,578** | 3 | 56 s | 3/3 ✓ | ✓ |
| **Claude Code** (glm-as-claude) | 158,005 | 9 | 127 s | 3/3 ✓ | — |

All three fully thread the field, so this is methodology efficiency, not correctness.
StackWeft threads `subtitle` in mk48's own idioms — object-form model
(`subtitle: { type: DataTypes.STRING }`), a reducer form (input +
`dispatch({type:"SUBTITLE_CHANGE"})`), the config list (`{ Header:"Subtitle",
accessor:"subtitle" }`), and the `state.subtitle` detail. ~4× fewer tokens cold, ~28×
warm; Claude Code re-reads the whole repo every run (147k of its 158k is input). Same
越用越省 here: first encounter 40.4k → 5.6k once the WeftRecipe is cached.

(mk48 is not green-verified — the shallow clone has no test infra — so it is scored by
static cross-stack coverage + diff inspection, stated honestly, not by a green run.)
