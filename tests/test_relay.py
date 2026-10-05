import asyncio
import json

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from agent8s.relay import server as relay_server
from agent8s.relay.server import make_app

ROOM = "ab" * 16


@pytest.fixture
async def client(tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html><head><title>x</title></head><body>ui</body></html>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("nope")
    c = TestClient(TestServer(make_app(web, "/agent8s")))
    await c.start_server()
    yield c
    await c.close()


async def join(client, role, room=ROOM):
    ws = await client.ws_connect(f"/agent8s/ws/{role}")
    await ws.send_str(json.dumps({"t": "hello", "room": room}))
    return ws


async def recv(ws, timeout=2):
    msg = await asyncio.wait_for(ws.receive(), timeout)
    assert msg.type == WSMsgType.TEXT, msg
    return msg.data


async def test_frames_flow_both_ways_and_presence(client):
    host = await join(client, "host")
    assert json.loads(await recv(host)) == {"t": "ready"}
    phone = await join(client, "client")
    assert json.loads(await recv(phone)) == {"t": "presence", "host": True}
    join_msg = json.loads(await recv(host))
    assert join_msg["t"] == "join"
    cid = join_msg["c"]

    await phone.send_str("opaque-up")
    assert json.loads(await recv(host)) == {"t": "msg", "c": cid, "d": "opaque-up"}

    await host.send_str(json.dumps({"t": "msg", "c": cid, "d": "opaque-down"}))
    assert await recv(phone) == "opaque-down"
    await host.send_str(json.dumps({"t": "msg", "c": "*", "d": "broadcast"}))
    assert await recv(phone) == "broadcast"

    await host.close()
    assert json.loads(await recv(phone)) == {"t": "presence", "host": False}
    host2 = await join(client, "host")
    assert json.loads(await recv(phone)) == {"t": "presence", "host": True}
    await recv(host2)  # ready
    assert json.loads(await recv(host2))["t"] == "join"  # the phone is re-announced to the new host
    await phone.close()
    await host2.close()


async def test_bad_hello_is_rejected(client, monkeypatch):
    for payload in ('{"t":"hello","room":"short"}', "not json", '{"t":"nope"}', json.dumps({"t": "hello", "room": 5})):
        ws = await client.ws_connect("/agent8s/ws/host")
        await ws.send_str(payload)
        await ws.receive()
        assert ws.close_code == relay_server.CLOSE_BAD_HELLO

    monkeypatch.setattr(relay_server, "HELLO_TIMEOUT", 0.2)
    ws = await client.ws_connect("/agent8s/ws/client")  # never says hello
    await asyncio.wait_for(ws.receive(), 2)
    assert ws.close_code == relay_server.CLOSE_BAD_HELLO


async def test_client_without_a_host_gets_offline_not_state(client):
    ws = await join(client, "client")
    assert json.loads(await recv(ws)) == {"t": "presence", "host": False}
    await ws.receive()
    assert ws.close_code == relay_server.CLOSE_NO_ROOM
    assert client.server.app[relay_server.RELAY_KEY].stats()["rooms"] == 0
    assert await (await client.get("/agent8s/health")).json() == {"ok": True}  # reveals nothing about presence


async def test_a_new_host_replaces_the_old_one(client):
    old = await join(client, "host")
    await recv(old)
    new = await join(client, "host")
    await recv(new)
    await old.receive()
    assert old.close_code == relay_server.CLOSE_REPLACED
    phone = await join(client, "client")
    assert json.loads(await recv(phone))["host"] is True
    await phone.close()
    await new.close()


async def test_limits(client, monkeypatch):
    monkeypatch.setattr(relay_server, "MAX_CLIENTS_PER_ROOM", 1)
    host = await join(client, "host")
    await recv(host)
    first = await join(client, "client")
    await recv(first)
    second = await join(client, "client")
    await second.receive()
    assert second.close_code == relay_server.CLOSE_FULL

    monkeypatch.setattr(relay_server, "RATE_BURST", 3)
    monkeypatch.setattr(relay_server, "RATE_PER_SECOND", 0.001)
    flooder = await join(client, "host", room="cd" * 16)
    await recv(flooder)
    flood_client = await join(client, "client", room="cd" * 16)
    await recv(flood_client)
    for i in range(20):
        await flood_client.send_str(f"x{i}")
    while True:
        msg = await asyncio.wait_for(flood_client.receive(), 2)
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
            break
    assert flood_client.close_code == relay_server.CLOSE_RATE


async def test_room_cap(client, monkeypatch):
    monkeypatch.setattr(relay_server, "MAX_ROOMS", 1)
    first = await join(client, "host", room="11" * 16)
    await recv(first)
    second = await join(client, "host", room="22" * 16)
    await second.receive()
    assert second.close_code == relay_server.CLOSE_FULL


async def test_static_ui_is_served_safely(client, tmp_path):
    index = await client.get("/agent8s/")
    body = await index.text()
    assert 'name="agent8s-mode" content="relay"' in body
    assert "script-src 'self'" in index.headers["Content-Security-Policy"]
    assert index.headers["X-Robots-Tag"].startswith("noindex")
    assert index.headers["Cache-Control"] == "no-store"

    asset = await client.get("/agent8s/assets/app.js")
    assert asset.status == 200 and "immutable" in asset.headers["Cache-Control"]

    for evil in ("/agent8s/..%2Fsecret.txt", "/agent8s/%2e%2e/secret.txt", "/agent8s/assets/../../secret.txt"):
        assert (await client.get(evil)).status in (404, 400)
    assert (await client.get("/agent8s", allow_redirects=False)).status == 308
    assert (await client.get("/")).status == 404  # nothing outside the prefix
