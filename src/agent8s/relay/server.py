"""Blind relay between a Mac running agent8s-desktop and a phone.

Why it exists: the phone cannot reach the Mac directly (home NAT, mobile
networks that block SSH). Both sides instead open an *outbound* WebSocket to
this relay over ordinary HTTPS, and the relay forwards frames between them.

The relay is deliberately dumb and untrusted:
  - frames are opaque strings (AES-GCM ciphertext made with a key the relay
    never sees), so it can neither read nor forge anything, only drop or delay;
  - the room id, a 128-bit capability derived from that key, is sent in the
    first WebSocket message, never in a URL, so it does not end up in access logs.

Dependencies: aiohttp only (this file is copied alone into the container).

Wire protocol (JSON text frames):
  host   -> relay  {"t":"hello","room":<32 hex>}   first message, within 5 s
  relay  -> host   {"t":"ready"}
  relay  -> host   {"t":"join","c":<cid>} / {"t":"leave","c":<cid>} / {"t":"msg","c":<cid>,"d":<opaque>}
  host   -> relay  {"t":"msg","c":<cid>|"*","d":<opaque>}
  client -> relay  {"t":"hello","room":<32 hex>}   first message
  client -> relay  <opaque text frame>             forwarded to the host as "msg"
  relay  -> client {"t":"presence","host":true|false}   plaintext, starts with "{"
  relay  -> client <opaque text frame>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aiohttp import WSMsgType, web

log = logging.getLogger("agent8s.relay")

MAX_ROOMS = 32
MAX_CLIENTS_PER_ROOM = 8
MAX_CONNECTIONS_PER_IP = 24
MAX_FRAME = 8 * 1024 * 1024
HELLO_TIMEOUT = 5.0
QUEUE_SIZE = 512
RATE_BURST = 60
RATE_PER_SECOND = 30
RELAY_KEY = web.AppKey("relay", object)
ROOM_RE = re.compile(r"^[0-9a-f]{32}$")

CLOSE_GOING_AWAY = 1001
CLOSE_REPLACED = 4001
CLOSE_BAD_HELLO = 4002
CLOSE_FULL = 4003
CLOSE_NO_ROOM = 4004
CLOSE_SLOW = 4008
CLOSE_RATE = 4029


@dataclass
class Peer:
    ws: web.WebSocketResponse
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))
    tokens: float = RATE_BURST
    refilled: float = field(default_factory=time.monotonic)

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(RATE_BURST, self.tokens + (now - self.refilled) * RATE_PER_SECOND)
        self.refilled = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    def push(self, item) -> bool:
        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            return False

    def close_soon(self, code: int, message: bytes = b"") -> None:
        """Ask the writer task (the only task allowed to touch the socket's
        output) to close the connection, dropping whatever is still queued."""
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(("close", code, message))


@dataclass
class Room:
    room_id: str
    host: Optional[Peer] = None
    clients: dict[str, Peer] = field(default_factory=dict)


class Relay:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}
        self.per_ip: dict[str, int] = {}

    def stats(self) -> dict:
        return {
            "rooms": len(self.rooms),
            "hosts": sum(1 for r in self.rooms.values() if r.host),
            "clients": sum(len(r.clients) for r in self.rooms.values()),
        }


async def _hello(ws: web.WebSocketResponse) -> Optional[str]:
    try:
        msg = await asyncio.wait_for(ws.receive(), HELLO_TIMEOUT)
    except asyncio.TimeoutError:
        return None
    if msg.type != WSMsgType.TEXT:
        return None
    try:
        data = json.loads(msg.data)
    except ValueError:
        return None
    room = data.get("room") if isinstance(data, dict) and data.get("t") == "hello" else None
    return room if isinstance(room, str) and ROOM_RE.match(room) else None


async def _writer(peer: Peer) -> None:
    """Sole writer to the socket; ends on a None sentinel or a close request."""
    while True:
        item = await peer.queue.get()
        if item is None:
            return
        if isinstance(item, tuple):
            await peer.ws.close(code=item[1], message=item[2])
            return
        await peer.ws.send_str(item if isinstance(item, str) else json.dumps(item, separators=(",", ":")))


def _client_ip(request: web.Request) -> str:
    # The relay is only reachable through the reverse proxy, which sets this.
    return request.headers.get("X-Real-IP") or request.remote or "?"


def _cleanup_room(relay: Relay, room: Room) -> None:
    if room.host is None and not room.clients:
        relay.rooms.pop(room.room_id, None)


def make_app(web_dir: Optional[Path] = None, prefix: str = "") -> web.Application:
    prefix = prefix.rstrip("/")
    relay = Relay()
    app = web.Application()

    async def serve(request: web.Request, role: str) -> web.WebSocketResponse:
        ip = _client_ip(request)
        if relay.per_ip.get(ip, 0) >= MAX_CONNECTIONS_PER_IP:
            raise web.HTTPTooManyRequests()
        relay.per_ip[ip] = relay.per_ip.get(ip, 0) + 1
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=MAX_FRAME)
        try:
            await ws.prepare(request)
            room_id = await _hello(ws)
            if room_id is None:
                await ws.close(code=CLOSE_BAD_HELLO, message=b"bad hello")
                return ws
            if role == "host":
                await _run_host(relay, ws, room_id)
            else:
                await _run_client(relay, ws, room_id)
        finally:
            relay.per_ip[ip] -= 1
            if relay.per_ip[ip] <= 0:
                relay.per_ip.pop(ip, None)
        return ws

    async def host_ws(request: web.Request) -> web.WebSocketResponse:
        return await serve(request, "host")

    async def client_ws(request: web.Request) -> web.WebSocketResponse:
        return await serve(request, "client")

    async def health(request: web.Request) -> web.Response:
        # Public: must not reveal whether anyone (e.g. your Mac) is online.
        return web.json_response({"ok": True})

    app.router.add_get(f"{prefix}/ws/host", host_ws)
    app.router.add_get(f"{prefix}/ws/client", client_ws)
    app.router.add_get(f"{prefix}/health", health)

    if web_dir is not None and (web_dir / "index.html").exists():
        _add_static(app, web_dir, prefix)
    app[RELAY_KEY] = relay

    async def close_all(_: web.Application) -> None:
        # Tell every peer we are going away so they reconnect right away
        # instead of waiting for a dead socket to time out.
        for room in list(relay.rooms.values()):
            for peer in [room.host, *room.clients.values()]:
                if peer is not None:
                    peer.close_soon(CLOSE_GOING_AWAY, b"relay restarting")

    app.on_shutdown.append(close_all)
    return app


async def _run_host(relay: Relay, ws: web.WebSocketResponse, room_id: str) -> None:
    room = relay.rooms.get(room_id)
    if room is None:
        if len(relay.rooms) >= MAX_ROOMS:
            await ws.close(code=CLOSE_FULL, message=b"relay full")
            return
        room = relay.rooms[room_id] = Room(room_id)
    if room.host is not None:  # a restarted Mac replaces its stale connection
        room.host.close_soon(CLOSE_REPLACED, b"replaced")

    host = Peer(ws)
    room.host = host
    writer = asyncio.create_task(_writer(host))
    host.push({"t": "ready"})
    for cid, client in room.clients.items():
        host.push({"t": "join", "c": cid})
        client.push({"t": "presence", "host": True})
    log.info("host up (room %s…)", room_id[:4])

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT or not host.allow():
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict) or data.get("t") != "msg" or not isinstance(data.get("d"), str):
                continue
            target = data.get("c")
            recipients = list(room.clients.items()) if target == "*" else (
                [(target, room.clients[target])] if target in room.clients else []
            )
            for cid, client in recipients:
                if not client.push(data["d"]):
                    client.close_soon(CLOSE_SLOW, b"slow consumer")  # it will reconnect and resync
    finally:
        if room.host is host:
            room.host = None
            for client in room.clients.values():
                client.push({"t": "presence", "host": False})
        host.push(None)
        await asyncio.gather(writer, return_exceptions=True)
        _cleanup_room(relay, room)
        log.info("host down (room %s…)", room_id[:4])


async def _run_client(relay: Relay, ws: web.WebSocketResponse, room_id: str) -> None:
    room = relay.rooms.get(room_id)
    if room is None:
        # Nothing to attach to: tell the phone the Mac is offline, don't create state.
        await ws.send_str('{"t":"presence","host":false}')
        await ws.close(code=CLOSE_NO_ROOM, message=b"offline")
        return
    if len(room.clients) >= MAX_CLIENTS_PER_ROOM:
        await ws.close(code=CLOSE_FULL, message=b"too many clients")
        return

    cid = secrets.token_hex(6)
    client = Peer(ws)
    room.clients[cid] = client
    writer = asyncio.create_task(_writer(client))
    client.push({"t": "presence", "host": room.host is not None})
    if room.host:
        room.host.push({"t": "join", "c": cid})

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            if not client.allow():
                client.close_soon(CLOSE_RATE, b"rate limit")
                break
            if room.host is None:
                client.push({"t": "presence", "host": False})
            elif not room.host.push({"t": "msg", "c": cid, "d": msg.data}):
                client.push({"t": "presence", "host": False})
    finally:
        room.clients.pop(cid, None)
        if room.host:
            room.host.push({"t": "leave", "c": cid})
        client.push(None)
        await asyncio.gather(writer, return_exceptions=True)
        _cleanup_room(relay, room)


# -- static UI --------------------------------------------------------------------

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self' wss:; manifest-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}
MODE_META = b'<meta name="agent8s-mode" content="relay">'


def _add_static(app: web.Application, web_dir: Path, prefix: str) -> None:
    root = web_dir.resolve()
    index = (root / "index.html").read_bytes().replace(b"</head>", MODE_META + b"</head>", 1)

    async def index_handler(request: web.Request) -> web.Response:
        return web.Response(body=index, content_type="text/html", charset="utf-8",
                            headers={**SECURITY_HEADERS, "Cache-Control": "no-store"})

    async def file_handler(request: web.Request) -> web.StreamResponse:
        target = (root / request.match_info["path"]).resolve()
        if root not in target.parents or not target.is_file():
            raise web.HTTPNotFound()
        immutable = "assets" in target.relative_to(root).parts
        return web.FileResponse(target, headers={
            **SECURITY_HEADERS,
            "Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache",
        })

    app.router.add_get(f"{prefix}/", index_handler)
    if prefix:
        async def redirect(request: web.Request) -> web.Response:
            raise web.HTTPPermanentRedirect(f"{prefix}/")
        app.router.add_get(prefix, redirect)
    app.router.add_get(prefix + "/{path:.+}", file_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="agent8s blind relay")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--prefix", default="/agent8s", help="URL prefix the reverse proxy forwards")
    parser.add_argument("--web-dir", type=Path, default=None, help="built UI bundle to serve")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    web.run_app(make_app(args.web_dir, args.prefix), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
