import asyncio
import base64
import gzip
import json
import time

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from agent8s.desktop import ports as portscan
from agent8s.desktop.preview import MAX_PREVIEWS, PreviewManager
from agent8s.desktop.remote import RelayLink, RemoteManager
from agent8s.desktop.runner import Hub, UserError
from agent8s.relay import server as relay_server
from agent8s.relay.server import make_app

KEY = bytes(range(32))
ROOM = "ab" * 16
CAP = "cd" * 16


# -- port detection -------------------------------------------------------------------------

LSOF = """p101
cnode
n*:3000
n[::1]:3000
p102
cpostgres
n127.0.0.1:5432
p103
cControlCenter
n192.168.1.5:7000
p104
cVite
n[::1]:5173
"""


def test_parse_lsof_keeps_local_listeners_only():
    ports = {p.port: p for p in portscan.parse_lsof_listeners(LSOF)}
    assert set(ports) == {3000, 5173, 5432}  # 192.168.* is reachable from the LAN, not a local dev server
    assert ports[3000].command == "node" and ports[3000].pid == 101


def test_classify_separates_the_chats_server_dev_servers_and_noise():
    here = "/work/proj"
    assert portscan.classify(portscan.ListeningPort(1, 1, "weirdbin", cwd="/work/proj/api"), here) == "mine"
    assert portscan.classify(portscan.ListeningPort(2, 1, "node", cwd="/elsewhere"), here) == "dev"
    assert portscan.classify(portscan.ListeningPort(3, 1, "Python", cwd="/elsewhere"), here) == "dev"
    assert portscan.classify(portscan.ListeningPort(2, 1, "python3.14", cwd="/elsewhere"), here) == "dev"
    assert portscan.classify(portscan.ListeningPort(6, 1, "goland", cwd="/elsewhere"), here) == "other"  # an IDE, not `go`
    assert portscan.classify(portscan.ListeningPort(4, 1, "ControlCenter", cwd="/"), here) == "other"
    assert portscan.classify(portscan.ListeningPort(5, 1, "OrbStack Helper"), here) == "other"


def test_belongs_to_matches_the_chat_folder_and_its_subfolders():
    p = portscan.ListeningPort(3000, 1, "node", cwd="/work/proj/web")
    assert portscan.belongs_to(p, "/work/proj") and portscan.belongs_to(p, "/work/proj/web")
    assert not portscan.belongs_to(p, "/work/other") and not portscan.belongs_to(p, "/work/pro")
    assert not portscan.belongs_to(portscan.ListeningPort(1, 1, "x", cwd=""), "/work/proj")


# -- a fake dev server --------------------------------------------------------------------------


@pytest.fixture
async def devserver():
    seen = {}

    async def index(request):
        seen["host"] = request.headers.get("Host")
        return web.Response(text='<script src="/app.js"></script>hello', content_type="text/html")

    async def redirect(request):
        raise web.HTTPFound(f"http://localhost:{server.port}/target?x=1")

    async def cookie(request):
        r = web.Response(text="c")
        r.headers.add("Set-Cookie", "sid=abc; Domain=localhost; Path=/")
        r.headers.add("Set-Cookie", "theme=dark; Path=/")
        return r

    async def gz(request):
        return web.Response(body=gzip.compress(b"zipped body"), headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"})

    async def echo(request):
        seen["cookie"] = request.headers.get("Cookie")
        seen["x"] = request.headers.get("X-Custom")
        return web.json_response({"body": (await request.read()).decode(), "q": request.query_string, "method": request.method})

    async def big(request):
        return web.Response(body=b"x" * (6 * 1024 * 1024))

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/cookie", cookie)
    app.router.add_get("/gz", gz)
    app.router.add_get("/big", big)
    app.router.add_route("*", "/echo", echo)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    server.seen = seen
    yield server
    await server.close()


def request_msg(cap, path, method="GET", headers=None, body=b""):
    return {"t": "http", "id": 7, "cap": cap, "m": method, "p": path, "h": headers or [], "b": base64.b64encode(body).decode()}


async def test_manager_validates_ports(devserver):
    mgr = PreviewManager(Hub(), own_ports={9999})
    with pytest.raises(UserError, match="(?i)порт"):
        mgr.create(1, 0)
    with pytest.raises(UserError, match="agent8s"):
        mgr.create(1, 9999)
    with pytest.raises(UserError, match="agent8s"):
        mgr.create(1, 8731)  # the background service's port
    with pytest.raises(UserError, match="ничего не слушает"):
        mgr.create(1, 1)  # nothing listens on port 1
    first = mgr.create(1, devserver.port)
    assert mgr.create(1, devserver.port).cap == first.cap  # same site again: same link
    assert mgr.create(2, devserver.port).cap != first.cap  # a different chat gets its own
    assert len(first.cap) == 32 and mgr.remove(first.cap) and not mgr.remove(first.cap)


async def test_manager_limits_and_expires(devserver):
    mgr = PreviewManager(Hub(), set(), ttl=0.05)
    mgr.create(1, devserver.port)
    await asyncio.sleep(0.1)
    assert mgr.list() == []  # expired links disappear
    mgr = PreviewManager(Hub(), set())
    for chat in range(MAX_PREVIEWS):
        mgr.create(chat, devserver.port)
    with pytest.raises(UserError, match="не больше"):
        mgr.create(99, devserver.port)


async def test_events_tell_phones_about_new_previews(devserver):
    hub = Hub()
    queue = hub.subscribe()
    mgr = PreviewManager(hub, set(), url_for=lambda cap: f"https://x/p/{cap}/")
    preview = mgr.create(3, devserver.port)
    event = queue.get_nowait()
    assert event["t"] == "preview" and event["op"] == "add" and event["chat_id"] == 3
    assert event["url"] == f"https://x/p/{preview.cap}/"
    mgr.remove(preview.cap)
    assert queue.get_nowait()["op"] == "del"


async def test_fetch_forwards_requests_and_rewrites_what_breaks_behind_a_proxy(devserver):
    mgr = PreviewManager(Hub(), set())
    cap = mgr.create(1, devserver.port).cap
    try:
        res = await mgr.fetch(request_msg(cap, "/"))
        assert res["s"] == 200 and base64.b64decode(res["b"]) == b'<script src="/app.js"></script>hello'
        assert devserver.seen["host"] == f"localhost:{devserver.port}"  # dev servers reject foreign Host headers

        res = await mgr.fetch(request_msg(cap, "/redirect"))
        assert res["s"] == 302 and dict(res["h"])["Location"] == "/target?x=1"  # stays inside the preview

        res = await mgr.fetch(request_msg(cap, "/cookie"))
        cookies = [v for k, v in res["h"] if k == "Set-Cookie"]
        assert cookies == ["sid=abc; Path=/", "theme=dark; Path=/"]  # Domain=localhost would never stick

        res = await mgr.fetch(request_msg(cap, "/gz"))
        assert dict(res["h"])["Content-Encoding"] == "gzip" and gzip.decompress(base64.b64decode(res["b"])) == b"zipped body"

        res = await mgr.fetch(request_msg(cap, "/echo?a=b", "POST", [["Cookie", "k=v"], ["X-Custom", "1"], ["Connection", "close"]], b"payload"))
        assert json.loads(base64.b64decode(res["b"])) == {"body": "payload", "q": "a=b", "method": "POST"}
        assert devserver.seen["cookie"] == "k=v" and devserver.seen["x"] == "1"

        assert (await mgr.fetch(request_msg(cap, "/big")))["t"] == "httperr"  # would not fit a relay frame
    finally:
        await mgr.close()


async def test_fetch_refuses_unknown_expired_and_malformed_requests(devserver):
    mgr = PreviewManager(Hub(), set(), ttl=0.05)
    cap = mgr.create(1, devserver.port).cap
    assert (await mgr.fetch(request_msg("0" * 32, "/")))["t"] == "httperr"
    for bad in (request_msg(cap, "no-slash"), request_msg(cap, "/", method="CONNECT"), {**request_msg(cap, "/"), "b": "***"}):
        assert (await mgr.fetch(bad))["t"] == "httperr"
    mgr._items[cap].exp = time.time() - 1
    assert (await mgr.fetch(request_msg(cap, "/")))["t"] == "httperr"
    await mgr.close()


# -- the relay's HTTP side ---------------------------------------------------------------------


@pytest.fixture
async def relay():
    c = TestClient(TestServer(make_app(None, "/agent8s")))
    await c.start_server()
    yield c
    await c.close()


async def fake_host(client, room=ROOM):
    ws = await client.ws_connect("/agent8s/ws/host")
    await ws.send_str(json.dumps({"t": "hello", "room": room}))
    assert json.loads((await ws.receive()).data) == {"t": "ready"}
    return ws


async def register(ws, cap=CAP, exp=None):
    await ws.send_str(json.dumps({"t": "preview", "op": "add", "cap": cap, "exp": exp or time.time() + 600}))
    await asyncio.sleep(0.05)


async def answer_one(ws, reply):
    """Wait for a forwarded request, send `reply(frame)` back; returns the request frame."""
    frame = json.loads((await asyncio.wait_for(ws.receive(), 3)).data)
    assert frame["t"] == "http"
    await ws.send_str(json.dumps({"id": frame["id"], **reply(frame)}))
    return frame


async def test_entry_link_sets_the_cookie_and_redirects_to_the_site_root(relay):
    host = await fake_host(relay)
    await register(host)
    r = await relay.get(f"/agent8s/p/{CAP}/dash/board?tab=2", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/dash/board?tab=2"
    cookie = r.headers["Set-Cookie"]
    assert cookie.startswith(f"a8p={CAP}") and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
    assert "Domain" not in cookie  # host-only: the cookie never leaks to sibling hostnames

    assert (await relay.get(f"/agent8s/p/{'0' * 32}/", allow_redirects=False)).status == 404
    assert (await relay.get("/agent8s/p/not-a-cap/", allow_redirects=False)).status == 404
    await host.close()


async def test_requests_are_forwarded_to_the_mac_and_answers_come_back(relay):
    host = await fake_host(relay)
    await register(host)
    reply = lambda f: {"t": "httpres", "s": 201, "h": [["Content-Type", "text/plain"], ["Set-Cookie", "sid=1; Path=/"],
                       ["Set-Cookie", f"a8p=evil; Path=/"], ["Transfer-Encoding", "chunked"]],
                       "b": base64.b64encode(b"from the mac").decode()}
    pending = asyncio.create_task(relay.post(
        "/agent8s/pv/api/items?x=1", data=b"form", headers={"Cookie": f"a8p={CAP}; theme=dark", "X-Real-IP": "1.2.3.4", "X-Custom": "yes"}))
    frame = await answer_one(host, reply)
    r = await pending

    assert frame["m"] == "POST" and frame["p"] == "/api/items?x=1" and base64.b64decode(frame["b"]) == b"form"
    headers = {k.lower(): v for k, v in frame["h"]}
    assert headers["cookie"] == "theme=dark"  # the app sees its own cookies, never ours
    assert headers["x-custom"] == "yes" and "x-real-ip" not in headers and "host" not in headers
    assert r.status == 201 and await r.read() == b"from the mac"
    assert r.cookies.get("sid") is not None and r.cookies.get("a8p") is None  # the app cannot swap the preview cookie
    assert r.headers["X-Robots-Tag"].startswith("noindex")
    await host.close()


async def test_proxy_refuses_what_it_cannot_serve(relay):
    cookie = {"Cookie": f"a8p={CAP}"}
    assert (await relay.get("/agent8s/pv/")).status == 404  # no cookie
    assert (await relay.get("/agent8s/pv/", headers={"Cookie": "a8p=" + "0" * 32})).status == 404  # unknown capability
    host = await fake_host(relay)
    await register(host)
    assert (await relay.get("/agent8s/pv/", headers={**cookie, "Upgrade": "websocket", "Connection": "Upgrade"})).status == 501
    big = await relay.post("/agent8s/pv/up", data=b"x" * (1024 * 1024 + 10), headers=cookie)
    assert big.status == 413
    await host.close()
    await asyncio.sleep(0.05)
    assert (await relay.get("/agent8s/pv/", headers=cookie)).status == 404  # host gone: its previews are gone


async def test_preview_expiry_and_ownership(relay):
    host = await fake_host(relay)
    await register(host, exp=time.time() + 0.1)
    await asyncio.sleep(0.2)
    assert (await relay.get("/agent8s/pv/", headers={"Cookie": f"a8p={CAP}"})).status == 404

    # a capability belongs to the room that registered it
    await register(host, cap="11" * 16)
    other = await fake_host(relay, room="ef" * 16)
    await register(other, cap="11" * 16)
    host_gets = asyncio.create_task(relay.get("/agent8s/pv/", headers={"Cookie": "a8p=" + "11" * 16}))
    await answer_one(host, lambda f: {"t": "httpres", "s": 200, "h": [], "b": ""})
    assert (await host_gets).status == 200
    await host.close()
    await other.close()


async def test_a_host_that_vanishes_mid_request_does_not_hang_the_browser(relay):
    host = await fake_host(relay)
    await register(host)
    pending = asyncio.create_task(relay.get("/agent8s/pv/slow", headers={"Cookie": f"a8p={CAP}"}))
    await asyncio.wait_for(host.receive(), 3)  # the request arrives…
    await host.close()  # …and the Mac goes away
    r = await asyncio.wait_for(pending, 5)
    assert r.status == 502


async def test_preview_traffic_is_not_throttled_like_chat_frames(relay, monkeypatch):
    monkeypatch.setattr(relay_server, "RATE_BURST", 2)
    monkeypatch.setattr(relay_server, "RATE_PER_SECOND", 0.001)
    host = await fake_host(relay)
    await register(host)

    async def serve_all():
        for _ in range(8):
            await answer_one(host, lambda f: {"t": "httpres", "s": 200, "h": [], "b": ""})

    server_task = asyncio.create_task(serve_all())
    results = await asyncio.gather(*[relay.get("/agent8s/pv/a", headers={"Cookie": f"a8p={CAP}"}) for _ in range(8)])
    await server_task
    assert [r.status for r in results] == [200] * 8  # a page with many assets must load completely
    await host.close()


# -- the whole chain: browser -> relay -> RelayLink -> PreviewManager -> dev server -----------------


async def test_the_whole_chain_and_reregistration_after_the_relay_restarts(devserver):
    relay_srv = TestServer(make_app(None, "/agent8s"), host="127.0.0.1")
    await relay_srv.start_server()
    port = relay_srv.port
    hub = Hub()
    manager = RemoteManager.__new__(RemoteManager)  # only the preview pieces are under test here
    previews = PreviewManager(hub, set())
    link = RelayLink(f"http://127.0.0.1:{port}/agent8s", KEY, hub, "http://127.0.0.1:9", "t", previews)
    link.start()
    for _ in range(100):
        if link.state == "connected":
            break
        await asyncio.sleep(0.02)
    assert link.state == "connected"

    preview = previews.create(1, devserver.port)  # created AFTER connecting: announced live
    async with aiohttp.ClientSession(cookies={"a8p": preview.cap}) as browser:
        async with browser.get(f"http://127.0.0.1:{port}/agent8s/pv/") as r:
            assert r.status == 200 and "hello" in await r.text()
        async with browser.get(f"http://127.0.0.1:{port}/agent8s/pv/redirect", allow_redirects=False) as r:
            assert r.headers["Location"] == "/target?x=1"

    # the relay restarts and forgets everything; the link reconnects and registers the preview again
    await relay_srv.close()
    relay_srv = TestServer(make_app(None, "/agent8s"), host="127.0.0.1", port=port)
    await relay_srv.start_server()
    for _ in range(300):
        if link.state == "connected" and relay_srv.app[relay_server.RELAY_KEY].previews:
            break
        await asyncio.sleep(0.05)
    async with aiohttp.ClientSession(cookies={"a8p": preview.cap}) as browser:
        async with browser.get(f"http://127.0.0.1:{port}/agent8s/pv/") as r:
            assert r.status == 200
    await link.stop()
    await previews.close()
    await relay_srv.close()


def test_preview_url_uses_a_different_origin_than_the_app(tmp_path):
    import json as _json

    m = RemoteManager(tmp_path, Hub(), 1, "t")
    with pytest.raises(UserError, match="телефон"):
        m.preview_url(CAP)  # not paired yet

    def paired(relay, origin=None):
        (tmp_path / "remote.json").write_text(_json.dumps({"relay": relay, "key": "k"}))
        mgr = RemoteManager(tmp_path, Hub(), 1, "t", preview_origin=origin)
        return mgr

    assert paired("https://example.com/agent8s").preview_url(CAP) == f"https://www.example.com/agent8s/p/{CAP}/"
    assert paired("https://example.com/agent8s", "https://preview.example.com/").preview_url(CAP) == f"https://preview.example.com/agent8s/p/{CAP}/"
    assert paired("http://127.0.0.1:8770/agent8s").preview_url(CAP) == f"http://127.0.0.1:8770/agent8s/p/{CAP}/"
    with pytest.raises(UserError, match="AGENT8S_PREVIEW_ORIGIN"):
        paired("https://www.example.com/agent8s").preview_url(CAP)  # no second name to derive


async def test_web_probe_tells_sites_from_everything_else(devserver):
    import socket as _socket
    from agent8s.desktop.preview import is_web_page

    assert await is_web_page(devserver.port) is True  # serves HTML at /

    async def api(request):
        return web.json_response({"ok": True})

    async def denied(request):
        return web.Response(status=403, text="no", content_type="text/html")

    for handler, expected in ((api, False), (denied, False)):
        app = web.Application()
        app.router.add_get("/", handler)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        assert await is_web_page(server.port) is expected  # JSON API / AirPlay-style 403: not a page to open
        await server.close()

    raw = _socket.socket()  # something that listens but does not speak HTTP (a database)
    raw.bind(("127.0.0.1", 0))
    raw.listen()
    assert await is_web_page(raw.getsockname()[1]) is False
    raw.close()
    assert await is_web_page(1) is False  # nothing listening
