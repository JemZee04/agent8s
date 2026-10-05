"""Mac side of the phone connection.

RelayLink keeps one outbound WebSocket to the relay and speaks the end-to-end
encrypted protocol with phones that hold the pairing key. A phone's requests
are replayed against this process's own local HTTP API, so the phone gets
exactly the same features as the desktop window, nothing more.

Plaintext messages (inside the AES-GCM frames of remote_crypto):
  phone -> Mac  {"k":"hello","cn":<nonce>}
                {"k":"req","sn":<server nonce>,"n":<counter>,"id":<int>,"m":<method>,"p":<path>,"b":<json|null>}
  Mac -> phone  {"k":"welcome","cn":<echo>,"sn":<fresh server nonce>}
                {"k":"res","id":<int>,"s":<status>,"b":<json>}
                {"k":"ev","e":[<hub events>]}

Replay protection: every `req` must carry the server nonce issued to *that*
connection by a fresh `welcome` plus a strictly increasing counter, so frames
captured earlier (by the relay or anyone on the path) are rejected even after
the Mac restarts.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import random
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import aiohttp

from .preview import Preview, PreviewManager
from .remote_crypto import C2H, H2C, b64url, b64url_decode, derive, new_key, open_frame, seal
from .runner import Hub, UserError

log = logging.getLogger("agent8s.desktop.remote")

MAX_FRAME = 8 * 1024 * 1024
MAX_BODY = 256 * 1024
MAX_PARALLEL = 8
EVENT_BATCH_SECONDS = 0.03
# What a phone may call. Deliberately excludes /open (would pop windows up on
# the Mac) and /api/remote* (a phone must not manage its own pairing).
ALLOWED_PATH = re.compile(
    r"^/api/(bootstrap|discover|projects|import/claude|previews(/[0-9a-f]{32})?"
    r"|chats(/\d+(\?(?:limit|before)=\d+(?:&(?:limit|before)=\d+)?|/(send|stop|diff|commit|merge|dirs|ports|preview|html))?)?)$"
)
ALLOWED_METHODS = {"GET", "POST", "PATCH", "DELETE"}


@dataclass
class ClientState:
    sn: str = ""
    last_n: int = 0


def ws_url(relay_url: str) -> str:
    parsed = urlparse(relay_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{parsed.path}/ws/host"


def validate_relay_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    loopback = parsed.hostname in ("127.0.0.1", "localhost")
    if not parsed.netloc or parsed.scheme not in ("https", "http") or (parsed.scheme == "http" and not loopback):
        raise UserError("Адрес реле должен быть HTTPS-адресом, например https://example.com/agent8s")
    return url


class RelayLink:
    def __init__(self, relay_url: str, key: bytes, hub: Hub, local_base: str, local_token: str,
                 previews: Optional[PreviewManager] = None):
        self.room_id, self._enc = derive(key)
        self._previews = previews
        self._url = ws_url(relay_url)
        self._hub = hub
        self._local_base = local_base.rstrip("/")
        self._local_token = local_token
        self._clients: dict[str, ClientState] = {}
        self._tasks: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(MAX_PARALLEL)
        self._runner: Optional[asyncio.Task] = None
        self._http: Optional[aiohttp.ClientSession] = None
        self.state = "stopped"
        self.error = ""

    # -- lifecycle --

    def start(self) -> None:
        self.state = "connecting"
        self._runner = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        self.state = "stopped"

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "error": self.error, "clients": len(self._clients)}

    async def _run(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._connected_session()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("relay link dropped: %s", self.error)
            self.state = "connecting"
            self._clients.clear()
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 30.0)

    async def _connected_session(self) -> None:
        async with aiohttp.ClientSession() as http:
            self._http = http
            async with http.ws_connect(self._url, heartbeat=25, max_msg_size=MAX_FRAME) as ws:
                await ws.send_str(json.dumps({"t": "hello", "room": self.room_id}))
                first = await asyncio.wait_for(ws.receive(), 10)
                if first.type != aiohttp.WSMsgType.TEXT or json.loads(first.data).get("t") != "ready":
                    raise RuntimeError(f"relay refused the connection ({ws.close_code})")
                self.state, self.error = "connected", ""
                log.info("connected to relay")

                outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
                events = self._hub.subscribe()
                unsubscribe = self._share_previews(outbox)
                workers = [
                    asyncio.create_task(self._reader(ws, outbox)),
                    asyncio.create_task(self._sender(ws, outbox)),
                    asyncio.create_task(self._forward_events(events, outbox)),
                ]
                try:
                    done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()  # surfaces the reason the session ended
                finally:
                    for task in workers:
                        task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                    self._hub.unsubscribe(events)
                    unsubscribe()

    # -- previews --

    def _share_previews(self, outbox: asyncio.Queue) -> Callable[[], None]:
        """Tell the relay about every live preview now, and about changes while connected."""
        if self._previews is None:
            return lambda: None

        def announce(op: str, preview: Preview) -> None:
            frame = json.dumps({"t": "preview", "op": op, "cap": preview.cap, "exp": preview.exp})
            try:
                outbox.put_nowait(frame)
            except asyncio.QueueFull:
                log.warning("outbox full; a preview update was dropped")

        for preview in self._previews.list():
            announce("add", preview)
        return self._previews.subscribe(announce)

    async def _serve_http(self, request: dict[str, Any], outbox: asyncio.Queue) -> None:
        async with self._sem:
            response = await self._previews.fetch(request)  # type: ignore[union-attr]
        try:
            outbox.put_nowait(json.dumps(response, separators=(",", ":")))
        except asyncio.QueueFull:
            log.warning("outbox full; dropping a preview response")

    # -- relay traffic --

    def _pack(self, cid: str, payload: dict[str, Any]) -> str:
        return json.dumps({"t": "msg", "c": cid, "d": seal(self._enc, self.room_id, H2C, payload)}, separators=(",", ":"))

    async def _sender(self, ws: aiohttp.ClientWebSocketResponse, outbox: asyncio.Queue) -> None:
        while True:
            await ws.send_str(await outbox.get())

    async def _reader(self, ws: aiohttp.ClientWebSocketResponse, outbox: asyncio.Queue) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            kind, cid = data.get("t"), data.get("c")
            if kind == "http" and self._previews is not None:
                task = asyncio.create_task(self._serve_http(data, outbox))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                continue
            if not isinstance(cid, str):
                continue
            if kind == "join":
                self._clients[cid] = ClientState()
            elif kind == "leave":
                self._clients.pop(cid, None)
            elif kind == "msg" and isinstance(data.get("d"), str):
                self._on_frame(cid, data["d"], outbox)

    async def _forward_events(self, queue: asyncio.Queue, outbox: asyncio.Queue) -> None:
        while True:
            batch = [await queue.get()]
            await asyncio.sleep(EVENT_BATCH_SECONDS)  # coalesce a burst of stream deltas into one frame
            while len(batch) < 200 and not queue.empty():
                batch.append(queue.get_nowait())
            if None in batch:
                raise ConnectionError("fell behind the event stream")
            if self._clients:
                # Same ciphertext for everyone: only holders of the key can read it.
                outbox.put_nowait(self._pack("*", {"k": "ev", "e": batch}))

    # -- protocol --

    def _on_frame(self, cid: str, frame: str, outbox: asyncio.Queue) -> None:
        obj = open_frame(self._enc, self.room_id, C2H, frame)
        if not isinstance(obj, dict):
            return  # forged, corrupt or for another room: ignore silently
        state = self._clients.setdefault(cid, ClientState())
        kind = obj.get("k")
        if kind == "hello":
            nonce = obj.get("cn")
            if isinstance(nonce, str) and len(nonce) <= 64:
                state.sn, state.last_n = secrets.token_hex(16), 0
                outbox.put_nowait(self._pack(cid, {"k": "welcome", "cn": nonce, "sn": state.sn}))
        elif kind == "req":
            rid, n = obj.get("id"), obj.get("n")
            if not state.sn or not isinstance(obj.get("sn"), str) or not hmac.compare_digest(obj["sn"], state.sn):
                outbox.put_nowait(self._pack(cid, {"k": "res", "id": rid, "s": 401, "b": {"error": "stale"}}))
                return
            if not isinstance(n, int) or isinstance(n, bool) or n <= state.last_n:
                return  # replayed or reordered request
            state.last_n = n
            task = asyncio.create_task(self._answer(cid, obj, outbox))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _answer(self, cid: str, req: dict[str, Any], outbox: asyncio.Queue) -> None:
        async with self._sem:
            status, body = await self.local_request(req.get("m"), req.get("p"), req.get("b"))
        try:
            outbox.put_nowait(self._pack(cid, {"k": "res", "id": req.get("id"), "s": status, "b": body}))
        except asyncio.QueueFull:
            log.warning("outbox full; dropping a response")

    async def local_request(self, method: Any, path: Any, body: Any) -> tuple[int, Any]:
        if method not in ALLOWED_METHODS or not isinstance(path, str) or not ALLOWED_PATH.match(path):
            return 403, {"error": "Этот запрос не разрешён для удалённого доступа."}
        if body is not None and len(json.dumps(body)) > MAX_BODY:
            return 413, {"error": "Слишком большой запрос."}
        assert self._http is not None
        try:
            async with self._http.request(
                method,
                self._local_base + path,
                json=body,
                headers={"X-Agent8s-Token": self._local_token, "Origin": self._local_base},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                text = await resp.text()
                try:
                    return resp.status, json.loads(text)
                except ValueError:
                    return resp.status, {"error": text[:500]}
        except Exception as exc:
            return 502, {"error": f"{type(exc).__name__}: {exc}"}


class RemoteManager:
    """Owns the pairing key on disk and the (optional) running RelayLink."""

    def __init__(self, data_dir: Path, hub: Hub, port: int, token: str, default_relay: Optional[str] = None,
                 preview_origin: Optional[str] = None):
        self._file = data_dir / "remote.json"
        self._hub = hub
        self.previews = PreviewManager(hub, {port}, url_for=self.preview_url)
        self._preview_origin = preview_origin
        self._base = f"http://127.0.0.1:{port}"
        self._token = token
        self.default_relay = default_relay
        self._link: Optional[RelayLink] = None
        self._awake: Optional[subprocess.Popen] = None

    def _load(self) -> Optional[dict[str, str]]:
        try:
            data = json.loads(self._file.read_text())
            return data if {"relay", "key"} <= set(data) else None
        except (OSError, ValueError):
            return None

    def configured(self) -> bool:
        return self._load() is not None

    async def start(self) -> None:
        cfg = self._load()
        if cfg and self._link is None:
            self._link = RelayLink(cfg["relay"], b64url_decode(cfg["key"]), self._hub, self._base, self._token, self.previews)
            self._link.start()
            self._keep_awake()

    async def pair(self, relay_url: Optional[str]) -> dict[str, Any]:
        """(Re)generate the pairing key. Phones paired with the old key stop working."""
        url = validate_relay_url(relay_url or self.default_relay or "")
        await self._stop_link()
        self._file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"relay": url, "key": b64url(new_key())})
        fd = os.open(self._file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        await self.start()
        return self.info()

    async def disable(self) -> None:
        await self._stop_link()
        self._file.unlink(missing_ok=True)

    async def shutdown(self) -> None:
        await self._stop_link()
        await self.previews.close()

    async def _stop_link(self) -> None:
        if self._link:
            await self._link.stop()
            self._link = None
        if self._awake:
            self._awake.terminate()
            self._awake = None

    def _keep_awake(self) -> None:
        # The phone is useless if the Mac idles to sleep; this holds the idle
        # sleep off for exactly as long as this process lives.
        if sys.platform == "darwin" and self._awake is None:
            try:
                self._awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                pass

    def preview_url(self, cap: str) -> str:
        """Where the phone opens a preview. Must be a different *origin* than the app (it runs
        arbitrary dev-site scripts, which must not reach the app's stored key)."""
        cfg = self._load()
        if cfg is None:
            raise UserError("Сначала подключите телефон (кнопка «Телефон»): превью идёт через реле.")
        parts = urlparse(cfg["relay"])
        if self._preview_origin:
            origin = self._preview_origin.rstrip("/")
        elif parts.hostname in ("127.0.0.1", "localhost"):
            origin = f"{parts.scheme}://{parts.netloc}"
        elif parts.hostname and not parts.hostname.startswith("www."):
            origin = f"{parts.scheme}://www.{parts.netloc}"
        else:
            raise UserError("Не могу вывести адрес для превью: задайте AGENT8S_PREVIEW_ORIGIN (второе имя вашего сервера).")
        return f"{origin}{parts.path.rstrip('/')}/p/{cap}/"

    def create_preview(self, chat_id: int, port: Optional[int] = None, file: Optional[str] = None) -> dict[str, Any]:
        self.preview_url("0" * 32)  # fails early (not paired / no preview host) before anything is registered
        preview = self.previews.create_file(chat_id, file) if file else self.previews.create(chat_id, port or 0)
        return {**preview.public(), "url": self.preview_url(preview.cap)}

    def list_previews(self, chat_id: Optional[int] = None) -> list[dict[str, Any]]:
        result = []
        for preview in self.previews.list(chat_id):
            try:
                result.append({**preview.public(), "url": self.preview_url(preview.cap)})
            except UserError:
                pass
        return result

    def pairing_url(self) -> Optional[str]:
        cfg = self._load()
        return f"{cfg['relay']}/#k={cfg['key']}" if cfg else None

    def info(self) -> dict[str, Any]:
        cfg = self._load()
        link = self._link.status() if self._link else {"state": "stopped", "error": "", "clients": 0}
        return {
            "configured": cfg is not None,
            "relay": cfg["relay"] if cfg else self.default_relay,
            **link,
        }
