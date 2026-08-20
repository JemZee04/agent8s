from __future__ import annotations

from pathlib import Path

GITIGNORE = """\
.DS_Store
__pycache__/
*.pyc
node_modules/
.env
"""


def claude_md(name: str, description: str) -> str:
    return f"""\
# {name}

{description}

## Stack

Not established yet — this project was scaffolded empty. Whichever agent
picks up the first real task here should fill this section in (language,
framework, package manager) once it's clear from what gets built.

## Commands

- Tests: TBD
- Lint: TBD
- Build/run: TBD

## Conventions

None recorded yet.
"""


def readme_md(name: str, description: str) -> str:
    return f"# {name}\n\n{description}\n"


def write_skeleton(path: Path, name: str, description: str) -> None:
    (path / "README.md").write_text(readme_md(name, description))
    (path / "CLAUDE.md").write_text(claude_md(name, description))
    (path / ".gitignore").write_text(GITIGNORE)
