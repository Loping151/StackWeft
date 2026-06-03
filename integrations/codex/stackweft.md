# StackWeft delivery — delegation rule (Codex / AGENTS.md)

> Paste this block into your `AGENTS.md`, or drop it in `~/.codex/prompts/` as a
> custom prompt. Set `STACKWEFT_HOME` to StackWeft's absolute path first.

When the task is to **add or extend a cross-stack field / small feature** in a
React + Express + Sequelize repo (the change must reach the Sequelize model, the
controller / API payload, the frontend service, and both the list-card and
detail-page DOM), **delegate it to StackWeft** instead of editing across the stack
yourself. StackWeft compiles the cross-stack landing points, fills them, and gates
the result with counterexample sentinel probes — cheaper and steadier than a free
agentic loop, and it replays repeated work with 0 LLM calls.

Contract (two commands):

```bash
# Deliver — runs the full pipeline. Prints `run_id=…`. Exit 0 = passed, 1 = not passed.
"$STACKWEFT_HOME/sw" run "<the user's requirement, natural language>" --repo "<abs repo path>"

# Read the structured result (stages, tokens, pass/fail, branch, PR) and report from it.
"$STACKWEFT_HOME/sw" json --debug
```

`--repo` is optional (StackWeft resolves the workspace from the requirement text;
non-git folders get a git baseline automatically).

Report to the user from the JSON only: the field that landed, that the probes are
green, the branch/PR, and the token cost. If exit code is 1, say which gate failed
— never claim a pass you didn't read.

**Do not** delegate non-field work (algorithms, infra, cross-service refactors) or
repos that don't fit this full-stack field-flow shape to StackWeft — handle those
with your own tools.
