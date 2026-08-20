from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}:\n{result.stderr}")
    return result.stdout


def default_branch(project_path: Path) -> str:
    try:
        out = _run(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], project_path)
        return out.strip().removeprefix("origin/")
    except GitError:
        return _run(["rev-parse", "--abbrev-ref", "HEAD"], project_path).strip()


def create_worktree(project_path: Path, branch: str, worktree_path: Path, base_branch: str) -> None:
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["worktree", "add", "-b", branch, str(worktree_path), base_branch], project_path)


def remove_worktree(project_path: Path, worktree_path: Path, branch: str) -> None:
    if worktree_path.exists():
        _run(["worktree", "remove", "--force", str(worktree_path)], project_path)
    try:
        _run(["branch", "-D", branch], project_path)
    except GitError:
        pass


def _stage_new_files(worktree_path: Path) -> None:
    # `git diff` ignores untracked files entirely. --intent-to-add marks new
    # paths as tracked-but-empty (no content staged) so they show up as
    # additions in the diff instead of vanishing from it.
    _run(["add", "-A", "-N", "."], worktree_path)


def diff_stat(worktree_path: Path) -> str:
    _stage_new_files(worktree_path)
    return _run(["diff", "--stat"], worktree_path).strip()


def diff_full(worktree_path: Path) -> str:
    _stage_new_files(worktree_path)
    return _run(["diff"], worktree_path)


def has_changes(worktree_path: Path) -> bool:
    return bool(_run(["status", "--porcelain"], worktree_path).strip())


def commit_all(worktree_path: Path, message: str) -> None:
    _run(["add", "-A"], worktree_path)
    _run(["commit", "-m", message], worktree_path)


def merge_branch(project_path: Path, branch: str, message: str) -> None:
    _run(["merge", "--no-ff", branch, "-m", message], project_path)


@dataclass
class RepoCheck:
    ok: bool
    reason: str = ""


def check_repo(project_path: Path) -> RepoCheck:
    if not project_path.exists():
        return RepoCheck(False, "path does not exist")
    if not (project_path / ".git").exists():
        return RepoCheck(False, "not a git repository (no .git)")
    return RepoCheck(True)
