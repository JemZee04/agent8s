import asyncio
import json
import secrets

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from agent8s.desktop.remote import RelayLink, RemoteManager, validate_relay_url
from agent8s.desktop.remote_crypto import C2H, H2C, derive, new_key, open_frame, seal
from agent8s.desktop.runner import Hub, UserError
from agent8s.relay.server import make_app

KEY = bytes(range(32))


class Phone:
    """A phone: the client half of the protocol, in Python."""

    def __init__(self, session, url, key=KEY):
        self.session, self.url = session, url
        self.room, self.enc = derive(key)
        self.n = 0
        self.sn = None
        self.events = []
        self.pending = {}
        self.ws = None
        self.presence = None
        self._next = 0

    async def connect(self):
        self.ws = await self.session.ws_connect(self.url)
        await self.ws.send_str(json.dumps({"t": "hello", "room": self.room}))
        self._reader = asyncio.create_task(self._read())
        self.cn = secrets.token_hex(8)
        await self.ws.send_str(seal(self.enc, self.room, C2H, {"k": "hello", "cn": self.cn}))
        for _ in range(100):
            if self.sn:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("no welcome")

    async def _read(self):
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                continue
            if msg.data.startswith("{"):
                self.presence = json.loads(msg.data).get("host")
                continue
            obj = open_frame(self.enc, self.room, H2C, msg.data)
            if not obj:
                continue
            if obj["k"] == "welcome" and obj["cn"] == self.cn:
                self.sn = obj["sn"]
            elif obj["k"] == "res" and obj["id"] in self.pending:
                self.pending.pop(obj["id"]).set_result(obj)
            elif obj["k"] == "ev":
                self.events.extend(obj["e"])

    def frame(self, method, path, body=None, *, n=None, sn=None, rid=None):
        self.n += 1
        self._next += 1
        rid = rid if rid is not None else self._next
        return rid, seal(self.enc, self.room, C2H, {
            "k": "req", "sn": sn or self.sn, "n": n if n is not None else self.n, "id": rid, "m": method, "p": path, "b": body,
        })

    async def request(self, method, path, body=None):
        rid, frame = self.frame(method, path, body)
        fut = self.pending[rid] = asyncio.get_running_loop().create_future()
        await self.ws.send_str(frame)
        return await asyncio.wait_for(fut, 5)


@pytest.fixture
async def stack():
    calls = []

    async def bootstrap(request):
        calls.append(("GET", request.path, dict(request.headers)))
        return web.json_response({"chats": [1, 2]})

    async def send(request):
        calls.append(("POST", request.path, await request.json()))
        return web.json_response({"ok": True}, status=202)

    local = web.Application()
    local.router.add_get("/api/bootstrap", bootstrap)
    local.router.add_post("/api/chats/1/send", send)
    local_server = TestServer(local)
    await local_server.start_server()
    relay_server = TestServer(make_app(None, "/agent8s"))
    await relay_server.start_server()

    hub = Hub()
    link = RelayLink(f"http://127.0.0.1:{relay_server.port}/agent8s", KEY, hub,
                     f"http://127.0.0.1:{local_server.port}", "local-token")
    link.start()
    for _ in range(100):
        if link.state == "connected":
            break
        await asyncio.sleep(0.02)
    assert link.state == "connected", link.error
    session = aiohttp.ClientSession()
    ws_url = f"http://127.0.0.1:{relay_server.port}/agent8s/ws/client"
    yield link, hub, session, ws_url, calls
    await session.close()
    await link.stop()
    await relay_server.close()
    await local_server.close()


async def test_phone_requests_reach_the_local_api_and_events_come_back(stack):
    link, hub, session, url, calls = stack
    phone = Phone(session, url)
    await phone.connect()

    res = await phone.request("GET", "/api/bootstrap")
    assert res["s"] == 200 and res["b"] == {"chats": [1, 2]}
    assert calls[0][2]["X-Agent8s-Token"] == "local-token"  # the phone never sees the local token

    res = await phone.request("POST", "/api/chats/1/send", {"text": "hi"})
    assert res["s"] == 202 and calls[1] == ("POST", "/api/chats/1/send", {"text": "hi"})

    hub.publish({"t": "msg_status", "chat_id": 1, "msg_id": 2, "status": "done"})
    hub.publish({"t": "diff_changed", "chat_id": 1})
    for _ in range(100):
        if len(phone.events) >= 2:
            break
        await asyncio.sleep(0.02)
    assert [e["t"] for e in phone.events] == ["msg_status", "diff_changed"]
    assert link.status()["clients"] == 1


async def test_only_whitelisted_endpoints_are_reachable(stack):
    _, _, session, url, calls = stack
    phone = Phone(session, url)
    await phone.connect()
    for method, path in [("GET", "/api/remote"), ("POST", "/api/chats/1/open"), ("GET", "/etc/passwd"),
                         ("GET", "/api/bootstrap?x=1"), ("GET", "/api/../secret"), ("PUT", "/api/bootstrap"),
                         ("GET", "http://evil.example/api/bootstrap"), ("GET", None)]:
        res = await phone.request(method, path)
        assert res["s"] == 403, (method, path)
    assert calls == []  # none of them reached the local server


async def test_replayed_stale_and_forged_frames_are_ignored(stack):
    _, _, session, url, calls = stack
    phone = Phone(session, url)
    await phone.connect()
    rid, frame = phone.frame("POST", "/api/chats/1/send", {"text": "once"})
    fut = phone.pending[rid] = asyncio.get_running_loop().create_future()
    await phone.ws.send_str(frame)
    await asyncio.wait_for(fut, 5)
    assert len(calls) == 1

    await phone.ws.send_str(frame)  # exact replay of a valid frame
    await asyncio.sleep(0.3)
    assert len(calls) == 1

    _, old_counter = phone.frame("POST", "/api/chats/1/send", {"text": "x"}, n=1)  # counter not increasing
    await phone.ws.send_str(old_counter)
    await asyncio.sleep(0.3)
    assert len(calls) == 1

    rid, stale = phone.frame("GET", "/api/bootstrap", sn="0" * 32)  # nonce from another connection
    fut = phone.pending[rid] = asyncio.get_running_loop().create_future()
    await phone.ws.send_str(stale)
    assert (await asyncio.wait_for(fut, 5))["s"] == 401
    assert len(calls) == 1

    attacker = Phone(session, url, key=new_key())  # right room is unknown to it; same relay, wrong key
    attacker.room = phone.room  # even knowing the room id...
    attacker.n = 0
    ws = await session.ws_connect(url)
    await ws.send_str(json.dumps({"t": "hello", "room": phone.room}))
    await ws.send_str(seal(attacker.enc, phone.room, C2H, {"k": "hello", "cn": "evil"}))
    await ws.send_str(seal(attacker.enc, phone.room, C2H,
                           {"k": "req", "sn": phone.sn, "n": 99, "id": 1, "m": "POST", "p": "/api/chats/1/send", "b": {"text": "pwn"}}))
    await asyncio.sleep(0.4)
    assert len(calls) == 1  # ...a forged frame is dropped without effect
    await ws.close()


async def test_a_second_phone_gets_its_own_session(stack):
    _, _, session, url, calls = stack
    a, b = Phone(session, url), Phone(session, url)
    await a.connect()
    await b.connect()
    assert a.sn != b.sn
    assert (await a.request("GET", "/api/bootstrap"))["s"] == 200
    assert (await b.request("GET", "/api/bootstrap"))["s"] == 200
    # a's nonce is useless on b's connection
    rid, frame = b.frame("GET", "/api/bootstrap", sn=a.sn)
    fut = b.pending[rid] = asyncio.get_running_loop().create_future()
    await b.ws.send_str(frame)
    assert (await asyncio.wait_for(fut, 5))["s"] == 401


async def test_link_reconnects_after_the_relay_restarts():
    relay = TestServer(make_app(None, "/agent8s"))
    await relay.start_server()
    port = relay.port
    link = RelayLink(f"http://127.0.0.1:{port}/agent8s", KEY, Hub(), "http://127.0.0.1:9", "t")
    link.start()
    for _ in range(100):
        if link.state == "connected":
            break
        await asyncio.sleep(0.02)
    assert link.state == "connected"
    await relay.close()
    for _ in range(100):
        if link.state != "connected":
            break
        await asyncio.sleep(0.02)
    assert link.state == "connecting"
    await link.stop()


async def test_manager_pairing_lifecycle(tmp_path):
    manager = RemoteManager(tmp_path, Hub(), 1, "t")
    assert manager.configured() is False and manager.pairing_url() is None
    with pytest.raises(UserError):
        await manager.pair("http://insecure.example/agent8s")
    with pytest.raises(UserError):
        await manager.pair(None)

    relay = TestServer(make_app(None, "/agent8s"))
    await relay.start_server()
    url = f"http://127.0.0.1:{relay.port}/agent8s"
    info = await manager.pair(url)
    first = manager.pairing_url()
    assert info["configured"] and first.startswith(url + "/#k=")
    assert (tmp_path / "remote.json").stat().st_mode & 0o777 == 0o600  # the key never world-readable

    await manager.pair(url)  # re-pairing rotates the key: old phones stop working
    assert manager.pairing_url() != first
    await manager.disable()
    assert manager.configured() is False and not (tmp_path / "remote.json").exists()
    await manager.shutdown()
    await relay.close()


def test_relay_url_validation():
    assert validate_relay_url("https://example.com/agent8s/") == "https://example.com/agent8s"
    assert validate_relay_url("http://127.0.0.1:8765/x") == "http://127.0.0.1:8765/x"
    for bad in ("http://example.com", "ftp://example.com", "example.com", "", "https://"):
        with pytest.raises(UserError):
            validate_relay_url(bad)
