from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_chat_ids: set[int]
    data_dir: Path
    worktree_dir: Path
    projects_dir: Path
    default_agent: str
    claude_allowed_tools: list[str] = field(default_factory=list)
    claude_permission_mode: str = "acceptEdits"
    codex_sandbox: str = "workspace-write"
    jira_url: Optional[str] = None
    jira_token: Optional[str] = None
    jira_verify_ssl: bool = True
    confluence_url: Optional[str] = None
    confluence_token: Optional[str] = None
    confluence_verify_ssl: bool = True
    caldav_url: Optional[str] = None
    caldav_login: Optional[str] = None
    caldav_password: Optional[str] = None
    reminder_lead_minutes: int = 15
    reminder_poll_seconds: int = 300

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent8s.sqlite3"

    @property
    def jira_configured(self) -> bool:
        return bool(self.jira_url and self.jira_token)

    @property
    def caldav_configured(self) -> bool:
        return bool(self.caldav_url and self.caldav_login and self.caldav_password)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


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
    projects_dir = Path(os.environ.get("AGENT8S_PROJECTS_DIR", "~/Documents")).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    worktree_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        telegram_bot_token=token,
        allowed_chat_ids=allowed_chat_ids,
        data_dir=data_dir,
        worktree_dir=worktree_dir,
        projects_dir=projects_dir,
        default_agent=os.environ.get("AGENT8S_DEFAULT_AGENT", "claude").strip(),
        claude_allowed_tools=_split_csv(os.environ.get("AGENT8S_CLAUDE_ALLOWED_TOOLS", "Bash,Edit,Write,Read,Grep,Glob")),
        claude_permission_mode=os.environ.get("AGENT8S_CLAUDE_PERMISSION_MODE", "acceptEdits").strip(),
        codex_sandbox=os.environ.get("AGENT8S_CODEX_SANDBOX", "workspace-write").strip(),
        jira_url=os.environ.get("JIRA_URL", "").strip() or None,
        jira_token=os.environ.get("JIRA_PERSONAL_TOKEN", "").strip() or None,
        jira_verify_ssl=_bool_env("JIRA_SSL_VERIFY", True),
        confluence_url=os.environ.get("CONFLUENCE_URL", "").strip() or None,
        confluence_token=os.environ.get("CONFLUENCE_PERSONAL_TOKEN", "").strip() or None,
        confluence_verify_ssl=_bool_env("CONFLUENCE_SSL_VERIFY", True),
        caldav_url=os.environ.get("YANDEX_CALDAV_URL", "").strip() or None,
        caldav_login=os.environ.get("YANDEX_CALDAV_LOGIN", "").strip() or None,
        caldav_password=os.environ.get("YANDEX_CALDAV_PASSWORD", "").strip() or None,
        reminder_lead_minutes=int(os.environ.get("AGENT8S_REMINDER_LEAD_MINUTES", "15")),
        reminder_poll_seconds=int(os.environ.get("AGENT8S_REMINDER_POLL_SECONDS", "300")),
    )
