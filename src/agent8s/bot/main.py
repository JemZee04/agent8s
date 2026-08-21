from __future__ import annotations

import asyncio
import logging
import logging.handlers

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from ..config import load_config
from ..db import Database
from ..singleton import acquire_singleton_lock
from .handlers import get_bot_commands, register_handlers
from .middleware import AllowlistMiddleware
from .reminders import reminder_loop

logger = logging.getLogger(__name__)


def _configure_logging(data_dir) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Persisted to disk (not just stdout) so /diagnose has something concrete
    # to read after a restart — stdout alone disappears with the terminal.
    file_handler = logging.handlers.RotatingFileHandler(
        data_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


async def _notify_stale_tasks(bot: Bot, db: Database) -> None:
    stale = db.reconcile_stale_running_tasks()
    for task in stale:
        logger.warning("task #%s was stuck 'running' from a previous run — marked %s", task.id, task.status)
        if task.status == "interrupted":
            text = (
                f"⚠️ задача #{task.id} осталась в статусе «running» с прошлого запуска бота "
                "(пережить рестарт она не могла) — отмечена как прерванная, чат разблокирован.\n"
                "У неё есть сессия агента — можно продолжить: /continue"
            )
        else:
            text = (
                f"⚠️ задача #{task.id} осталась в статусе «running» с прошлого запуска бота "
                "(пережить рестарт она не могла) — помечена неуспешной, чат разблокирован.\n"
                "Сессии агента нет, продолжить не получится — посмотри worktree/ветку вручную, если нужно понять, докуда дошло."
            )
        try:
            await bot.send_message(task.chat_id, text)
        except Exception:
            logger.exception("failed to notify chat %s about stale task #%s", task.chat_id, task.id)


async def _main() -> None:
    config = load_config()
    _configure_logging(config.data_dir)
    acquire_singleton_lock(config.data_dir)
    db = Database(config.db_path)

    bot = Bot(token=config.telegram_bot_token, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.update.outer_middleware(AllowlistMiddleware(config.allowed_chat_ids))
    dp.include_router(register_handlers())

    await bot.set_my_commands(get_bot_commands())
    await _notify_stale_tasks(bot, db)

    await asyncio.gather(
        dp.start_polling(bot, db=db, config=config),
        reminder_loop(bot, db, config),
    )


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()
