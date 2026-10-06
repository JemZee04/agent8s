"""Import existing Claude Code sessions (~/.claude/projects/*/*.jsonl) as chats.

A session file is a log of events, not a chat: assistant replies arrive as one
entry per content block, tool results come back as `user` entries, and there
is bookkeeping (titles, snapshots, attachments) in between. This module folds
it into the app's model: one user message per prompt, and one assistant message
per turn holding text and tool parts.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from .drivers import _stringify, clip

CLAUDE_ROOT = Path.home() / ".claude" / "projects"
STOP_REASONS = {"end_turn", "stop_sequence", "max_tokens"}
# A turn without a final stop_reason is "still running" only if the file is being written right now;
# an old session that was simply interrupted must not lose its last turn.
ACTIVE_WINDOW_SECONDS = 120
HEAD_BYTES = 128 * 1024
TAIL_BYTES = 512 * 1024
# Imported history is context, not a live log: keep tool output short so a huge
# session stays pleasant to open (the files on disk are the source of truth).
IMPORT_OUTPUT_LIMIT = 4000
_WRAPPER_TAGS = ("system-reminder", "local-command-caveat", "local-command-stdout", "ide_opened_file", "ide_selection")
_WRAPPER_RE = re.compile(r"<(%s)\b[^>]*>.*?</\1>" % "|".join(_WRAPPER_TAGS), re.S)
_COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>(?:.*?<command-args>(.*?)</command-args>)?", re.S)


@dataclass
class SessionInfo:
    id: str
    path: Path
    title: str
    cwd: str
    branch: str
    mtime: float
    size: int
    first_prompt: str


@dataclass
class ParsedMessage:
    role: str
    parts: list[dict[str, Any]]
    created_at: Optional[str] = None
    model: Optional[str] = None
    status: str = "done"
    offset: int = 0  # where in the file this message begins


@dataclass
class ParsedSession:
    title: str
    cwd: str
    branch: str
    messages: list[ParsedMessage] = field(default_factory=list)
    first_at: Optional[str] = None
    last_at: Optional[str] = None
    # Everything before this byte offset is accounted for; the next read starts here. It stops short of
    # a turn that is still being written, so that turn is imported whole once it is finished.
    end_offset: int = 0
    pending: bool = False  # the session is in the middle of a turn
    new_title: Optional[str] = None  # a title found in the part just read


def encode_cwd(cwd: str) -> str:
    """How Claude Code names the project directory for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _events(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def _lines_from(path: Path, offset: int) -> Iterator[tuple[int, int, Optional[dict[str, Any]]]]:
    """(start, end, event) for each *complete* line from `offset`. A line still being written
    (no newline yet) is not yielded, so offsets always sit on line boundaries."""
    with path.open("rb") as handle:
        handle.seek(offset)
        position = offset
        for raw in handle:
            if not raw.endswith(b"\n"):
                return
            start, position = position, position + len(raw)
            try:
                event = json.loads(raw)
            except ValueError:
                event = None
            yield start, position, event if isinstance(event, dict) else None


def find_session_file(session_id: str, root: Optional[Path] = None) -> Optional[Path]:
    for candidate in (root or CLAUDE_ROOT).glob(f"*/{session_id}.jsonl"):
        return candidate
    return None


def end_of_complete_lines(path: Path) -> int:
    """Size of the file up to its last newline (what a finished turn leaves behind)."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - 65536))
        tail = handle.read()
    cut = tail.rfind(b"\n")
    return size - (len(tail) - cut - 1) if cut != -1 else 0


def legacy_offset(path: Path, imported_count: int) -> int:
    """For chats imported before offsets were recorded: where the imported part of the file ends.

    The chat holds the first `imported_count` messages the file yields; the next one starts at the answer.
    (Positions, not timestamps: a long answer starts at one moment and ends much later.)"""
    parsed = parse_session(path)
    if len(parsed.messages) > imported_count:
        return parsed.messages[imported_count].offset
    return end_of_complete_lines(path) if len(parsed.messages) < imported_count else parsed.end_offset


def clean_prompt(text: str) -> str:
    """Strip the wrappers Claude Code injects around what the person actually typed."""
    command = _COMMAND_RE.search(text) if "<command-name>" in text else None
    if command:
        return f"{command.group(1).strip()} {(command.group(2) or '').strip()}".strip()
    return _WRAPPER_RE.sub("", text).strip()


def _prompt_text(event: dict[str, Any]) -> Optional[str]:
    """The typed prompt of a `user` event, or None if it is only tool results / bookkeeping."""
    if event.get("isMeta") or event.get("isCompactSummary") or event.get("isSidechain"):
        return None
    content = event.get("message", {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        if not text and any(isinstance(b, dict) and b.get("type") == "image" for b in content):
            text = "[изображение]"
    else:
        return None
    text = clean_prompt(text)
    return text or None


def _launch_cwd(project_dir: str, cwds: list[str]) -> str:
    """The directory the session was started in. `claude --resume` only finds
    the session from there, and that directory is the one the folder name encodes."""
    for cwd in cwds:
        if encode_cwd(cwd) == project_dir:
            return cwd
    return cwds[0] if cwds else ""


def list_sessions(root: Optional[Path] = None) -> list[SessionInfo]:
    """Cheap metadata for every session: reads only the start and end of each file."""
    root = root or CLAUDE_ROOT
    sessions = []
    if not root.is_dir():
        return sessions
    for path in root.glob("*/*.jsonl"):
        try:
            stat = path.stat()
            head, tail = _read_ends(path, stat.st_size)
        except OSError:
            continue
        cwds, first_prompt, branch = [], "", ""
        for event in _lines(head):
            if event.get("cwd") and event["cwd"] not in cwds:
                cwds.append(event["cwd"])
            branch = branch or event.get("gitBranch", "")
            if not first_prompt and event.get("type") == "user":
                first_prompt = _prompt_text(event) or ""
        title = ""
        for event in _lines(tail):
            if event.get("type") == "custom-title" and event.get("customTitle"):
                title = event["customTitle"]
            elif event.get("type") == "ai-title" and event.get("aiTitle") and not title:
                title = event["aiTitle"]
        sessions.append(
            SessionInfo(
                id=path.stem, path=path, title=title or first_prompt[:80] or path.stem,
                cwd=_launch_cwd(path.parent.name, cwds), branch=branch,
                mtime=stat.st_mtime, size=stat.st_size, first_prompt=first_prompt[:200],
            )
        )
    return sorted(sessions, key=lambda s: s.mtime, reverse=True)


def _read_ends(path: Path, size: int) -> tuple[bytes, bytes]:
    with path.open("rb") as f:
        if size <= HEAD_BYTES + TAIL_BYTES:
            whole = f.read()  # small file: head and tail are the same, complete text
            return whole, whole
        head = f.read(HEAD_BYTES)
        f.seek(size - TAIL_BYTES)
        return head, f.read()


def _lines(blob: bytes) -> Iterator[dict[str, Any]]:
    # A slice of a file starts/ends mid-line; the broken lines simply fail to parse.
    for line in blob.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def parse_session(path: Path, start_offset: int = 0) -> ParsedSession:
    """Fold the events of a session file (from `start_offset`) into chat messages."""
    cwds: list[str] = []
    branch = ""
    custom_title = ai_title = ""
    messages: list[ParsedMessage] = []
    turn: Optional[ParsedMessage] = None
    tools: dict[str, dict[str, Any]] = {}
    first_at = last_at = None
    end_offset = start_offset
    turn_start: Optional[int] = None  # offset of the line that began the latest turn
    turn_index = 0  # messages[turn_index:] belong to that turn
    finished = True  # did the latest turn reach its final answer?

    for line_start, line_end, event in _lines_from(path, start_offset):
        end_offset = line_end
        if event is None:
            continue
        kind = event.get("type")
        if kind == "custom-title":
            custom_title = event.get("customTitle") or custom_title
        elif kind == "ai-title":
            ai_title = event.get("aiTitle") or ai_title
        if kind not in ("user", "assistant") or event.get("isSidechain"):
            continue
        stamp = event.get("timestamp")
        first_at, last_at = first_at or stamp, stamp or last_at
        if event.get("cwd") and event["cwd"] not in cwds:
            cwds.append(event["cwd"])
        branch = event.get("gitBranch") or branch

        if kind == "user":
            if event.get("isCompactSummary"):
                messages.append(ParsedMessage("system", [{"type": "text", "text": "Контекст сессии был сжат (compact)."}], stamp, offset=line_start))
                turn = None
                continue
            prompt = _prompt_text(event)
            if prompt is not None:
                turn_start, turn_index, finished = line_start, len(messages), False
                messages.append(ParsedMessage("user", [{"type": "text", "text": prompt}], stamp, offset=line_start))
                turn = None
                continue
            content = event.get("message", {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    part = tools.get(block.get("tool_use_id", ""))
                    if part is not None:
                        part["output"] = clip(_stringify(block.get("content")), IMPORT_OUTPUT_LIMIT)
                        part["is_error"] = bool(block.get("is_error"))
            continue

        message = event.get("message", {})
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if turn is None:
            turn = ParsedMessage("assistant", [], stamp, message.get("model") or None, offset=line_start)
            messages.append(turn)
        if message.get("model"):
            turn.model = message["model"]
        if event.get("isAbortedMidStream"):
            turn.status = "interrupted"
        finished = bool(event.get("isAbortedMidStream")) or message.get("stop_reason") in STOP_REASONS
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                if turn.parts and turn.parts[-1]["type"] == "text":
                    turn.parts[-1]["text"] += "\n\n" + block["text"]
                else:
                    turn.parts.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "tool_use" and block.get("id"):
                part = {
                    "type": "tool", "id": block["id"], "name": block.get("name", "tool"),
                    "input": block.get("input", {}), "output": None, "is_error": False, "status": "done",
                }
                tools[block["id"]] = part
                turn.parts.append(part)
            # `thinking` blocks are stored without their text: nothing to show.

    pending = False
    if not finished and turn_start is not None and time.time() - path.stat().st_mtime < ACTIVE_WINDOW_SECONDS:
        # The turn is still being written: leave it for the next read so it arrives whole.
        del messages[turn_index:]
        end_offset, pending = turn_start, True

    messages = [m for m in messages if m.role != "assistant" or m.parts]
    cwd = _launch_cwd(path.parent.name, cwds)
    first_user = next((m.parts[0]["text"] for m in messages if m.role == "user"), "")
    found_title = custom_title or ai_title or None
    return ParsedSession(
        title=(found_title or " ".join(first_user.split())[:60] or path.stem)[:120],
        cwd=cwd, branch=branch, messages=messages, first_at=first_at, last_at=last_at,
        end_offset=end_offset, pending=pending, new_title=found_title,
    )
