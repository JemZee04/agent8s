from __future__ import annotations

import argparse
import asyncio
import logging
import os
import json
import secrets
import signal
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from aiohttp import web

from ..config import load_desktop_config
from ..db import Database
from ..singleton import AlreadyRunningError, acquire_singleton_lock
from . import macapp, service
from .remote import RemoteManager
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
    parser.add_argument("--no-window", action="store_true", help="только сервер, без окна (так работает фоновый сервис)")
    parser.add_argument("--standalone", action="store_true", help="не подключаться к уже запущенному сервису, поднять свой сервер")
    parser.add_argument("--install-service", action="store_true", help="установить автозапуск при входе в систему (macOS)")
    parser.add_argument("--uninstall-service", action="store_true", help="убрать автозапуск")
    parser.add_argument("--service-status", action="store_true", help="показать состояние автозапуска")
    parser.add_argument("--install-app", action="store_true", help="установить приложение agent8s.app в ~/Applications (и автозапуск)")
    parser.add_argument("--uninstall-app", action="store_true", help="удалить приложение agent8s.app")
    parser.add_argument("--app", action="store_true", help="режим приложения: окно на фоновом сервисе (его запускает agent8s.app)")
    parser.add_argument("--token", default=None, help="фиксированный токен (для dev-сервера Vite)")
    parser.add_argument("--allow-origin", action="append", default=[], help="доп. Origin (dev-сервер Vite)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.install_app or args.uninstall_app:
        return macapp.cli(do_install=args.install_app, do_uninstall=args.uninstall_app)
    if args.app:
        return _run_app()
    if args.install_service or args.uninstall_service or args.service_status:
        return service.cli(do_install=args.install_service, do_uninstall=args.uninstall_service)

    # With the background service running, the window is just a client of it.
    if not args.no_window and not args.standalone:
        running = service.running_service_url()
        if running:
            print("agent8s-desktop: подключаюсь к работающему сервису", flush=True)
            _open_window(running)
            return 0

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
    remote = RemoteManager(
        config.data_dir, hub, port, token,
        os.environ.get("AGENT8S_RELAY_URL", "").strip() or None,
        os.environ.get("AGENT8S_PREVIEW_ORIGIN", "").strip() or None,
    )
    app = create_app(config, db, orch, hub, token, port, args.allow_origin, remote)

    server = ServerThread(orch, app, sock)
    server.start()
    server.ready.wait(10)
    # The token rides in the URL fragment: it is never sent to the server nor logged.
    url = f"http://127.0.0.1:{port}/#token={token}"
    info_file = config.data_dir / "desktop.json"
    _write_private(info_file, json.dumps({"port": port, "token": token, "pid": os.getpid()}))

    try:
        if args.no_window:
            print(f"agent8s-desktop: {url}", flush=True)
            stop = threading.Event()
            # launchd stops a service with SIGTERM: shut down cleanly so running agents are terminated
            # (they live in their own process groups and would otherwise be orphaned).
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            stop.wait()
        else:
            _open_window(url)
    except KeyboardInterrupt:
        pass
    finally:
        info_file.unlink(missing_ok=True)
        server.stop()
    return 0


def _run_app() -> int:
    """What the .app launcher runs: make sure the background service is up, then show the window."""
    try:
        url = service.ensure_running()
    except service.ServiceError as exc:
        log.error("%s", exc)
        _alert("agent8s не запустился", str(exc))
        return 1
    _open_window(url)
    return 0


def _alert(title: str, text: str) -> None:
    # No terminal in app mode: a failure must be visible somewhere other than a log file.
    if sys.platform == "darwin":
        script = f'display alert {json.dumps(title)} message {json.dumps(text)} as critical'
        subprocess.run(["osascript", "-e", script], capture_output=True)


def _mac_identity() -> None:
    """Show "agent8s" and its icon in the Dock and menu bar instead of "Python" and a rocket."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle

        NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "agent8s"
        icon = NSImage.alloc().initByReferencingFile_(str(macapp.ICON_PNG))
        NSApplication.sharedApplication().setApplicationIconImage_(icon)
    except Exception:  # cosmetic only
        log.debug("could not set the Dock identity", exc_info=True)


def _write_private(path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def _open_window(url: str) -> None:
    try:
        import webview
    except ImportError:
        print("pywebview не установлен (uv sync --extra desktop) — открываю в браузере.", file=sys.stderr)
        webbrowser.open(url)
        threading.Event().wait()
        return
    _mac_identity()
    width, height = 1320, 860
    try:  # a fixed 860 px is taller than the usable area of a 13" laptop: the bottom bar would be cut off
        screen = webview.screens[0]
        width, height = min(width, screen.width - 80), min(height, screen.height - 140)
    except Exception:
        pass
    webview.create_window(
        "agent8s", url, width=width, height=height, min_size=(720, 480), js_api=WindowApi(), text_select=True
    )
    webview.start()


def run() -> None:
    sys.exit(main())
