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
    async def start(self, prompt: str, cwd: Path, on_progress: Optional[ProgressCallback] = None) -> AgentResult:
        """Run a fresh session in cwd, reporting live steps via on_progress if given."""

    @abstractmethod
    async def resume(
        self, session_id: str, prompt: str, cwd: Path, on_progress: Optional[ProgressCallback] = None
    ) -> AgentResult:
        """Continue a previous session (follow-up message) in cwd."""
