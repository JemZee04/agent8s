from __future__ import annotations

from pathlib import Path

SELF_PROJECT_NAME = "agent8s"


def self_repo_path() -> Path:
    # src/agent8s/selfrepo.py -> src/agent8s -> src -> repo root
    return Path(__file__).resolve().parents[2]


def tail_log(data_dir: Path, lines: int = 150) -> str:
    log_path = data_dir / "bot.log"
    if not log_path.exists():
        return "(лог-файла ещё нет)"
    with log_path.open(errors="replace") as f:
        tail = f.readlines()[-lines:]
    return "".join(tail) if tail else "(лог пуст)"
