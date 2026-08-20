from __future__ import annotations

from ..config import Config
from .base import AgentRunner
from .claude_agent import ClaudeAgent
from .codex_agent import CodexAgent

AGENT_NAMES = ["claude", "codex"]


def build_agent(name: str, config: Config) -> AgentRunner:
    if name == "claude":
        return ClaudeAgent(
            allowed_tools=config.claude_allowed_tools,
            permission_mode=config.claude_permission_mode,
        )
    if name == "codex":
        return CodexAgent(sandbox=config.codex_sandbox)
    raise ValueError(f"unknown agent '{name}', known agents: {', '.join(AGENT_NAMES)}")
