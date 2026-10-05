"""Open a site the agent runs on this Mac's localhost on the phone.

A preview maps an unguessable capability to a local port. The relay receives
HTTP requests for that capability and forwards them over the existing link; this
module performs them against 127.0.0.1 and returns the response. Unlike chat
traffic this is *not* end-to-end encrypted: the relay has to see the pages to
serve them, so treat the relay as trusted for previews.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import aiohttp

from .runner import Hub, UserError
from .service import PORT as SERVICE_PORT  # the background service's fixed port

log = logging.getLogger("agent8s.desktop.preview")

TTL_SECONDS = 8 * 3600
MAX_PREVIEWS = 8
MAX_RESPONSE = 5 * 1024 * 1024  # base64 must fit one 8 MB relay frame
REQUEST_TIMEOUT = 50
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "content-length", "host"}
CAP_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class Preview:
    cap: str
    port: int
    chat_id: int
    host: str
    exp: float
    created: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {"cap": self.cap, "port": self.port, "chat_id": self.chat_id, "exp": self.exp}


Listener = Callable[[str, Preview], None]


def _reachable_host(port: int) -> Optional[str]:
    """Dev servers listen on 127.0.0.1 or only on ::1 (`localhost` on some setups)."""
    for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect((host, port))
                return host
        except OSError:
            continue
    return None


class PreviewManager:
    def __init__(self, hub: Hub, own_ports: set[int], ttl: float = TTL_SECONDS,
                 url_for: Optional[Callable[[str], str]] = None):
        self._hub = hub
        self._url_for = url_for
        self._own_ports = own_ports
        self._ttl = ttl
        self._items: dict[str, Preview] = {}
        self._listeners: list[Listener] = []
        self._session: Optional[aiohttp.ClientSession] = None

    # -- registry --

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _prune(self) -> None:
        for preview in [p for p in self._items.values() if p.exp < time.time()]:
            self.remove(preview.cap)

    def list(self, chat_id: Optional[int] = None) -> list[Preview]:
        self._prune()
        return [p for p in self._items.values() if chat_id is None or p.chat_id == chat_id]

    def create(self, chat_id: int, port: int) -> Preview:
        self._prune()
        if not 1 <= port <= 65535:
            raise UserError("Порт должен быть числом от 1 до 65535.")
        if port in self._own_ports or port == SERVICE_PORT:
            raise UserError("Это порт самого agent8s — его нельзя публиковать.")
        host = _reachable_host(port)
        if host is None:
            raise UserError(f"На порту {port} ничего не слушает. Сначала попросите агента запустить сайт.")
        existing = next((p for p in self._items.values() if p.port == port and p.chat_id == chat_id), None)
        if existing:  # same site again: keep the link (and the phone's session) stable
            existing.exp = time.time() + self._ttl
            self._notify("add", existing)
            return existing
        if len(self._items) >= MAX_PREVIEWS:
            raise UserError(f"Одновременно можно держать не больше {MAX_PREVIEWS} превью — остановите ненужные.")
        preview = Preview(secrets.token_hex(16), port, chat_id, host, time.time() + self._ttl)
        self._items[preview.cap] = preview
        self._notify("add", preview)
        return preview

    def remove(self, cap: str) -> bool:
        preview = self._items.pop(cap, None)
        if preview:
            self._notify("del", preview)
        return preview is not None

    def _notify(self, op: str, preview: Preview) -> None:
        for listener in list(self._listeners):
            listener(op, preview)
        event: dict[str, Any] = {"t": "preview", "op": op, **preview.public()}
        if op == "add" and self._url_for:
            try:
                event["url"] = self._url_for(preview.cap)
            except UserError:
                pass  # not paired: nobody to send the link to
        self._hub.publish(event)

    # -- serving --

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def fetch(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Answer one HTTP request the relay forwarded: {"t":"httpres",...} or {"t":"httperr",...}."""
        rid = msg.get("id")
        cap = msg.get("cap")
        preview = self._items.get(cap) if isinstance(cap, str) else None
        if preview is None or preview.exp < time.time():
            return {"t": "httperr", "id": rid, "e": "Превью остановлено или истекло."}
        path = msg.get("p")
        method = msg.get("m")
        if not isinstance(path, str) or not path.startswith("/") or method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            return {"t": "httperr", "id": rid, "e": "Некорректный запрос."}
        try:
            body = base64.b64decode(msg.get("b") or "", validate=True)
        except ValueError:
            return {"t": "httperr", "id": rid, "e": "Некорректное тело запроса."}

        headers = {k: v for k, v in (msg.get("h") or []) if isinstance(k, str) and isinstance(v, str) and k.lower() not in HOP_BY_HOP}
        target_host = f"[{preview.host}]" if ":" in preview.host else preview.host
        headers["Host"] = f"localhost:{preview.port}"  # dev servers reject unknown Host headers
        if self._session is None:
            # auto_decompress=False: the body stays exactly as the app sent it, matching its Content-Encoding.
            self._session = aiohttp.ClientSession(auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar())
        try:
            async with self._session.request(
                method, f"http://{target_host}:{preview.port}{path}", headers=headers, data=body or None,
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                chunks: list[bytes] = []
                total = 0
                # read() may return only what has arrived so far, so read to the end, bounded.
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > MAX_RESPONSE:
                        return {"t": "httperr", "id": rid, "e": "Ответ слишком большой для превью."}
                    chunks.append(chunk)
                payload = b"".join(chunks)
                out = [[k, self._rewrite_header(k, v, preview.port)] for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP]
                return {"t": "httpres", "id": rid, "s": resp.status, "h": out, "b": base64.b64encode(payload).decode()}
        except asyncio.TimeoutError:
            return {"t": "httperr", "id": rid, "e": "Сайт на компьютере не ответил вовремя."}
        except aiohttp.ClientError as exc:
            return {"t": "httperr", "id": rid, "e": f"Сайт на компьютере недоступен: {type(exc).__name__}"}

    @staticmethod
    def _rewrite_header(name: str, value: str, port: int) -> str:
        lower = name.lower()
        if lower == "location":
            parts = urlsplit(value)
            # An absolute redirect to the dev server itself must stay inside the preview.
            if parts.scheme in ("http", "https") and parts.hostname in ("localhost", "127.0.0.1", "::1") and parts.port == port:
                return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if lower == "set-cookie":
            # A cookie for localhost:PORT must apply to whatever host the phone is on.
            return re.sub(r";\s*domain=[^;]*", "", value, flags=re.I)
        return value
