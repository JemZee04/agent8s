from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_chat_ids: set[int]
    data_dir: Path
    worktree_dir: Path
    default_agent: str
    claude_allowed_tools: list[str] = field(default_factory=list)
    claude_permission_mode: str = "acceptEdits"
    codex_sandbox: str = "workspace-write"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent8s.sqlite3"


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def load_config() -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (copy .env.example to .env and fill it in)")

    allowed_raw = os.environ.get("ALLOWED_CHAT_IDS", "").strip()
    if not allowed_raw:
        raise RuntimeError("ALLOWED_CHAT_IDS is not set — refusing to start with an open bot")
    allowed_chat_ids = {int(v) for v in _split_csv(allowed_raw)}

    data_dir = Path(os.environ.get("AGENT8S_DATA_DIR", "./data")).resolve()
    worktree_dir = Path(os.environ.get("AGENT8S_WORKTREE_DIR", "./worktrees")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    worktree_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        telegram_bot_token=token,
        allowed_chat_ids=allowed_chat_ids,
        data_dir=data_dir,
        worktree_dir=worktree_dir,
        default_agent=os.environ.get("AGENT8S_DEFAULT_AGENT", "claude").strip(),
        claude_allowed_tools=_split_csv(os.environ.get("AGENT8S_CLAUDE_ALLOWED_TOOLS", "Bash,Edit,Write,Read,Grep,Glob")),
        claude_permission_mode=os.environ.get("AGENT8S_CLAUDE_PERMISSION_MODE", "acceptEdits").strip(),
        codex_sandbox=os.environ.get("AGENT8S_CODEX_SANDBOX", "workspace-write").strip(),
    )
