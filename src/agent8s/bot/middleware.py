from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update


class AllowlistMiddleware(BaseMiddleware):
    """Hard chat_id whitelist, checked before any handler runs.

    This machine executes shell commands against local git repos on behalf of
    whoever can message the bot — the whitelist is the only thing standing
    between "my phone" and "anyone who finds the bot username".
    """

    def __init__(self, allowed_user_ids: set[int]):
        self._allowed = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None and isinstance(event, Update) and event.message:
            user = event.message.from_user

        if user is None or user.id not in self._allowed:
            return None

        return await handler(event, data)
