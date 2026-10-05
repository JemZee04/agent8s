"""Which local TCP ports are listening, and which of them belong to a chat's folder.

The agent starts dev servers itself ("run the site on localhost"); this finds
them so the phone can be pointed at the right one without typing a port.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_ADDRESS = re.compile(r"^(?:\*|\[?([0-9a-fA-F:.]+)\]?):(\d+)$")
LOOPBACK_OR_ANY = {"", "127.0.0.1", "::1", "0.0.0.0", "::"}


@dataclass
class ListeningPort:
    port: int
    pid: int
    command: str
    cwd: str = ""


def parse_lsof_listeners(output: str) -> list[ListeningPort]:
    """Parse `lsof -nP -iTCP -sTCP:LISTEN -Fpcn`: lines p<pid>, c<command>, n<address>, repeated."""
    found: dict[int, ListeningPort] = {}
    pid, command = 0, ""
    for line in output.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else 0
        elif tag == "c":
            command = value
        elif tag == "n" and pid:
            match = _ADDRESS.match(value)
            if match and (match.group(1) or "") in LOOPBACK_OR_ANY:  # reachable from this machine only or from all
                port = int(match.group(2))
                found.setdefault(port, ListeningPort(port, pid, command))
    return sorted(found.values(), key=lambda p: p.port)


def _lsof(*args: str) -> str:
    try:
        # +c 0: full command names (lsof cuts them to 9 characters otherwise)
        result = subprocess.run(["lsof", "-nP", "+c", "0", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout


def process_cwd(pid: int) -> str:
    for line in _lsof("-a", "-p", str(pid), "-d", "cwd", "-Fn").splitlines():
        if line.startswith("n"):
            return line[1:]
    return ""


def listening_ports(exclude: Optional[set[int]] = None) -> list[ListeningPort]:
    ports = [p for p in parse_lsof_listeners(_lsof("-iTCP", "-sTCP:LISTEN", "-Fpcn")) if p.port not in (exclude or set())]
    for p in ports:
        p.cwd = process_cwd(p.pid)
    return ports


# Runtimes and tools dev servers usually run under; anything else listening (system
# helpers, IDEs, other apps) is shown only on request. Whole names, not prefixes:
# "goland" is an IDE, not "go".
DEV_COMMANDS = re.compile(
    r"(node|nodejs|python[\d.]*|bun|deno|ruby|php|java|go|cargo|dotnet|next-server|vite|uvicorn|gunicorn|hugo|jekyll|"
    r"webpack|esbuild|parcel|rails|puma|npm|yarn|pnpm|tsx|astro|http-server|live-server)"
)


def classify(port: ListeningPort, folder: str) -> str:
    """"mine" (runs inside the chat's folder), "dev" (looks like a dev server) or "other"."""
    if belongs_to(port, folder):
        return "mine"
    return "dev" if DEV_COMMANDS.fullmatch(port.command.lower()) else "other"


def belongs_to(port: ListeningPort, folder: str) -> bool:
    if not port.cwd or not folder:
        return False
    cwd, root = Path(port.cwd), Path(folder)
    return cwd == root or root in cwd.parents
