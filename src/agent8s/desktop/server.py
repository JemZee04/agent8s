from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import WSMsgType, web

from ..config import DesktopConfig
from ..db import Database
from .runner import Busy, Hub, Orchestrator, UserError

log = logging.getLogger("agent8s.desktop")

WEB_DIR = Path(__file__).parent / "web"
TOKEN_HEADER = "X-Agent8s-Token"

NO_UI_PAGE = """<!doctype html><meta charset="utf-8"><title>agent8s</title>
<body style="font:15px system-ui;max-width:40em;margin:4em auto;padding:0 1em">
<h2>Интерфейс ещё не собран</h2>
<p>Бэкенд работает, но нет собранного фронтенда. Выполни:</p>
<pre>cd desktop-ui &amp;&amp; npm install &amp;&amp; npm run build</pre>
</body>"""

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def make_security_middleware(token: str, port: int, extra_origins: list[str]):
    hosts = _allowed_hosts(port)
    origins = {f"http://{h}" for h in hosts} | set(extra_origins)

    @web.middleware
    async def security(request: web.Request, handler: Handler) -> web.StreamResponse:
        # The server executes coding agents on this machine, so it must be
        # unreachable for any web page the user happens to have open:
        #  - Host check defeats DNS rebinding,
        #  - Origin check defeats cross-site requests and WebSocket hijacking,
        #  - the per-launch token (never written to disk) authenticates the UI.
        if request.host not in hosts:
            raise web.HTTPForbidden(text="bad host")
        if request.path.startswith(("/api/", "/ws")):
            # Browsers always attach Origin to cross-site and WebSocket
            # requests; non-browser clients (curl) send none and rely on the token.
            origin = request.headers.get("Origin")
            if origin is not None and origin not in origins:
                raise web.HTTPForbidden(text="bad origin")
            supplied = request.headers.get(TOKEN_HEADER) or request.query.get("token", "")
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                raise web.HTTPUnauthorized(text="bad token")
        return await handler(request)

    return security


@web.middleware
async def errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except UserError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Busy:
        return web.json_response({"error": "Агент ещё работает в этом чате — дождись ответа или нажми «Стоп»."}, status=409)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("unhandled error in %s %s", request.method, request.path)
        return web.json_response({"error": "Внутренняя ошибка сервера (подробности в логе)."}, status=500)


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise UserError("Тело запроса должно быть JSON.")
    if not isinstance(data, dict):
        raise UserError("Тело запроса должно быть JSON-объектом.")
    return data


def _str(data: dict[str, Any], key: str, default: str | None = None) -> str | None:
    value = data.get(key, default)
    if value is not None and not isinstance(value, str):
        raise UserError(f"Поле «{key}» должно быть строкой.")
    return value


def _chat_id(request: web.Request) -> int:
    try:
        return int(request.match_info["chat_id"])
    except ValueError:
        raise web.HTTPNotFound()


def _discover_repos(root: Path, known: set[str]) -> list[dict[str, str]]:
    found = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        if os.path.exists(os.path.join(entry.path, ".git")) and entry.path not in known:
            found.append({"name": entry.name, "path": entry.path})
    return found


def _open_in(target: str, path: str) -> None:
    if sys.platform != "darwin":
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if target == "finder":
        command = ["open", path]
    elif target == "terminal":
        command = ["open", "-a", "Terminal", path]
    elif target == "code" and shutil.which("code"):
        command = ["code", path]
    else:
        raise UserError("Не удалось открыть: нет такого приложения.")
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def create_app(config: DesktopConfig, db: Database, orch: Orchestrator, hub: Hub, token: str, port: int,
               extra_origins: list[str] | None = None) -> web.Application:
    app = web.Application(
        middlewares=[make_security_middleware(token, port, extra_origins or []), errors],
        client_max_size=2 * 1024 * 1024,
    )
    routes = web.RouteTableDef()

    def projects_payload() -> list[dict[str, Any]]:
        return [
            {"id": p.id, "name": p.name, "path": p.path, "default_branch": p.default_branch}
            for p in db.list_projects()
        ]

    @routes.get("/api/bootstrap")
    async def bootstrap(request: web.Request) -> web.Response:
        return web.json_response(
            {"catalog": orch.catalog, "projects": projects_payload(), "chats": orch.list_chats()}
        )

    @routes.post("/api/projects")
    async def add_project(request: web.Request) -> web.Response:
        data = await _body(request)
        project = orch.add_project(_str(data, "name", "") or "", _str(data, "path", "") or "", _str(data, "branch"))
        return web.json_response({"id": project.id, "name": project.name, "path": project.path,
                                  "default_branch": project.default_branch}, status=201)

    @routes.get("/api/discover")
    async def discover(request: web.Request) -> web.Response:
        known = {p.path for p in db.list_projects()}
        repos = await asyncio.to_thread(_discover_repos, config.projects_dir, known)
        return web.json_response({"root": str(config.projects_dir), "repos": repos})

    @routes.post("/api/chats")
    async def create_chat(request: web.Request) -> web.Response:
        data = await _body(request)
        project_id = data.get("project_id")
        if not isinstance(project_id, int):
            raise UserError("Нужен project_id.")
        chat = await orch.create_chat(
            project_id,
            _str(data, "agent", "claude") or "claude",
            _str(data, "model", "") or "",
            _str(data, "effort", "") or "",
            _str(data, "mode", "worktree") or "worktree",
        )
        return web.json_response(chat, status=201)

    @routes.get("/api/chats/{chat_id}")
    async def get_chat(request: web.Request) -> web.Response:
        return web.json_response(orch.get_chat_with_messages(_chat_id(request)))

    @routes.patch("/api/chats/{chat_id}")
    async def patch_chat(request: web.Request) -> web.Response:
        data = await _body(request)
        chat = orch.update_chat(
            _chat_id(request), _str(data, "title"), _str(data, "agent"), _str(data, "model"), _str(data, "effort")
        )
        return web.json_response(chat)

    @routes.delete("/api/chats/{chat_id}")
    async def delete_chat(request: web.Request) -> web.Response:
        await orch.discard(_chat_id(request))
        return web.json_response({"ok": True})

    @routes.post("/api/chats/{chat_id}/send")
    async def send(request: web.Request) -> web.Response:
        data = await _body(request)
        return web.json_response(await orch.send(_chat_id(request), _str(data, "text", "") or ""), status=202)

    @routes.post("/api/chats/{chat_id}/stop")
    async def stop(request: web.Request) -> web.Response:
        return web.json_response({"stopped": await orch.stop(_chat_id(request))})

    @routes.get("/api/chats/{chat_id}/diff")
    async def diff(request: web.Request) -> web.Response:
        return web.json_response(await orch.diff(_chat_id(request)))

    @routes.post("/api/chats/{chat_id}/commit")
    async def commit(request: web.Request) -> web.Response:
        data = await _body(request)
        await orch.commit(_chat_id(request), _str(data, "message", "") or "")
        return web.json_response({"ok": True})

    @routes.post("/api/chats/{chat_id}/merge")
    async def merge(request: web.Request) -> web.Response:
        data = await _body(request)
        target = await orch.merge(_chat_id(request), _str(data, "message", "") or "")
        return web.json_response({"ok": True, "into": target})

    @routes.post("/api/chats/{chat_id}/dirs")
    async def add_dir(request: web.Request) -> web.Response:
        data = await _body(request)
        return web.json_response(orch.add_extra_dir(_chat_id(request), _str(data, "path", "") or ""))

    @routes.post("/api/chats/{chat_id}/open")
    async def open_chat_dir(request: web.Request) -> web.Response:
        data = await _body(request)
        chat = orch.get_chat_with_messages(_chat_id(request))["chat"]
        _open_in(_str(data, "target", "finder") or "finder", chat["worktree_path"])
        return web.json_response({"ok": True})

    @routes.get("/ws")
    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        queue = hub.subscribe()

        async def writer() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                await ws.send_str(json.dumps(item, ensure_ascii=False))

        async def reader() -> None:
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    return

        tasks = [asyncio.create_task(writer()), asyncio.create_task(reader())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            hub.unsubscribe(queue)
            await ws.close()
        return ws

    @routes.get("/")
    async def index(request: web.Request) -> web.Response:
        page = WEB_DIR / "index.html"
        headers = {"Cache-Control": "no-store", "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff"}
        if not page.exists():
            return web.Response(text=NO_UI_PAGE, content_type="text/html", headers=headers)
        return web.Response(body=page.read_bytes(), content_type="text/html", headers=headers)

    app.add_routes(routes)
    if (WEB_DIR / "assets").is_dir():
        app.router.add_static("/assets", WEB_DIR / "assets", append_version=False)
    return app
