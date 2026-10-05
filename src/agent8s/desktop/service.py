"""Run agent8s-desktop in the background from login (macOS LaunchAgent).

Why the service lives outside the repository: a process started by launchd
cannot read ~/Documents (and Desktop/Downloads) without a TCC permission
prompt nobody can answer, so it hangs while Python merely reads its own
venv. The service therefore gets its own copy of the code in a private
virtualenv, and its database and worktrees, under
~/Library/Application Support/agent8s. The window you open from the terminal
or the Dock is then only a client of that already-running service.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

LABEL = "com.agent8s.desktop"
PORT = 8731
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "agent8s"
DATA_DIR = APP_SUPPORT / "data"
RUNTIME_DIR = APP_SUPPORT / "runtime"
LOG_DIR = APP_SUPPORT / "logs"
# Settings worth carrying from the interactive environment (.env) into the service,
# which cannot read the repository's .env.
CARRIED_ENV = (
    "AGENT8S_RELAY_URL", "AGENT8S_PREVIEW_ORIGIN", "AGENT8S_PROJECTS_DIR", "AGENT8S_CLAUDE_ALLOWED_TOOLS",
    "AGENT8S_CLAUDE_PERMISSION_MODE", "AGENT8S_CODEX_SANDBOX", "AGENT8S_DESKTOP_TURN_TIMEOUT",
)
DEFAULT_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


class ServiceError(RuntimeError):
    pass


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def build_plist(executable: Path, env: dict[str, str]) -> bytes:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [str(executable), "--no-window", "--port", str(PORT)],
            "WorkingDirectory": str(APP_SUPPORT),
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            # Restart after a crash, but not after a clean stop.
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            # Needs the user's GUI session: that is where macOS can show permission prompts.
            "LimitLoadToSessionType": "Aqua",
            "ProcessType": "Interactive",
            "StandardOutPath": str(LOG_DIR / "service.out.log"),
            "StandardErrorPath": str(LOG_DIR / "service.err.log"),
        }
    )


def service_environment(environ: dict[str, str]) -> dict[str, str]:
    # The interactive PATH knows where claude, codex, git, node, rtk, ... live;
    # launchd's own PATH knows none of them.
    path = [p for p in environ.get("PATH", "").split(":") if p]
    for extra in (str(Path.home() / ".local" / "bin"), *DEFAULT_PATH):
        if extra not in path:
            path.append(extra)
    env = {
        "PATH": ":".join(path),
        "LANG": environ.get("LANG", "en_US.UTF-8"),
        "AGENT8S_DATA_DIR": str(DATA_DIR),
        "AGENT8S_WORKTREE_DIR": str(APP_SUPPORT / "worktrees"),
    }
    env.update({k: environ[k] for k in CARRIED_ENV if environ.get(k)})
    return env


def running_service_url() -> Optional[str]:
    """URL (with token) of a live service, or None."""
    try:
        info = json.loads((DATA_DIR / "desktop.json").read_text())
        os.kill(info["pid"], 0)
        urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/", timeout=2).close()
        return f"http://127.0.0.1:{info['port']}/#token={info['token']}"
    except (OSError, ValueError, KeyError):
        return None


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def status() -> str:
    if not plist_path().exists():
        return "автозапуск не установлен"
    loaded = _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0
    alive = running_service_url() is not None
    if loaded and alive:
        return f"автозапуск включён, сервис работает (порт {PORT})"
    if loaded:
        return f"загружен в launchd, но не отвечает — смотри {LOG_DIR}/service.err.log"
    return "plist есть, но сервис не загружен в launchd"


def _repo_root() -> Path:
    root = Path(__file__).resolve().parents[3]
    if not (root / "pyproject.toml").exists():
        raise ServiceError("Установку сервиса нужно запускать из клона репозитория (uv run agent8s-desktop --install-service).")
    return root


def _install_runtime(repo: Path) -> Path:
    uv = shutil.which("uv")
    if not uv:
        raise ServiceError("Не найден uv.")
    if not (repo / "src" / "agent8s" / "desktop" / "web" / "index.html").exists():
        raise ServiceError("Интерфейс не собран: (cd desktop-ui && npm install && npm run build)")
    python = RUNTIME_DIR / "bin" / "python"
    if not python.exists():
        subprocess.run([uv, "venv", "--python", sys.executable, str(RUNTIME_DIR)], check=True, capture_output=True)
    # A non-editable copy: the service must not depend on files inside the repository.
    result = subprocess.run(
        [uv, "pip", "install", "--python", str(python), "--reinstall-package", "agent8s",
         "--refresh-package", "agent8s", f"{repo}[desktop]"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ServiceError(f"Не удалось установить окружение сервиса:\n{result.stderr[-800:]}")
    return RUNTIME_DIR / "bin" / "agent8s-desktop"


def _migrate_database(repo: Path) -> Optional[Path]:
    """First install: carry the projects/chats you already have over to the service's database."""
    source = repo / "data" / "agent8s.sqlite3"
    target = DATA_DIR / "agent8s.sqlite3"
    if target.exists() or not source.exists():
        return None
    # sqlite's backup API gives a consistent copy even while the DB is in use (WAL).
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    return source


def install(environ: Optional[dict[str, str]] = None) -> list[str]:
    if sys.platform != "darwin":
        raise ServiceError("Автозапуск поддерживается только на macOS.")
    repo = _repo_root()
    environ = dict(os.environ if environ is None else environ)
    notes: list[str] = []

    for directory in (DATA_DIR, LOG_DIR, APP_SUPPORT / "worktrees"):
        directory.mkdir(parents=True, exist_ok=True)
    executable = _install_runtime(repo)
    migrated = _migrate_database(repo)
    if migrated:
        notes.append(f"Проекты и чаты скопированы из {migrated} в {DATA_DIR} (дальше сервис работает со своей базой).")

    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_plist(executable, service_environment(environ)))

    _unload()
    result = _launchctl("bootstrap", _domain(), str(path))
    for _ in range(5):  # launchd may still be tearing the old job down
        if result.returncode == 0:
            break
        time.sleep(1)
        result = _launchctl("bootstrap", _domain(), str(path))
    if result.returncode != 0:
        raise ServiceError(f"launchctl bootstrap не сработал: {result.stderr.strip()}")

    for _ in range(40):
        if running_service_url():
            notes.append(f"Сервис запущен и будет стартовать при входе в систему. Логи: {LOG_DIR}")
            return notes
        time.sleep(0.5)
    err = LOG_DIR / "service.err.log"
    tail = err.read_text()[-600:] if err.exists() else ""
    raise ServiceError(f"Сервис не ответил за 20 секунд. {tail}")


def _unload() -> None:
    """`launchctl bootout` returns before the job is actually gone; wait until it is."""
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    for _ in range(40):
        if _launchctl("print", f"{_domain()}/{LABEL}").returncode != 0:
            return
        time.sleep(0.25)


def uninstall() -> None:
    _unload()
    plist_path().unlink(missing_ok=True)


def cli(do_install: bool, do_uninstall: bool) -> int:
    try:
        if do_install:
            for line in install():
                print(line)
        elif do_uninstall:
            uninstall()
            print("Автозапуск удалён. Данные в", APP_SUPPORT, "сохранены.")
        print(status())
        return 0
    except ServiceError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
