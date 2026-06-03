"""Skill registry — the abstraction that keeps the trunk generic.

A new requirement *type* = add one Skill .md file under ``skills/``, no trunk
change. Frontmatter: ``name``, ``match`` ([keywords], lowercased substring),
``priority`` (int). Body: ``## clarify|plan|generate|verify`` sections (any
subset). Stdlib-only parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from stackweft.core.config import STACKWEFT_HOME

# Two sources, merged: bundled skills ship WITH the engine (a capability library, part
# of the code — always present so the field-flow can engage), and user / AI-drafted
# skills live under $STACKWEFT_HOME/skills (writable; skillsmith writes here). The user
# dir overrides bundled by name. Earlier this only pointed at $STACKWEFT_HOME/skills, so
# after the install.sh move to ~/.stackweft the bundled skills went missing → every
# requirement fell back to generic and the Field Flow Graph never engaged.
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
SKILLS_DIR = STACKWEFT_HOME / "skills"  # user-writable (skillsmith target)
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_SECTION_RE = re.compile(r"^##\s+(\w+)\s*$", re.MULTILINE)


@dataclass
class Skill:
    name: str
    match: list[str]
    priority: int
    sections: dict[str, str] = field(default_factory=dict)
    path: str = ""
    repo_id: str = ""   # set on auto-profiled skills → only eligible for THAT repo

    def guidance(self, stage: str) -> str:
        return self.sections.get(stage, "").strip()

    def score(self, requirement: str) -> int:
        r = requirement.lower()
        return sum(1 for kw in self.match if kw.lower() in r)


def _parse_frontmatter(block: str) -> dict[str, object]:
    out: dict[str, object] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            out[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        elif v.isdigit():
            out[k] = int(v)
        else:
            out[k] = v.strip("'\"")
    return out


def _parse_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(body))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[m.group(1).lower()] = body[start:end].strip()
    return sections


def load_skill_file(path: Path) -> Skill | None:
    m = _FM_RE.match(path.read_text(encoding="utf-8"))
    if not m:
        return None
    fm = _parse_frontmatter(m.group(1))
    return Skill(name=str(fm.get("name", path.stem)),
                 match=list(fm.get("match", []) or []),  # type: ignore[arg-type]
                 priority=int(fm.get("priority", 0) or 0),  # type: ignore[arg-type]
                 sections=_parse_sections(m.group(2)), path=str(path),
                 repo_id=str(fm.get("repo_id", "") or ""))


def load_all() -> list[Skill]:
    by_name: dict[str, Skill] = {}
    for d in (BUNDLED_SKILLS_DIR, SKILLS_DIR):  # user dir last → overrides bundled by name
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            sk = load_skill_file(p)
            if sk:
                by_name[sk.name] = sk
    return list(by_name.values())


FALLBACK = Skill(
    name="generic-fullstack-change", match=[], priority=-1,
    sections={
        "clarify": ("Turn the requirement into a precise spec: what user-visible "
                    "behaviour changes, which layer(s), and the acceptance check. "
                    "List explicit assumptions for anything ambiguous."),
        "plan": ("Identify the minimal set of files to touch across the stack. Keep "
                 "backend/frontend consistent. Plan at least one automated test."),
        "generate": ("Make the smallest correct change, follow existing style, and "
                     "add an automated test that fails before and passes after."),
        "verify": "Run lint and the test you added; both must pass.",
    }, path="(builtin)")


def select(requirement: str, repo_id: str = "") -> Skill:
    # A repo-scoped (auto-profiled) skill is eligible ONLY for its own repo, so two repos'
    # profiled skills never compete by keyword. Bundled/hand-written skills (no repo_id)
    # are always eligible.
    cands = [sk for sk in load_all() if not sk.repo_id or sk.repo_id == repo_id]
    scored = [(sk.score(requirement), sk.priority, sk) for sk in cands]
    scored = [t for t in scored if t[0] > 0]
    if not scored:
        return FALLBACK
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]
