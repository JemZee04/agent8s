from __future__ import annotations

import time
from typing import Optional

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

MAX_LINES = 10
MIN_EDIT_INTERVAL = 2.0
TEXT_LIMIT = 4000


class ProgressReporter:
    """Edits a single Telegram message with the agent's live tool calls.

    Edits are throttled client-side (Telegram rate-limits message edits) —
    lines between throttled updates aren't lost, they just ride along in the
    buffer until the next edit or the final one.
    """

    def __init__(self, message: Message, header: str, min_interval: float = MIN_EDIT_INTERVAL):
        self._origin = message
        self._header = header
        self._lines: list[str] = []
        self._min_interval = min_interval
        self._last_edit = 0.0
        self._sent: Optional[Message] = None

    async def start(self) -> None:
        self._sent = await self._origin.answer(self._header)

    async def update(self, line: str) -> None:
        self._lines.append(line)
        self._lines = self._lines[-MAX_LINES:]
        now = time.monotonic()
        if now - self._last_edit < self._min_interval:
            return
        self._last_edit = now
        await self._flush()

    async def finish(self, final_text: str) -> None:
        text = _truncate(final_text)
        if self._sent is None:
            await self._origin.answer(text)
            return
        try:
            await self._sent.edit_text(text)
        except TelegramBadRequest:
            await self._origin.answer(text)

    async def _flush(self) -> None:
        if self._sent is None:
            return
        text = self._header + "\n\n" + "\n".join(self._lines)
        try:
            await self._sent.edit_text(_truncate(text))
        except TelegramBadRequest:
            pass  # e.g. "message is not modified" — harmless, next edit will land


def _truncate(text: str) -> str:
    if len(text) <= TEXT_LIMIT:
        return text
    return text[:TEXT_LIMIT] + "\n… (truncated, see /diff)"
