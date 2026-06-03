"""Repo layout — where the layers live, what extensions they use, how to test them.

The trunk used to hardcode ``backend``/``frontend`` dirs, ``*.js``/``*.jsx``, and
``npm test``. That pinned generality to one repo shape. This discovers the layout of
ANY repo deterministically (package.json locations + dependency classification) and the
engine consumes a ``Layout`` instead of literals.

Layer ``kind`` is the stable abstraction (``api`` ≈ backend, ``web`` ≈ frontend); the
engine reasons in kinds, so a repo can call its dirs ``server``/``webclient`` (or
anything) and cross-stack logic still works.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

_SKIP = {"node_modules", ".git", "dist", "build", ".cache", "coverage", ".next", "__pycache__"}
_SRC_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")
_API_DEPS = ("express", "koa", "fastify", "@nestjs/core", "sequelize", "prisma",
             "mongoose", "typeorm", "@prisma/client", "pg", "mysql2", "apollo-server")
_WEB_DEPS = ("react", "react-dom", "next", "vue", "svelte", "@sveltejs/kit",
             "@remix-run/react", "vite", "@vitejs/plugin-react")


@dataclass
class Layer:
    name: str            # display/dir name (backend, frontend, server, webclient, …)
    kind: str            # "api" | "web" | "db" | "other"
    roots: list[str]     # repo-relative dirs
    exts: list[str]      # source extensions present in this layer
    test_cmd: str = ""   # how to run this layer's tests (cd root && <cmd>)


@dataclass
class Layout:
    layers: list[Layer] = field(default_factory=list)
    root_test_cmd: str = ""   # workspace-root test (fallback when a layer has none)

    # — selectors —
    def by_kind(self, kind: str) -> list[Layer]:
        return [l for l in self.layers if l.kind == kind]

    def all_roots(self) -> list[str]:
        return [r for l in self.layers for r in l.roots]

    def all_exts(self) -> list[str]:
        seen: list[str] = []
        for l in self.layers:
            for e in l.exts:
                if e not in seen:
                    seen.append(e)
        return seen or list(_SRC_EXTS[:2])

    # — grep helpers (replace hardcoded --include + dir lists) —
    def include_args(self, exts: list[str] | None = None) -> str:
        return " ".join(f"--include='*{e}'" for e in (exts or self.all_exts()))

    def grep_scope(self, kind: str | None = None) -> str:
        roots = ([r for l in self.by_kind(kind) for r in l.roots] if kind
                 else self.all_roots())
        return " ".join(shlex.quote(r) for r in roots) or "."

    # — path → kind (replaces path.startswith("backend"/"frontend")) —
    def kind_of(self, path: str) -> str | None:
        p = str(path).replace("\\", "/").lstrip("./")
        best: tuple[int, str] | None = None
        for l in self.layers:
            for r in l.roots:
                r = r.strip("/")
                if r in (".", "") or p == r or p.startswith(r + "/"):
                    depth = len(r.split("/")) if r not in (".", "") else 0
                    if best is None or depth > best[0]:
                        best = (depth, l.kind)
        return best[1] if best else None

    def is_cross_stack(self, paths: list[str]) -> bool:
        kinds = {self.kind_of(p) for p in paths}
        return "api" in kinds and "web" in kinds

    # — tests —
    def test_cmds(self) -> list[tuple[str, str]]:
        out = []
        for l in self.layers:
            if l.test_cmd:
                out.append((l.roots[0] if l.roots else ".", l.test_cmd))
        return out


# ----------------------------------------------------------------------------- detection

def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _classify(pkg: dict, dir_path: Path) -> str:
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    has_web = any(d in deps for d in _WEB_DEPS)
    has_api = any(d in deps for d in _API_DEPS)
    # structural tie-breakers
    if not has_web and (dir_path / "src").is_dir() and any(
            (dir_path / "src").rglob("*.jsx")):
        has_web = True
    if not has_api and any((dir_path / s).is_dir() for s in ("models", "controllers", "routes")):
        has_api = True
    if has_web and not has_api:
        return "web"
    if has_api and not has_web:
        return "api"
    if has_web and has_api:  # combined → lean web if it has a UI src, else api
        return "web" if (dir_path / "src").is_dir() else "api"
    return "other"


def _exts_in(dir_path: Path) -> list[str]:
    found: list[str] = []
    n = 0
    for p in dir_path.rglob("*"):
        if n > 4000:
            break
        if any(s in p.parts for s in _SKIP) or not p.is_file():
            continue
        n += 1
        if p.suffix in _SRC_EXTS and p.suffix not in found:
            found.append(p.suffix)
    return found or [".js"]


def _test_cmd(pkg: dict, root_rel: str) -> str:
    t = (pkg.get("scripts", {}) or {}).get("test", "")
    if not t or "no test specified" in t:
        return ""
    cd = "" if root_rel in (".", "") else f"cd {shlex.quote(root_rel)} && "
    return f"{cd}npm test"


def _pkg_dirs(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("package.json"):
        if any(s in p.parts for s in _SKIP):
            continue
        if len(p.relative_to(root).parts) > 4:
            continue
        out.append(p.parent)
    return sorted(out, key=lambda d: len(d.parts))


def detect(root: Path) -> Layout:
    """Deterministic layout discovery. No LLM. Falls back to a single root layer."""
    pkg_dirs = _pkg_dirs(root)
    # keep the LEAF package.json dirs (drop a workspace root that is an ancestor of others)
    leaves = [d for d in pkg_dirs if not any(o != d and d in o.parents for o in pkg_dirs)]
    effective = leaves or pkg_dirs
    # workspace-root test command (fallback when a leaf layer declares no test script)
    root_test = ""
    workspace_roots = [d for d in pkg_dirs if d not in leaves] or ([root] if pkg_dirs else [])
    for d in workspace_roots:
        rt = _test_cmd(_read_json(d / "package.json"), str(d.relative_to(root)).replace("\\", "/"))
        if rt:
            root_test = rt
            break
    layers: list[Layer] = []
    for d in effective:
        rel = str(d.relative_to(root)).replace("\\", "/")
        rel = "" if rel == "." else rel
        pkg = _read_json(d / "package.json")
        layers.append(Layer(name=(rel or root.name), kind=_classify(pkg, d),
                            roots=[rel or "."], exts=_exts_in(d),
                            test_cmd=_test_cmd(pkg, rel or ".")))
    if not layers:
        layers = [Layer(name=root.name, kind="other", roots=["."], exts=_exts_in(root))]
    return Layout(layers=layers, root_test_cmd=root_test)


_DEFAULT = Layout(layers=[
    Layer("backend", "api", ["backend"], [".js"], "cd backend && npm test"),
    Layer("frontend", "web", ["frontend"], [".js", ".jsx"], "cd frontend && npm test"),
])


def for_sandbox(sb) -> Layout:
    """Layout for an engine run: detect from the repo; on any miss, the legacy
    backend/frontend default keeps existing repos working unchanged."""
    try:
        lay = detect(sb.root)
        # if detection found at least one api/web layer, use it; else fall back
        if any(l.kind in ("api", "web") for l in lay.layers):
            return lay
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT
