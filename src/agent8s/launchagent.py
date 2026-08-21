from __future__ import annotations

import subprocess
from pathlib import Path

from .selfrepo import self_repo_path

LABEL = "com.agent8s.bot"


class LaunchAgentError(RuntimeError):
    pass


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _bot_executable(repo_dir: Path) -> Path:
    return repo_dir / ".venv" / "bin" / "agent8s-bot"


def _build_plist(repo_dir: Path, bot_exe: Path) -> str:
    path_env = f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    logs_dir = repo_dir / "data"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{bot_exe}</string>
    </array>
    <key>WorkingDirectory</key><string>{repo_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{path_env}</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>{logs_dir}/launchd-stdout.log</string>
    <key>StandardErrorPath</key><string>{logs_dir}/launchd-stderr.log</string>
</dict>
</plist>
"""


def is_installed() -> bool:
    return _plist_path().exists()


def status() -> str:
    if not is_installed():
        return "не установлен"
    result = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    return "установлен и загружен в launchd" if result.returncode == 0 else "plist есть, но не загружен в launchd"


def install() -> Path:
    repo_dir = self_repo_path()
    bot_exe = _bot_executable(repo_dir)
    if not bot_exe.exists():
        raise LaunchAgentError(f"{bot_exe} не существует — сначала выполни `uv sync`")

    plist_path = _plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_build_plist(repo_dir, bot_exe))

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise LaunchAgentError(f"launchctl load не сработал: {result.stderr.strip()}")
    return plist_path


def uninstall() -> None:
    plist_path = _plist_path()
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
