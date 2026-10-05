import asyncio
import json
import socket
import subprocess

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent8s.config import DesktopConfig
from agent8s.db import Database
from agent8s.desktop.remote import RemoteManager
from agent8s.desktop.runner import Hub, Orchestrator
from agent8s.desktop.server import create_app
from agent8s.desktop.store import Store

TOKEN = "t0ken"


class ScriptedDriver:
    def __init__(self, events):
        self.events = events

    async def run(self, req):
        for event in self.events:
            if event == "hang":
                await asyncio.sleep(3600)
            yield event


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def ctx(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    config = DesktopConfig(tmp_path / "data", tmp_path / "wt", tmp_path, [], "acceptEdits", "workspace-write", 60)
    config.data_dir.mkdir()
    db, store, hub = Database(config.db_path), Store(config.db_path), Hub()
    orch = Orchestrator(config, db, store, hub)
    orch.catalog = {"agents": [{"id": "claude", "label": "Claude Code", "models": [{"id": "", "label": "d"}]}]}
    orch._drivers = {"claude": ScriptedDriver([
        {"type": "session", "id": "S"}, {"type": "text", "text": "hel"}, {"type": "text", "text": "lo"},
        {"type": "done", "ok": True, "error": None},
    ])}
    port = free_port()
    remote = RemoteManager(config.data_dir, hub, port, TOKEN)
    app = create_app(config, db, orch, hub, TOKEN, port, None, remote)
    server = TestServer(app, host="127.0.0.1", port=port)
    client = TestClient(server)
    await client.start_server()
    yield client, orch, repo, port
    await orch.shutdown()
    await client.close()


def auth(extra=None):
    return {"X-Agent8s-Token": TOKEN, **(extra or {})}


async def test_requests_without_valid_token_are_rejected(ctx):
    client, *_ = ctx
    assert (await client.get("/api/bootstrap")).status == 401
    assert (await client.get("/api/bootstrap", headers={"X-Agent8s-Token": "wrong"})).status == 401
    assert (await client.post("/api/chats", json={}, headers={"X-Agent8s-Token": ""})).status == 401
    assert (await client.get("/api/bootstrap", headers=auth())).status == 200


async def test_foreign_origin_and_rebinding_host_are_rejected(ctx):
    client, _, _, port = ctx
    assert (await client.get("/api/bootstrap", headers=auth({"Origin": "http://evil.example"}))).status == 403
    assert (await client.get("/api/bootstrap", headers=auth({"Host": "evil.example"}))).status == 403
    assert (await client.get("/", headers={"Host": "evil.example"})).status == 403
    assert (await client.get("/api/bootstrap", headers=auth({"Origin": f"http://127.0.0.1:{port}"}))).status == 200
    # the WebSocket needs the same protection (cross-site WebSocket hijacking)
    assert (await client.get("/ws", headers={"Origin": "http://evil.example"})).status in (401, 403)
    assert (await client.get(f"/ws?token={TOKEN}", headers={"Origin": "http://evil.example"})).status == 403


async def test_index_is_served_with_a_strict_csp(ctx):
    client, *_ = ctx
    response = await client.get("/")
    assert response.status == 200
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


async def test_chat_flow_and_event_stream(ctx):
    client, orch, repo, _ = ctx
    ws = await client.ws_connect(f"/ws?token={TOKEN}")

    r = await client.post("/api/projects", json={"name": "proj", "path": str(repo)}, headers=auth())
    assert r.status == 201
    project = await r.json()
    r = await client.post("/api/chats", json={"project_id": project["id"], "agent": "claude"}, headers=auth())
    chat = await r.json()
    assert r.status == 201

    r = await client.post(f"/api/chats/{chat['id']}/send", json={"text": "hi"}, headers=auth())
    assert r.status == 202

    seen = []
    while True:
        event = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
        seen.append(event)
        if event["t"] == "msg_status":
            break
    kinds = [e["t"] for e in seen]
    assert kinds.index("chat") < kinds.index("msg_new") < kinds.index("msg_ops") < kinds.index("msg_status")
    revs = [e["rev"] for e in seen if e["t"] == "msg_ops"]
    assert revs == sorted(revs) and len(set(revs)) == len(revs)  # strictly increasing: clients can dedupe
    assert seen[-1]["status"] == "done"

    data = await (await client.get(f"/api/chats/{chat['id']}", headers=auth())).json()
    assert data["messages"][-1]["parts"][0]["text"] == "hello"
    assert data["chat"]["title"] == "hi"
    await ws.close()


async def test_busy_and_validation_errors_are_reported_cleanly(ctx):
    client, orch, repo, _ = ctx
    orch._drivers["claude"] = ScriptedDriver([{"type": "session", "id": "S"}, "hang"])
    project = await (await client.post("/api/projects", json={"name": "p", "path": str(repo)}, headers=auth())).json()
    chat = await (await client.post("/api/chats", json={"project_id": project["id"], "agent": "claude"}, headers=auth())).json()
    path = f"/api/chats/{chat['id']}"

    assert (await client.post(f"{path}/send", json={"text": "go"}, headers=auth())).status == 202
    await asyncio.sleep(0.05)
    busy = await client.post(f"{path}/send", json={"text": "again"}, headers=auth())
    assert busy.status == 409 and "error" in await busy.json()
    assert (await client.patch(path, json={"agent": "claude", "model": "x"}, headers=auth())).status == 409
    assert (await client.delete(path, headers=auth())).status == 409

    stopped = await (await client.post(f"{path}/stop", headers=auth())).json()
    assert stopped == {"stopped": True}

    assert (await client.post(f"{path}/send", data="not json", headers=auth())).status == 400
    assert (await client.post(f"{path}/send", json={"text": "   "}, headers=auth())).status == 400
    assert (await client.get("/api/chats/999", headers=auth())).status == 400
    assert (await client.get("/api/chats/abc", headers=auth())).status == 404
    assert (await client.post("/api/projects", json={"name": "x", "path": "/definitely/not/here"}, headers=auth())).status == 400



async def test_pairing_endpoints(ctx):
    client, *_ = ctx
    info = await (await client.get("/api/remote", headers=auth())).json()
    assert info["configured"] is False and info["state"] == "stopped"
    assert (await client.get("/api/remote/pairing", headers=auth())).status == 400  # nothing paired yet

    insecure = await client.post("/api/remote/pair", json={"relay": "http://example.com/agent8s"}, headers=auth())
    assert insecure.status == 400

    paired = await client.post("/api/remote/pair", json={"relay": "http://127.0.0.1:9/agent8s"}, headers=auth())
    assert paired.status == 200 and (await paired.json())["configured"] is True
    pairing = await (await client.get("/api/remote/pairing", headers=auth())).json()
    assert pairing["url"].startswith("http://127.0.0.1:9/agent8s/#k=") and "<svg" in pairing["svg"]

    assert (await client.delete("/api/remote", headers=auth())).status == 200
    assert (await (await client.get("/api/remote", headers=auth())).json())["configured"] is False
    assert (await client.get("/api/remote", headers={})).status == 401  # still behind the token
