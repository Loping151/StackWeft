"""Governance — a permission model with teeth.

Two pieces:
1. A capability model: org roles (Employee/Supervisor/CEO/Secretary/HR/Liaison)
   and project roles (Viewer/PM/Developer/Reviewer/Maintainer/Owner) → capability
   sets. Code checks capabilities, never `if role == 'ceo'` scattered around.
2. A **ToolBroker**: classifies every tool call and gates dangerous actions
   (git push / PR, dependency install, network egress, destructive fs, secret
   reads) — APPROVE-gated or DENIED — while allowing the safe dev set (read,
   sandbox edit, test, local commit, grep). Designed deny-list-style so it never
   blocks the verified green flow.

Stdlib only. The ToolBroker is enforced inside ``tools.dispatch``.
"""

from __future__ import annotations

import os
import re
import time

from stackweft.core import obs

# ── capability model (data, not scattered role checks) ──────────────────────
CAPABILITIES = [
    "project.run.start", "project.run.approve_taskir", "repo.read",
    "repo.write.sandbox", "test.run", "repo.commit.local", "repo.pr.create",
    "tool.install_deps", "tool.network", "tool.destructive",
    "model.use.basic", "model.use.reasoning", "model.use.expensive",
    "debug.view.raw_llm", "approval.resolve", "credential.bind_provider",
    "project.admin",
]

PROJECT_ROLE_CAPS: dict[str, set[str]] = {
    "viewer": {"repo.read", "debug.view.raw_llm"},
    "pm": {"repo.read", "project.run.start", "project.run.approve_taskir",
           "model.use.basic", "debug.view.raw_llm"},
    "developer": {"repo.read", "repo.write.sandbox", "test.run", "repo.commit.local",
                  "project.run.start", "model.use.basic", "model.use.reasoning",
                  "debug.view.raw_llm"},
    "reviewer": {"repo.read", "test.run", "approval.resolve", "debug.view.raw_llm"},
    "maintainer": {"repo.read", "repo.write.sandbox", "test.run", "repo.commit.local",
                   "repo.pr.create", "approval.resolve", "project.run.start",
                   "model.use.basic", "model.use.reasoning", "debug.view.raw_llm"},
    "owner": set(CAPABILITIES),
}

# Org roles mainly gate org-level actions; project role drives delivery caps.
ORG_ROLE_EXTRA: dict[str, set[str]] = {
    "employee": {"repo.write.sandbox", "test.run", "repo.commit.local"},
    "supervisor": {"repo.pr.create", "approval.resolve", "model.use.expensive"},
    "ceo": {"project.admin", "credential.bind_provider"},
    "secretary": set(), "hr": {"approval.resolve", "credential.bind_provider"},
    "liaison": {"project.run.start", "project.run.approve_taskir"},
}


def capabilities(*, project_role: str = "developer", org_role: str = "employee") -> set[str]:
    return PROJECT_ROLE_CAPS.get(project_role, set()) | ORG_ROLE_EXTRA.get(org_role, set())


def can(capability: str, *, project_role: str | None = None, org_role: str | None = None) -> bool:
    project_role = project_role or os.environ.get("STACKWEFT_PROJECT_ROLE", "developer")
    org_role = org_role or os.environ.get("STACKWEFT_ORG_ROLE", "employee")
    return capability in capabilities(project_role=project_role, org_role=org_role)


# ── ToolBroker: classify + gate ─────────────────────────────────────────────
_SECRET_PATH = re.compile(r"(^|/)(\.env|secrets?\.env|\.glm\.sh|\.kimi\.sh|id_rsa|"
                          r"\.ssh/|\.aws/|credentials|\.netrc|\.git-credentials)", re.I)
_DANGEROUS_SHELL = [
    (re.compile(r"\b(npm|pnpm|yarn)\s+(install|i|add)\b"), "tool.install_deps", "dependency install"),
    (re.compile(r"\bgit\s+push\b"), "repo.pr.create", "git push"),
    (re.compile(r"\b(gh\s+pr\s+create|git\s+request-pull)\b"), "repo.pr.create", "open PR"),
    (re.compile(r"\b(curl|wget|nc|ncat|ssh|scp)\b"), "tool.network", "network egress"),
    (re.compile(r"\brm\s+-rf?\b|\bgit\s+clean\s+-[a-z]*f|\bgit\s+reset\s+--hard\b"), "tool.destructive", "destructive fs/git"),
    (re.compile(r"(^|[;&|])\s*:\s*\(\)\s*\{|\bmkfs\b|\bdd\s+if="), "tool.destructive", "system-destructive"),
]


def classify_tool(name: str, args: dict) -> tuple[str, str]:
    """Return (capability_required, action_label) for a tool call."""
    if name in ("read_file", "read_window"):
        p = str(args.get("path", ""))
        if _SECRET_PATH.search(p):
            return "credential.bind_provider", f"read secret-like path {p}"
        return "repo.read", "read"
    if name in ("write_file", "edit_file", "multi_edit"):
        return "repo.write.sandbox", "sandbox edit"
    if name == "grep":
        return "repo.read", "grep"
    if name == "list_dir":
        return "repo.read", "list"
    if name == "run_shell":
        cmd = str(args.get("command", ""))
        for rx, cap, label in _DANGEROUS_SHELL:
            if rx.search(cmd):
                return cap, label
        if re.search(r"\bgit\s+commit\b", cmd):
            return "repo.commit.local", "local commit"
        if re.search(r"\b(npm\s+test|npx\s+vitest|npx\s+eslint|vitest|eslint)\b", cmd):
            return "test.run", "run tests/lint"
        return "repo.read", "shell (read-only class)"
    return "repo.read", name


def authorize(name: str, args: dict, *, project_role: str | None = None,
              org_role: str | None = None) -> tuple[bool, str, str]:
    """(allowed, capability, reason). Enforced in tools.dispatch. Secret reads and
    capabilities the role lacks are DENIED; everything in the safe dev set passes."""
    cap, label = classify_tool(name, args)
    if cap == "credential.bind_provider" and "secret-like" in label:
        return False, cap, f"policy: refusing to {label}"
    if can(cap, project_role=project_role, org_role=org_role):
        return True, cap, label
    return False, cap, f"role lacks capability {cap} for {label}"


# ── approval modes (Claude-Code-style human-in-the-loop) ─────────────────────
# allow-all : run everything (secret reads still hard-denied). DEFAULT.
# auto      : role-based allow/deny (the ToolBroker capability check).
# allow-edit: auto-approve reads + file edits; ASK before commands / dangerous.
# always-ask: auto-approve reads only; ASK before edits / commands / dangerous.
_MODES = ("allow-all", "auto", "allow-edit", "always-ask")
APPROVAL_TIMEOUT_S = int(os.environ.get("STACKWEFT_APPROVAL_TIMEOUT", "900"))


def approval_mode() -> str:
    m = obs.get_setting("approval_mode", os.environ.get("STACKWEFT_APPROVAL_MODE", "allow-all"))
    return m if m in _MODES else "allow-all"


def set_approval_mode(mode: str) -> bool:
    if mode not in _MODES:
        return False
    obs.set_setting("approval_mode", mode)
    return True


def risk_kind(cap: str, label: str) -> str:
    """Coarse risk class used by the approval gate."""
    if cap == "credential.bind_provider":
        return "secret"  # always denied
    if cap in ("tool.install_deps", "repo.pr.create", "tool.network", "tool.destructive"):
        return "dangerous"
    if cap in ("repo.commit.local", "test.run"):
        return "command"
    if cap == "repo.write.sandbox":
        return "edit"
    return "read"


def _decision(mode: str, kind: str) -> str:
    """Return 'allow' | 'ask' | 'deny' | 'auto' for (mode, risk kind)."""
    if kind == "secret":
        return "deny"
    if mode == "allow-all":
        return "allow"
    if mode == "auto":
        return "auto"
    if mode == "allow-edit":
        return "allow" if kind in ("read", "edit") else "ask"
    if mode == "always-ask":
        return "allow" if kind == "read" else "ask"
    return "allow"


def gate_action(run_id: str | None, kind: str, label: str, detail: str = "",
                name: str | None = None, args: dict | None = None) -> tuple[bool, str]:
    """The execution gate. Returns (allowed, reason). For 'ask' it BLOCKS, recording a
    pending approval and polling for the user's decision (DB = IPC; the web/phone UIs
    both surface it and either can approve — same backend state, so they stay synced)."""
    mode = approval_mode()
    d = _decision(mode, kind)
    if d == "deny":
        return False, f"policy: refusing to {label}"
    if d == "auto":  # role-based capability check
        if name is not None:
            ok, _, reason = authorize(name, args or {})
            return ok, ("" if ok else reason)
        return True, ""
    if d == "allow":
        return True, ""
    # d == "ask": no run context to surface it → fail open (allow) so we never wedge.
    if not run_id:
        return True, ""
    obs.set_approval_pending(run_id, kind, label, detail)
    obs.update_run(run_id, status="awaiting_approval")
    deadline = time.time() + APPROVAL_TIMEOUT_S
    while time.time() < deadline:
        a = obs.get_approval(run_id)
        if a and a.get("decision") == "approved":
            obs.clear_approval(run_id); obs.update_run(run_id, status="running")
            return True, ""
        if a and a.get("decision") == "denied":
            obs.clear_approval(run_id); obs.update_run(run_id, status="running")
            return False, f"user denied: {label}"
        ctl = obs.get_control(run_id)
        if ctl and ctl.get("action") == "abort":
            obs.clear_approval(run_id)
            return False, f"aborted while awaiting approval: {label}"
        time.sleep(2)
    obs.clear_approval(run_id); obs.update_run(run_id, status="running")
    return False, f"approval timed out ({APPROVAL_TIMEOUT_S}s): {label}"
