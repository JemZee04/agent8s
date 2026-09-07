from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

ProgressCallback = Callable[[str], Awaitable[None]]

# Claude/codex otherwise tend to default to English for anything code-shaped
# even when the prompt itself is Russian (the model leans on the language of
# surrounding code/docs, not just the immediate instruction). Every agent
# applies this the same way so it's not something each handler has to
# remember to add.
RESPONSE_LANGUAGE_INSTRUCTION = (
    "Общайся с пользователем на русском языке — итоговый ответ, любые "
    "промежуточные пояснения и мысли вслух. Это не касается самого кода: "
    "имена, идентификаторы и комментарии в коде пиши так, как уже принято "
    "в конкретном репозитории, не меняй сложившийся стиль."
)


@dataclass
class AgentResult:
    success: bool
    session_id: Optional[str]
    summary: str
    raw_output: str


class AgentRunner(ABC):
    """Headless coding agent, invoked as a subprocess against a git worktree.

    A new agent (e.g. gemini, aider) is added by subclassing this and
    registering it in agents/registry.py — nothing else in the bot changes.
    """

    name: str

    @abstractmethod
    async def start(
        self,
        prompt: str,
        cwd: Path,
        on_progress: Optional[ProgressCallback] = None,
        extra_dirs: Optional[list[str]] = None,
    ) -> AgentResult:
        """Run a fresh session in cwd, reporting live steps via on_progress if given.

        extra_dirs: additional absolute paths the agent may write to besides
        cwd itself. Agents without a real write sandbox (claude) ignore this
        — there's nothing to widen. Agents that do sandbox writes (codex)
        apply it at session start via --add-dir.
        """

    @abstractmethod
    async def resume(
        self,
        session_id: str,
        prompt: str,
        cwd: Path,
        on_progress: Optional[ProgressCallback] = None,
        extra_dirs: Optional[list[str]] = None,
    ) -> AgentResult:
        """Continue a previous session (follow-up message) in cwd.

        extra_dirs: same as start() — additional writable paths. codex's
        `exec resume` has no --add-dir flag, but its sandbox is still
        widenable per-call via `-c sandbox_workspace_write.writable_roots=[...]`
        (confirmed live: this adds to, not replaces, the default writable
        roots, so cwd stays writable too). So unlike start()/resume in
        general, this parameter is NOT just symmetry — codex actually
        applies it on every resumed call, meaning a granted directory stays
        writable for the rest of the task, not just the next turn.
        """
