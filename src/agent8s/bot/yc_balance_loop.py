from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from .. import yandexcloud
from ..config import Config
from ..db import Database

logger = logging.getLogger(__name__)


async def yc_balance_loop(bot: Bot, db: Database, config: Config) -> None:
    """Deterministic, LLM-free: poll the Billing API, record a snapshot,
    alert once (with cooldown) when balance drops below the threshold."""
    if not config.yc_configured:
        logger.info("Yandex Cloud billing not configured, balance loop disabled")
        return
    while True:
        try:
            await _check_once(bot, db, config)
        except Exception:
            logger.exception("yandex cloud balance check failed")
        await asyncio.sleep(config.yc_balance_poll_seconds)


async def _check_once(bot: Bot, db: Database, config: Config) -> None:
    balance = await asyncio.to_thread(yandexcloud.get_balance, config)
    db.record_balance_snapshot(balance)

    threshold = config.yc_balance_alert_threshold
    if threshold is None or balance >= threshold:
        return
    if not db.should_send_balance_alert(config.yc_balance_alert_cooldown_hours):
        return

    text = f"💸 Баланс Yandex Cloud: {balance:.2f} — ниже порога {threshold:.2f}. Пора пополнить."
    for chat_id in config.allowed_chat_ids:
        await bot.send_message(chat_id, text)
    db.mark_balance_alert_sent()
