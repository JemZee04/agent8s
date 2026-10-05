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
import mimetypes
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlsplit

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

# A shared HTML file brings along its folder (images, styles, scripts), but only things a web page
# uses: a folder next to it may well contain .env files, keys or databases.
SERVED_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".map", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".txt", ".md", ".pdf",
}
HTML_EXTENSIONS = {".html", ".htm"}
SKIPPED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", ".cache", "site-packages", ".idea"}


def list_html_files(folder: str, limit: int = 100, max_depth: int = 4) -> list[dict[str, Any]]:
    """HTML files under folder, newest first (what the agent just produced comes first)."""
    root = Path(folder)
    found: list[dict[str, Any]] = []
    if not root.is_dir():
        return found
    for current, dirs, files in os.walk(root):
        depth = len(Path(current).relative_to(root).parts)
        dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS and not d.startswith(".")] if depth < max_depth else []
        for name in files:
            path = Path(current) / name
            if path.suffix.lower() in HTML_EXTENSIONS and not name.startswith("."):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                found.append({"path": str(path), "rel": str(path.relative_to(root)), "size": stat.st_size, "mtime": stat.st_mtime})
    found.sort(key=lambda f: f["mtime"], reverse=True)
    return found[:limit]


@dataclass
class Preview:
    cap: str
    port: int
    chat_id: int
    host: str
    exp: float
    created: float = field(default_factory=time.time)
    kind: str = "port"  # "port": a site on localhost; "file": an HTML file (and its folder) served from disk
    file: str = ""
    root: str = ""

    @property
    def name(self) -> str:
        return Path(self.file).name if self.kind == "file" else f"localhost:{self.port}"

    def public(self) -> dict[str, Any]:
        return {"cap": self.cap, "port": self.port, "chat_id": self.chat_id, "exp": self.exp,
                "kind": self.kind, "name": self.name}


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


async def is_web_page(port: int) -> bool:
    """Does something on this port answer `GET /` with a web page? Process names say little
    (a dev server can be called anything), the response does."""
    host = await asyncio.to_thread(_reachable_host, port)
    if host is None:
        return False
    target = f"[{host}]" if ":" in host else host
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
            async with session.get(
                f"http://{target}:{port}/", allow_redirects=False, timeout=aiohttp.ClientTimeout(total=1.2),
                headers={"Host": f"localhost:{port}", "Accept": "text/html"},
            ) as resp:
                return resp.status < 400 and "html" in resp.headers.get("Content-Type", "").lower()
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError, ValueError):
        return False  # not HTTP at all (a database, a daemon), or too slow to be a dev server


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

    def create_file(self, chat_id: int, file: str) -> Preview:
        self._prune()
        path = Path(file).expanduser()
        if not path.is_absolute():
            raise UserError("Нужен абсолютный путь к файлу.")
        try:
            path = path.resolve(strict=True)
        except OSError:
            raise UserError(f"Файл не найден: {file}")
        if not path.is_file() or path.suffix.lower() not in HTML_EXTENSIONS:
            raise UserError("Это не HTML-файл (.html / .htm).")
        existing = next((p for p in self._items.values() if p.kind == "file" and p.file == str(path) and p.chat_id == chat_id), None)
        if existing:
            existing.exp = time.time() + self._ttl
            self._notify("add", existing)
            return existing
        if len(self._items) >= MAX_PREVIEWS:
            raise UserError(f"Одновременно можно держать не больше {MAX_PREVIEWS} превью — остановите ненужные.")
        preview = Preview(secrets.token_hex(16), 0, chat_id, "", time.time() + self._ttl,
                          kind="file", file=str(path), root=str(path.parent))
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
        if preview.kind == "file":
            return self._serve_file(preview, rid, msg.get("m"), msg.get("p"))
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
    def _serve_file(preview: Preview, rid: Any, method: Any, raw_path: Any) -> dict[str, Any]:
        def reply(status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8") -> dict[str, Any]:
            headers = [["Content-Type", content_type], ["Cache-Control", "no-cache"]]  # edits show on reload
            return {"t": "httpres", "id": rid, "s": status, "h": headers, "b": base64.b64encode(body).decode()}

        if method not in ("GET", "HEAD"):
            return reply(405, b"Method Not Allowed")
        if not isinstance(raw_path, str) or not raw_path.startswith("/") or "\x00" in raw_path:
            return {"t": "httperr", "id": rid, "e": "Некорректный запрос."}
        relative = unquote(urlsplit(raw_path).path).lstrip("/")
        root = Path(preview.root)
        if not relative:
            target = Path(preview.file)
        else:
            parts = Path(relative).parts
            if any(part.startswith(".") or part == ".." for part in parts):  # dotfiles and traversal
                return reply(404, b"Not found")
            target = (root / relative).resolve()
            # resolve() follows symlinks, so a link pointing out of the folder fails this check too
            if root not in target.parents or target.suffix.lower() not in SERVED_EXTENSIONS:
                return reply(404, b"Not found")
        try:
            if not target.is_file():
                return reply(404, b"Not found")
            if target.stat().st_size > MAX_RESPONSE:
                return {"t": "httperr", "id": rid, "e": "Файл слишком большой для превью."}
            body = target.read_bytes()
        except OSError:
            return reply(404, b"Not found")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/json", "application/javascript"):
            try:
                body.decode("utf-8")
                content_type += "; charset=utf-8"  # otherwise the browser guesses (and guesses wrong for Cyrillic)
            except UnicodeDecodeError:
                pass
        return reply(200, b"" if method == "HEAD" else body, content_type)

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
