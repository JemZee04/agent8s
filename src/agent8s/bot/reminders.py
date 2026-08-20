from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from .. import calendar_client
from ..config import Config
from ..db import Database

logger = logging.getLogger(__name__)


async def reminder_loop(bot: Bot, db: Database, config: Config) -> None:
    """Deterministic, LLM-free: poll CalDAV, dedupe against sent_reminders, send."""
    if not config.caldav_configured:
        logger.info("Yandex Calendar not configured, reminder loop disabled")
        return
    while True:
        try:
            await _check_once(bot, db, config)
        except Exception:
            logger.exception("reminder check failed")
        await asyncio.sleep(config.reminder_poll_seconds)


async def _check_once(bot: Bot, db: Database, config: Config) -> None:
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=config.reminder_lead_minutes)
    events = await asyncio.to_thread(calendar_client.fetch_events, config, now, window_end)

    for event in events:
        start_key = event.start.isoformat()
        if db.was_reminded(event.uid, start_key):
            continue

        local_time = event.start.astimezone().strftime("%H:%M")
        text = f"🔔 {event.summary} — {local_time}"
        if event.location:
            text += f"\n📍 {event.location}"

        for chat_id in config.allowed_chat_ids:
            await bot.send_message(chat_id, text)

        db.mark_reminded(event.uid, start_key)
