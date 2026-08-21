from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from ..config import load_config
from ..db import Database
from ..singleton import acquire_singleton_lock
from .handlers import register_handlers
from .middleware import AllowlistMiddleware
from .reminders import reminder_loop

logger = logging.getLogger(__name__)


async def _notify_stale_tasks(bot: Bot, db: Database) -> None:
    stale = db.reconcile_stale_running_tasks()
    for task in stale:
        logger.warning("task #%s was stuck 'running' from a previous run — marked failed", task.id)
        try:
            await bot.send_message(
                task.chat_id,
                f"⚠️ task #{task.id} was still marked running from before the bot last restarted "
                "(it can't have survived that) — marked failed and unblocked this chat. "
                "Check the worktree/branch by hand if you need to know how far it got.",
            )
        except Exception:
            logger.exception("failed to notify chat %s about stale task #%s", task.chat_id, task.id)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    acquire_singleton_lock(config.data_dir)
    db = Database(config.db_path)

    bot = Bot(token=config.telegram_bot_token, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.update.outer_middleware(AllowlistMiddleware(config.allowed_chat_ids))
    dp.include_router(register_handlers())

    await _notify_stale_tasks(bot, db)

    await asyncio.gather(
        dp.start_polling(bot, db=db, config=config),
        reminder_loop(bot, db, config),
    )


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()
