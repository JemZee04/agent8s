from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from aiohttp import web

from ..config import load_desktop_config
from ..db import Database
from ..singleton import AlreadyRunningError, acquire_singleton_lock
from .runner import Hub, Orchestrator
from .server import create_app
from .store import Store

log = logging.getLogger("agent8s.desktop")
REPO_ROOT = Path(__file__).resolve().parents[3]


class ServerThread(threading.Thread):
    def __init__(self, orch: Orchestrator, app: web.Application, sock: socket.socket):
        super().__init__(daemon=True, name="agent8s-server")
        self._orch = orch
        self._app = app
        self._sock = sock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self.ready = threading.Event()

    def run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        await web.SockSite(runner, self._sock).start()
        self.ready.set()
        await self._stop_event.wait()
        await self._orch.shutdown()  # kills running agents and their process groups
        await runner.cleanup()

    def stop(self) -> None:
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
            self.join(timeout=15)


class WindowApi:
    """Exposed to the page as window.pywebview.api — native things a browser can't do."""

    def pick_folder(self) -> str | None:
        import webview

        window = webview.windows[0]
        result = window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

    def open_url(self, url: str) -> None:
        # A link inside the app window must open in the real browser, not
        # replace the app. Only plain web links; anything else is ignored.
        if url.startswith(("http://", "https://", "mailto:")):
            webbrowser.open(url)


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    return sock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent8s-desktop", description="Десктоп-клиент agent8s")
    parser.add_argument("--port", type=int, default=0, help="порт (по умолчанию случайный свободный)")
    parser.add_argument("--no-window", action="store_true", help="только сервер: напечатать адрес, окно не открывать")
    parser.add_argument("--token", default=None, help="фиксированный токен (для dev-сервера Vite)")
    parser.add_argument("--allow-origin", action="append", default=[], help="доп. Origin (dev-сервер Vite)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # Paths like AGENT8S_DATA_DIR=./data are relative; resolve them against the
    # repository, not against whatever directory the app was launched from.
    if (REPO_ROOT / "pyproject.toml").exists():
        os.chdir(REPO_ROOT)

    config = load_desktop_config()
    try:
        acquire_singleton_lock(
            config.data_dir, name="desktop", label="agent8s-desktop",
            reason="two instances would fight over running chats.",
        )
    except AlreadyRunningError:
        return 1

    db = Database(config.db_path)
    store = Store(config.db_path)
    interrupted = store.reconcile_interrupted()
    if interrupted:
        log.info("marked %d chat(s) as idle: their turns did not survive the previous shutdown", interrupted)

    hub = Hub()
    orch = Orchestrator(config, db, store, hub)
    sock = _bind(args.port)
    port = sock.getsockname()[1]
    token = args.token or secrets.token_urlsafe(32)
    app = create_app(config, db, orch, hub, token, port, args.allow_origin)

    server = ServerThread(orch, app, sock)
    server.start()
    server.ready.wait(10)
    # The token rides in the URL fragment: it is never sent to the server nor logged.
    url = f"http://127.0.0.1:{port}/#token={token}"

    try:
        if args.no_window:
            print(f"agent8s-desktop: {url}", flush=True)
            threading.Event().wait()  # until Ctrl+C
        else:
            _open_window(url)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def _open_window(url: str) -> None:
    try:
        import webview
    except ImportError:
        print("pywebview не установлен (uv sync --extra desktop) — открываю в браузере.", file=sys.stderr)
        webbrowser.open(url)
        threading.Event().wait()
        return
    webview.create_window(
        "agent8s", url, width=1320, height=860, min_size=(900, 560), js_api=WindowApi(), text_select=True
    )
    webview.start()


def run() -> None:
    sys.exit(main())
