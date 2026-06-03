---
name: stackweft-delivery
description: >-
  Delegate full-stack field/feature delivery to StackWeft — a compiled,
  evidence-gated pipeline for React + Express + Sequelize repos. Use when the
  user wants to ADD or EXTEND a cross-stack field (Sequelize model → controller
  / API payload → frontend service → list-card AND detail-page DOM) and have it
  verified by counterexample sentinel probes. Cheaper and steadier than doing it
  yourself in a free agentic loop, and it remembers repeated work (0-LLM replay).
  Do NOT use for non-field work (algorithms, infra, cross-service refactors) or
  repos that don't fit this full-stack field-flow shape — handle those yourself.
---

# StackWeft delivery (delegated subagent)

You are the host agent. For a cross-stack **field/feature delivery** in a real
full-stack repo, do not hand-edit across the stack yourself — delegate the whole
job to StackWeft and report back its verified result.

Set `STACKWEFT_HOME` to this repo's absolute path (or edit the paths below).

## Steps

1. **Delegate.** Pass the user's requirement verbatim (natural language is fine):

   ```bash
   "$STACKWEFT_HOME/sw" run "<the user's requirement>" --repo "<abs repo path>"
   ```

   - `--repo` is optional; without it StackWeft resolves the workspace from the
     requirement text (and initializes a git baseline for non-git folders).
   - The command prints `run_id=…` near the top. Exit code **0 = all gates passed**,
     **1 = not passed**.

2. **Collect the structured result** and base your report on it (do not re-verify
   by hand — the probes already did):

   ```bash
   "$STACKWEFT_HOME/sw" json --debug
   ```

   This emits one JSON object: stages (clarify→plan→localize→compile→generate→
   verify→pr), per-call tokens, pass/fail per gate, the branch, and PR info.

3. **Report** to the user: what field landed, that the sentinel probes are green
   (value reaches model + API payload + list/detail DOM), the branch/PR, and the
   token cost. If exit code was 1, surface which gate failed from the JSON instead
   of claiming success.

## Run-time control (optional)

A delivery is pausable/resumable; you generally don't need this, but:

```bash
"$STACKWEFT_HOME/sw" control <run_id> pause|abort|append|resume ["append text"]
```

## Don't

- Don't fabricate a `run_id`, token count, or "passed" — only report values you
  actually read from `sw json` this turn.
- Don't fall back to editing files yourself unless StackWeft returns failure AND
  the user asks you to take over.
