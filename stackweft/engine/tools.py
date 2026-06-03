"""Worker tools: filesystem + shell, sandboxed to one repo root.

All paths resolve under ``root``; escapes raise. ``run_shell`` runs with
``cwd=root`` and a wall-clock timeout. These are the single agent's hands for
editing a real repo and running its lint/tests — no mocks.

Each tool exposes an Anthropic tool schema (``SCHEMAS``) and a Python impl
(``dispatch``). Results are strings (what the model sees back).
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from collections import deque
from pathlib import Path
from typing import Any

MAX_READ_BYTES = 60_000
MAX_SHELL_OUTPUT = 20_000
_SKIP_DIRS = {".git", "node_modules", ".venv", "dist", "build", "coverage"}


class Sandbox:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"sandbox root is not a directory: {self.root}")

    def _resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"path escapes sandbox: {rel}")
        return p

    def read_file(self, path: str, max_bytes: int = MAX_READ_BYTES) -> str:
        p = self._resolve(path)
        if not p.is_file():
            return f"ERROR: not a file: {path}"
        data = p.read_text(encoding="utf-8", errors="replace")
        if len(data) > max_bytes:
            return data[:max_bytes] + f"\n…[truncated, {len(data)} bytes total]"
        return data

    def write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
        return f"{'overwrote' if existed else 'created'} {path} ({len(content)} bytes)"

    def edit_file(self, path: str, old: str, new: str) -> str:
        p = self._resolve(path)
        if not p.is_file():
            return f"ERROR: not a file: {path}"
        text = p.read_text(encoding="utf-8")
        n = text.count(old)
        if n == 0:
            return f"ERROR: old string not found in {path} (no change)"
        if n > 1:
            return f"ERROR: old string occurs {n}× in {path}; make it unique"
        p.write_text(text.replace(old, new), encoding="utf-8")
        return f"edited {path} (1 replacement)"

    def multi_edit(self, edits: list[dict[str, str]]) -> str:
        """Apply several (path, old, new) edits as one logical operation.

        Single-occurrence ``edit_file`` is brittle for cross-stack
        changes that touch several files. This validates EVERY hunk first (each
        ``old`` must occur exactly once in its file) and only then writes — so a
        backend+frontend+test change either lands fully or not at all, instead
        of leaving the repo half-migrated. Same-file edits are applied in order.
        """
        if not isinstance(edits, list) or not edits:
            return "ERROR: multi_edit needs a non-empty list of {path,old,new}"
        # Validate all hunks against an in-memory working copy first.
        working: dict[str, str] = {}
        for i, e in enumerate(edits):
            path, old, new = e.get("path"), e.get("old"), e.get("new")
            if not path or old is None or new is None:
                return f"ERROR: edit #{i} needs path, old, new"
            p = self._resolve(path)
            if path not in working:
                if not p.is_file():
                    return f"ERROR: edit #{i}: not a file: {path}"
                working[path] = p.read_text(encoding="utf-8")
            n = working[path].count(old)
            if n != 1:
                return (f"ERROR: edit #{i}: old occurs {n}× in {path} "
                        f"(must be exactly 1); no changes applied")
            working[path] = working[path].replace(old, new)
        # All valid → commit to disk.
        for path, content in working.items():
            self._resolve(path).write_text(content, encoding="utf-8")
        return f"applied {len(edits)} edit(s) across {len(working)} file(s)"

    def read_window(self, path: str, line_offset: int = 1, n_lines: int = 200) -> str:
        """Read a line window with 1-indexed ``cat -n`` numbers (windowed read).

        ``line_offset`` is 1-indexed; negative reads the last N lines (tail).
        This lets the worker read a focused window around an anchor instead of
        whole files — the single biggest lever against O(n²) context growth."""
        p = self._resolve(path)
        if not p.is_file():
            return f"ERROR: not a file: {path}"
        n_lines = max(1, min(n_lines, 600))
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        if line_offset < 0:
            window = list(deque(enumerate(lines, 1), maxlen=n_lines))[-n_lines:]
            start = window[0][0] if window else 1
            sel = [t for _, t in window]
            nums = list(range(start, start + len(sel)))
        else:
            start = max(1, line_offset)
            sel = lines[start - 1:start - 1 + n_lines]
            nums = list(range(start, start + len(sel)))
        body = "\n".join(f"{n:6d}\t{t[:2000]}" for n, t in zip(nums, sel))
        tail = "" if (nums and nums[-1] >= total) else f"\n…[{total - (nums[-1] if nums else 0)} more lines below; re-window if needed]"
        return f"({path}: lines {nums[0] if nums else 0}-{nums[-1] if nums else 0} of {total})\n{body}{tail}"

    def grep(self, pattern: str, glob: str | None = None,
             output_mode: str = "content", context: int = 0,
             head_limit: int = 200) -> str:
        """Regex search under the repo root (stdlib, skips node_modules/.git).

        output_mode: ``content`` (path:line:text + optional context),
        ``files`` (unique matching paths), ``count`` (path: N).
        ``glob`` filters by fnmatch on the repo-relative path (e.g. ``*.jsx``)."""
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"ERROR: bad regex: {e}"
        out: list[str] = []
        files_hit: list[str] = []
        counts: dict[str, int] = {}
        for fp in sorted(self.root.rglob("*")):
            if not fp.is_file():
                continue
            if any(part in _SKIP_DIRS for part in fp.parts):
                continue
            rel = str(fp.relative_to(self.root))
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(fp.name, glob)):
                continue
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:  # noqa: BLE001
                continue
            hit_lines = [i for i, ln in enumerate(lines) if rx.search(ln)]
            if not hit_lines:
                continue
            files_hit.append(rel)
            counts[rel] = len(hit_lines)
            if output_mode == "content":
                shown: set[int] = set()
                for i in hit_lines:
                    lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                    for j in range(lo, hi):
                        if j in shown:
                            continue
                        shown.add(j)
                        sep = ":" if j == i else "-"
                        out.append(f"{rel}{sep}{j+1}{sep}{lines[j][:2000]}")
                    if context:
                        out.append("--")
        if output_mode == "files":
            res = files_hit
        elif output_mode == "count":
            res = [f"{f}: {counts[f]}" for f in files_hit]
        else:
            res = out
        if not res:
            return f"(no matches for /{pattern}/" + (f" in {glob})" if glob else ")")
        clipped = res[:head_limit]
        more = "" if len(res) <= head_limit else f"\n…[{len(res)-head_limit} more; narrow the pattern/glob]"
        return "\n".join(clipped) + more

    def list_dir(self, path: str = ".") -> str:
        p = self._resolve(path)
        if not p.is_dir():
            return f"ERROR: not a dir: {path}"
        out = []
        for c in sorted(p.iterdir()):
            if c.name in {".git", "node_modules", ".venv"}:
                out.append(f"{c.name}/  [skipped]")
                continue
            out.append(f"{c.name}/" if c.is_dir() else c.name)
        return "\n".join(out) or "(empty)"

    def run_shell(self, command: str, timeout: int = 300) -> str:
        try:
            proc = subprocess.run(command, shell=True, cwd=self.root,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {timeout}s: {command}"
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        if len(out) > MAX_SHELL_OUTPUT:
            out = out[:MAX_SHELL_OUTPUT] + "\n…[output truncated]"
        return f"exit={proc.returncode}\n{out}"


SCHEMAS: list[dict[str, Any]] = [
    {"name": "read_file", "description": "Read a UTF-8 text file under the repo root.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Create or overwrite a file under the repo root.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file",
     "description": "Replace one unique occurrence of `old` with `new` in a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                                     "new": {"type": "string"}},
                      "required": ["path", "old", "new"]}},
    {"name": "read_window",
     "description": "Read a line window of a file with line numbers. Prefer this over "
                    "read_file: pass line_offset (1-indexed; negative = tail) and n_lines "
                    "(default 200) to read a focused window around an anchor, not the whole file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "line_offset": {"type": "integer"},
                                     "n_lines": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "grep",
     "description": "Regex search under the repo root (skips node_modules/.git). "
                    "output_mode: 'content' (path:line:text, with optional context lines), "
                    "'files' (paths only), or 'count'. glob filters by filename (e.g. '*.jsx').",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"},
                                     "glob": {"type": "string"},
                                     "output_mode": {"type": "string"},
                                     "context": {"type": "integer"},
                                     "head_limit": {"type": "integer"}},
                      "required": ["pattern"]}},
    {"name": "list_dir", "description": "List a directory (skips .git/node_modules).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": []}},
    {"name": "multi_edit",
     "description": "Apply several edits atomically across one or more files. "
                    "Each edit is {path, old, new}; every `old` must occur exactly "
                    "once in its file. Validates all hunks first, then writes — use "
                    "for cross-stack changes (backend+frontend+test together).",
     "input_schema": {"type": "object",
                      "properties": {"edits": {"type": "array", "items": {
                          "type": "object",
                          "properties": {"path": {"type": "string"},
                                         "old": {"type": "string"},
                                         "new": {"type": "string"}},
                          "required": ["path", "old", "new"]}}},
                      "required": ["edits"]}},
    {"name": "run_shell",
     "description": "Run a shell command (cwd=repo root) for lint, tests, git, grep, npm. "
                    "Returns exit code + combined stdout/stderr.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"},
                                     "timeout": {"type": "integer"}},
                      "required": ["command"]}},
]

# Read-only subset — for the general assistant (it explores but never mutates).
READONLY_TOOLS = {"read_file", "read_window", "grep", "list_dir"}
READONLY_SCHEMAS: list[dict[str, Any]] = [s for s in SCHEMAS if s["name"] in READONLY_TOOLS]


def dispatch(sandbox: Sandbox, name: str, args: dict[str, Any]) -> str:
    # Read-only mode (general assistant): hard-deny any mutation, regardless of catalog.
    if os.environ.get("STACKWEFT_READONLY") == "1" and name not in READONLY_TOOLS:
        return f"DENIED (read-only assistant): {name} is not allowed; you may only read/grep/list."
    # The ToolBroker gates dangerous actions (push/install/network/
    # destructive/secret-read) per the caller's capabilities; safe dev set passes.
    if os.environ.get("STACKWEFT_GOVERN", "1") == "1":
        try:
            from stackweft.platform import govern
            run_id = os.environ.get("STACKWEFT_RUN_ID") or None
            cap, label = govern.classify_tool(name, args)
            kind = govern.risk_kind(cap, label)
            detail = str(args.get("command") or args.get("path") or "")[:300]
            ok, reason = govern.gate_action(run_id, kind, label, detail=detail,
                                            name=name, args=args)  # approval-mode aware
            if not ok:
                return f"DENIED ({cap}): {reason}. Use an allowed action."
        except Exception:  # noqa: BLE001 — never let governance import break the tool
            pass
    try:
        if name == "read_file":
            return sandbox.read_file(args["path"])
        if name == "write_file":
            return sandbox.write_file(args["path"], args["content"])
        if name == "edit_file":
            return sandbox.edit_file(args["path"], args["old"], args["new"])
        if name == "multi_edit":
            return sandbox.multi_edit(args.get("edits", []))
        if name == "read_window":
            return sandbox.read_window(args["path"],
                                       int(args.get("line_offset", 1)),
                                       int(args.get("n_lines", 200)))
        if name == "grep":
            return sandbox.grep(args["pattern"], args.get("glob"),
                                args.get("output_mode", "content"),
                                int(args.get("context", 0)),
                                int(args.get("head_limit", 200)))
        if name == "list_dir":
            return sandbox.list_dir(args.get("path", "."))
        if name == "run_shell":
            return sandbox.run_shell(args["command"], int(args.get("timeout", 300)))
        return f"ERROR: unknown tool {name}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR running {name}: {e!r}"
