"""Is each agent signed in *as the background service sees it*?

The service starts claude/codex without your shell's environment, so it relies on the login those CLIs keep
themselves (claude: the macOS keychain). That login is separate from the desktop Claude app you chat in, and
it can expire on its own. Without a check the symptom is an empty reply with a cryptic error.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

AUTH_COMMANDS = {"claude": ["claude", "auth", "status"], "codex": ["codex", "login", "status"]}
LOGIN_COMMANDS = {"claude": ["claude", "auth", "login"], "codex": ["codex", "login"]}
LABELS = {"claude": "Claude Code", "codex": "Codex"}
CACHE_SECONDS = 20.0

_AUTH_ERRORS = {
    "claude": re.compile(
        r"failed to authenticate|oauth (session|token)[^.]*expired|not logged in|please run /login|invalid api key|"
        r"authentication_error|could not be refreshed", re.I),
    "codex": re.compile(r"not logged in|please (log ?in|sign in)|run `?codex login|authentication required", re.I),
}
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def login_command(agent: str) -> str:
    return " ".join(LOGIN_COMMANDS[agent])


def explain_error(agent: str, error: Optional[str]) -> Optional[str]:
    """Append what to do when the failure is a sign-in problem; other errors pass through unchanged."""
    if not error or agent not in _AUTH_ERRORS or not _AUTH_ERRORS[agent].search(error):
        return error
    return (
        f"{error}\n\n{LABELS[agent]} не авторизован для фонового сервиса agent8s (его вход хранится отдельно от "
        f"приложения Claude и мог истечь). Войдите заново — один раз: нажмите «Войти» над полем ввода или выполните "
        f"в терминале `{login_command(agent)}`, затем повторите сообщение."
    )


async def check_auth(agent: str, refresh: bool = False) -> dict[str, Any]:
    """{"ok": True|False|None (unknown), "login": command, "label": name}"""
    cached = _cache.get(agent)
    if cached and not refresh and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    result: dict[str, Any] = {"ok": None, "login": login_command(agent), "label": LABELS[agent]}
    command = AUTH_COMMANDS[agent]
    if shutil.which(command[0]):
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            try:
                output, _ = await asyncio.wait_for(proc.communicate(), 15)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                output = b""
            else:
                result["ok"] = _parse(agent, proc.returncode, output.decode(errors="replace"))
        except OSError:
            pass
    _cache[agent] = (time.monotonic(), result)
    return result


def _parse(agent: str, returncode: Optional[int], text: str) -> Optional[bool]:
    if agent == "claude":
        try:
            return bool(json.loads(text).get("loggedIn"))
        except ValueError:
            return None
    lowered = text.lower()
    if "not logged in" in lowered:
        return False
    return True if returncode == 0 and "logged in" in lowered else (False if returncode else None)


def forget_cached(agent: Optional[str] = None) -> None:
    for name in [agent] if agent else list(_cache):
        _cache.pop(name, None)


def login_script(agent: str) -> str:
    executable = shutil.which(LOGIN_COMMANDS[agent][0]) or LOGIN_COMMANDS[agent][0]
    command = " ".join([f'"{executable}"', *LOGIN_COMMANDS[agent][1:]])
    return (
        "#!/bin/bash\n"
        f'echo "Вход в {LABELS[agent]} для agent8s. Откроется браузер: подтвердите вход."\n'
        "echo\n"
        f"{command}\n"
        'echo\necho "Готово. Вернитесь в agent8s и нажмите «Проверить снова». Это окно можно закрыть."\n'
        "read -n 1 -s -r -p 'Нажмите любую клавишу…'\n"
    )


def open_login_terminal(agent: str, folder: Path, opener: Optional[Callable[[list[str]], Any]] = None) -> Path:
    """Open Terminal.app running the agent's login: it is an interactive browser flow only you can complete."""
    if sys.platform != "darwin":
        raise RuntimeError("Кнопка работает только на macOS: выполните команду входа в терминале.")
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / f"login-{agent}.command"
    script.write_text(login_script(agent))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (opener or (lambda args: subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))(
        ["open", "-a", "Terminal", str(script)]
    )
    return script
