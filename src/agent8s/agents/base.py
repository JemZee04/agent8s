from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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
    async def start(self, prompt: str, cwd: Path) -> AgentResult:
        """Run a fresh session in cwd."""

    @abstractmethod
    async def resume(self, session_id: str, prompt: str, cwd: Path) -> AgentResult:
        """Continue a previous session (follow-up message) in cwd."""
