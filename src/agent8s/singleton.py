from __future__ import annotations

import fcntl
import sys
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass

# Held for the process's lifetime — if this gets garbage collected, its fd
# closes and the flock releases with it, silently defeating the whole lock.
_lock_file = None


def acquire_singleton_lock(data_dir: Path) -> None:
    """Refuse to start if another agent8s-bot is already running against the
    same data dir. Without this, two long-pollers on the same Telegram bot
    token silently race for updates — confusing at best, and a real cause of
    tasks getting stuck with no error ever surfaced (see incident: four
    stray instances accumulated over days, one task's handler died mid-run
    with nothing to show for it since a different instance kept answering
    /status from the same shared database).
    """
    global _lock_file
    lock_path = data_dir / "bot.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        print(
            f"Another agent8s-bot is already running (lock held on {lock_path}). "
            "Stop it before starting a new one — running two pollers on the same "
            "bot token races unpredictably.",
            file=sys.stderr,
        )
        raise AlreadyRunningError(str(lock_path))
    _lock_file = lock_file  # keep alive: closing this fd would release the flock
