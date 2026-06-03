"""Project registry — resolve a requirement to a workspace without the user
naming a codebase.

Everything (runs, recipes, permissions, fingerprints) keys off a stable repo
identity. A repo fingerprint (git HEAD + tracked-tree hash) tells "same surface"
from "drifted" for the recipe hot path. The registry starts empty and
auto-registers a repo the first time it is used.

Stdlib + the shared SQLite DB.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from stackweft.core import obs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace (id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS project (
  id TEXT PRIMARY KEY, workspace_id TEXT, name TEXT, aliases_json TEXT,
  repo_path TEXT, default_branch TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS project_member (project_id TEXT, user_id TEXT, project_role TEXT);
CREATE TABLE IF NOT EXISTS repo_snapshot (
  id TEXT PRIMARY KEY, project_id TEXT, tree_hash TEXT, head_sha TEXT, created_at INTEGER
);
"""

def _ensure(conn) -> None:
    conn.executescript(_SCHEMA)
    if not conn.execute("SELECT 1 FROM workspace LIMIT 1").fetchone():
        conn.execute("INSERT INTO workspace(id,name) VALUES(?,?)", ("default", "Default Workspace"))


def list_projects() -> list[dict]:
    with obs.connect() as conn:
        _ensure(conn)
        return [dict(r) for r in conn.execute(
            "SELECT id,name,aliases_json,repo_path,default_branch FROM project ORDER BY name")]


def add(project_id: str, name: str, repo_path: str, aliases: list[str] | None = None,
        default_branch: str = "main") -> dict:
    """Register a Project so the platform owns the repo (user need not pass --repo)."""
    import os as _os
    rp = _os.path.abspath(_os.path.expanduser(repo_path))
    if not _os.path.isdir(rp):
        raise ValueError(f"repo path is not a directory: {rp}")
    with obs.connect() as conn:
        _ensure(conn)
        conn.execute("INSERT OR REPLACE INTO project(id,workspace_id,name,aliases_json,repo_path,default_branch,created_at) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (project_id, "default", name, json.dumps(aliases or []), rp, default_branch, obs.now_ms()))
        if not conn.execute("SELECT 1 FROM project_member WHERE project_id=?", (project_id,)).fetchone():
            conn.execute("INSERT INTO project_member(project_id,user_id,project_role) VALUES(?,?,?)",
                         (project_id, "default", "owner"))
    return {"id": project_id, "name": name, "repo_path": rp, "aliases": aliases or []}


def resolve(requirement: str) -> dict | None:
    """Pick the project for a requirement: alias keyword hit, else the sole project,
    else None (ambiguous → the UI should ask)."""
    projs = list_projects()
    if not projs:
        return None
    rl = requirement.lower()
    for p in projs:
        aliases = json.loads(p.get("aliases_json") or "[]")
        if any(a.lower() in rl for a in aliases) or p["name"].lower() in rl:
            return p
    return projs[0] if len(projs) == 1 else None


# ── git identity / non-git handling ──────────────────────────────────────────

def is_git_repo(repo_path: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(Path(repo_path).expanduser()),
                            "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:  # noqa: BLE001
        return False


def repo_identity(repo_path: str) -> str:
    """STABLE repo id — clone-stable and commit-stable, unlike the directory
    basename (which collides across repos named app/frontend/…). Uses the git
    ROOT commit; falls back to an absolute-path hash for a non-git workspace."""
    root = Path(repo_path).expanduser()
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-list", "--max-parents=0", "HEAD"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        first = out.splitlines()[0].strip() if out else ""
        if first:
            return "git:" + first[:16]
    except Exception:  # noqa: BLE001
        pass
    return "path:" + hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:16]


def ensure_git_baseline(repo_path: str) -> bool:
    """Make a non-git folder into a usable workspace so the pipeline (branch /
    commit / diff report / PR) works. Returns True if it had to initialise.
    No-op when already a git repo. Handles the empty-repo (no commits) case too."""
    root = Path(repo_path).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"workspace path does not exist: {root}")

    def _git(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=60)

    inited = False
    if not is_git_repo(str(root)):
        _git("init", "-q")
        inited = True
    # ensure at least one commit exists (branching needs a base)
    has_head = _git("rev-parse", "--verify", "-q", "HEAD").returncode == 0
    if not has_head:
        _git("add", "-A")
        _git("-c", "user.email=stackweft@local", "-c", "user.name=StackWeft",
             "commit", "-q", "--allow-empty", "-m", "StackWeft baseline")
        inited = True
    return inited


def _augment(proj: dict | None) -> dict | None:
    if not proj:
        return None
    proj = dict(proj)
    proj["is_git"] = is_git_repo(proj["repo_path"])
    proj["repo_id"] = repo_identity(proj["repo_path"])
    return proj


def _ensure_registered(repo_path: str) -> dict:
    """Find the project for a concrete path, or auto-register one (so a user can
    point at any folder in natural language without pre-registering it)."""
    rp = str(Path(repo_path).expanduser().resolve())
    for p in list_projects():
        if str(Path(p["repo_path"]).expanduser().resolve()) == rp:
            return p
    pid = "ws-" + hashlib.sha1(rp.encode()).hexdigest()[:8]
    name = Path(rp).name or pid
    return add(pid, name, rp, aliases=[name])


_PATH_RE = re.compile(r"(~?/[^\s'\"，。：；、）)]+)")


def _llm_pick_workspace(requirement: str, projs: list[dict]) -> str | None:
    """LLM confirms the target workspace: returns a project_id from the catalog,
    or an explicit directory path the requirement names, or None if unclear."""
    try:
        from stackweft.core import llm
        from stackweft.core.utils import json_extract
    except Exception:  # noqa: BLE001
        return None
    catalog = "\n".join(
        f"- project_id={p['id']} | name={p['name']} | aliases="
        f"{json.loads(p.get('aliases_json') or '[]')} | path={p['repo_path']}" for p in projs)
    system = ("You map a software delivery requirement to the target workspace. "
              'Reply STRICT JSON {"project_id": string|null, "workspace_path": string|null}. '
              "ALWAYS choose the single best-matching project_id from the catalog — match on "
              "domain/topic, not just literal aliases (e.g. an order/checkout requirement → a "
              "shop project). Only set workspace_path (and project_id null) if the requirement "
              "explicitly names a filesystem directory NOT in the catalog. Set BOTH null ONLY "
              "if no listed project could plausibly own this requirement.")
    user = f"Requirement:\n{requirement}\n\nKnown workspaces:\n{catalog}\n\nJSON only."
    try:
        res = llm.messages(system=system, msgs=[{"role": "user", "content": user}],
                           level=0.6, stage="intake", purpose="resolve_workspace", max_tokens=300)
        j = json_extract(res.text)
        return j.get("project_id") or j.get("workspace_path") or None
    except Exception:  # noqa: BLE001
        return None


def resolve_workspace(requirement: str, *, allow_llm: bool = True,
                      default_cwd: str | None = None) -> dict | None:
    """Resolve a requirement to a workspace (non-git-aware).
    Order: explicit existing path in the text → registry alias → (optional) LLM
    confirm → the current working directory. Returns an _augment()'d dict (with
    is_git + repo_id) or None. ``default_cwd`` is the fallback target repo."""
    # 1) an explicit directory path written in the requirement
    for m in _PATH_RE.finditer(requirement):
        p = Path(m.group(1)).expanduser()
        if p.is_dir():
            return _augment(_ensure_registered(str(p)))
    # 2) deterministic alias / name match against already-registered repos
    hit = resolve(requirement)
    if hit:
        return _augment(hit)
    # 3) let the LLM confirm among already-registered repos, if any
    projs = list_projects()
    if allow_llm and projs:
        pick = _llm_pick_workspace(requirement, projs)
        if pick:
            by_id = next((p for p in projs if p["id"] == pick), None)
            if by_id:
                return _augment(by_id)
            pp = Path(str(pick)).expanduser()
            if pp.is_dir():
                return _augment(_ensure_registered(str(pp)))
    # 4) default: the current working directory
    if default_cwd and Path(default_cwd).expanduser().is_dir():
        return _augment(_ensure_registered(str(Path(default_cwd).expanduser())))
    return None


def fingerprint(repo_path: str) -> dict:
    """Cheap repo surface fingerprint: git HEAD sha + hash of the tracked file list.
    Used to distinguish 'same surface' (recipe hot path safe) from drift."""
    root = Path(repo_path)
    def _git(*a):
        try:
            return subprocess.run(["git", "-C", str(root), *a], capture_output=True,
                                  text=True, timeout=30).stdout.strip()
        except Exception:
            return ""
    head = _git("rev-parse", "HEAD")
    tree = _git("ls-files")
    th = hashlib.sha1(tree.encode()).hexdigest()[:16]
    return {"head_sha": head[:12], "tree_hash": th}


def record_snapshot(project_id: str, repo_path: str) -> dict:
    fp = fingerprint(repo_path)
    with obs.connect() as conn:
        _ensure(conn)
        conn.execute("INSERT INTO repo_snapshot(id,project_id,tree_hash,head_sha,created_at) "
                     "VALUES(?,?,?,?,?)",
                     (obs.new_id(), project_id, fp["tree_hash"], fp["head_sha"], obs.now_ms()))
    return fp
