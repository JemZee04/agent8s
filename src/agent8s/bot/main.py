from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from ..config import load_config
from ..db import Database
from .handlers import register_handlers
from .middleware import AllowlistMiddleware
from .reminders import reminder_loop


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    db = Database(config.db_path)

    bot = Bot(token=config.telegram_bot_token, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.update.outer_middleware(AllowlistMiddleware(config.allowed_chat_ids))
    dp.include_router(register_handlers())

    await asyncio.gather(
        dp.start_polling(bot, db=db, config=config),
        reminder_loop(bot, db, config),
    )


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()
