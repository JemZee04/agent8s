"""Streaming drivers for the headless CLIs.

Each CLI emits its own JSON dialect. A *normalizer* turns one raw event into
zero or more events of a small common vocabulary, and a *driver* runs the
process and yields those. Everything above this layer (storage, UI) only
knows the common vocabulary:

  {"type": "session",    "id": str}
  {"type": "text",       "text": str}                      # delta
  {"type": "thinking",   "text": str}                      # delta
  {"type": "tool_start", "id": str, "name": str}
  {"type": "tool_input", "id": str, "input": Any}
  {"type": "tool_end",   "id": str, "output": str, "is_error": bool}
  {"type": "usage",      ...numbers}
  {"type": "done",       "ok": bool, "error": str | None}  # always last
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..agents.base import RESPONSE_LANGUAGE_INSTRUCTION

# A single stream-json line can embed a whole file (tool_result), so asyncio's
# 64KiB default line limit is far too small.
STREAM_LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 20_000
STDERR_TAIL = 8_000

Event = dict[str, Any]


def clip(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [обрезано, ещё {len(text) - limit} символов]"


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                chunks.append(f"[{block.get('type', 'block')}]")
            else:
                chunks.append(str(block))
        return "\n".join(chunks)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


# -- claude ---------------------------------------------------------------


class ClaudeNormalizer:
    def __init__(self) -> None:
        self._streamed: set[tuple[str, str]] = set()
        self._started_tools: set[str] = set()
        self._message_id = ""
        self._session_sent = False

    def feed(self, e: dict[str, Any]) -> list[Event]:
        # Sub-agent traffic is nested under a parent tool call; showing it
        # inline would interleave two conversations in one timeline.
        if e.get("parent_tool_use_id"):
            return []
        kind = e.get("type")
        if kind == "system":
            return self._session(e) if e.get("subtype") == "init" else []
        if kind == "stream_event":
            return self._stream(e.get("event", {}))
        if kind == "assistant":
            return self._assistant(e.get("message", {}))
        if kind == "user":
            return self._user(e.get("message", {}))
        if kind == "result":
            return self._result(e)
        return []

    def _session(self, e: dict[str, Any]) -> list[Event]:
        sid = e.get("session_id")
        if not sid or self._session_sent:
            return []
        self._session_sent = True
        return [{"type": "session", "id": sid}]

    def _stream(self, ev: dict[str, Any]) -> list[Event]:
        kind = ev.get("type")
        if kind == "message_start":
            self._message_id = ev.get("message", {}).get("id", "")
            return []
        if kind == "content_block_start":
            block = ev.get("content_block", {})
            if block.get("type") == "tool_use" and block.get("id"):
                self._started_tools.add(block["id"])
                return [{"type": "tool_start", "id": block["id"], "name": block.get("name", "tool")}]
            return []
        if kind == "content_block_delta":
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta" and delta.get("text"):
                self._streamed.add((self._message_id, "text"))
                return [{"type": "text", "text": delta["text"]}]
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                self._streamed.add((self._message_id, "thinking"))
                return [{"type": "thinking", "text": delta["thinking"]}]
        return []

    def _assistant(self, message: dict[str, Any]) -> list[Event]:
        message_id = message.get("id", "")
        out: list[Event] = []
        for block in message.get("content", []):
            kind = block.get("type")
            if kind == "tool_use" and block.get("id"):
                if block["id"] not in self._started_tools:
                    self._started_tools.add(block["id"])
                    out.append({"type": "tool_start", "id": block["id"], "name": block.get("name", "tool")})
                out.append({"type": "tool_input", "id": block["id"], "input": block.get("input", {})})
            # Complete text/thinking blocks only matter when no partial
            # deltas were streamed for this message (otherwise it's a dupe).
            elif kind == "text" and block.get("text") and (message_id, "text") not in self._streamed:
                out.append({"type": "text", "text": block["text"]})
            elif kind == "thinking" and block.get("thinking") and (message_id, "thinking") not in self._streamed:
                out.append({"type": "thinking", "text": block["thinking"]})
        return out

    def _user(self, message: dict[str, Any]) -> list[Event]:
        content = message.get("content")
        if not isinstance(content, list):
            return []
        return [
            {
                "type": "tool_end",
                "id": block["tool_use_id"],
                "output": clip(_stringify(block.get("content"))),
                "is_error": bool(block.get("is_error")),
            }
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")
        ]

    def _result(self, e: dict[str, Any]) -> list[Event]:
        out = self._session(e)
        usage = e.get("usage") or {}
        out.append(
            {
                "type": "usage",
                "cost_usd": e.get("total_cost_usd"),
                "duration_ms": e.get("duration_ms"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            }
        )
        ok = not e.get("is_error") and e.get("subtype", "success") == "success"
        error = None if ok else str(e.get("result") or e.get("subtype") or "claude завершился с ошибкой")
        out.append({"type": "done", "ok": ok, "error": error})
        return out


# -- codex ----------------------------------------------------------------

_SHELL_PREFIXES = ("/bin/zsh -lc ", "/bin/bash -lc ", "/bin/sh -lc ", "zsh -lc ", "bash -lc ")


def unwrap_shell(command: str) -> str:
    """codex reports `/bin/zsh -lc "<cmd>"`; show just <cmd>."""
    for prefix in _SHELL_PREFIXES:
        if command.startswith(prefix):
            try:
                parts = shlex.split(command[len(prefix):])
            except ValueError:
                return command
            if len(parts) == 1:
                return parts[0]
    return command


class CodexNormalizer:
    def __init__(self) -> None:
        self.error: Optional[str] = None
        self._started: set[str] = set()
        self._last_was_text = False

    def feed(self, e: dict[str, Any]) -> list[Event]:
        kind = e.get("type")
        if kind == "thread.started" and e.get("thread_id"):
            return [{"type": "session", "id": e["thread_id"]}]
        if kind == "turn.completed":
            usage = e.get("usage") or {}
            return [
                {
                    "type": "usage",
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                }
            ]
        if kind == "turn.failed":
            self.error = str((e.get("error") or {}).get("message") or "turn failed")
            return []
        if kind == "error":
            self.error = str(e.get("message") or "error")
            return []
        if kind in ("item.started", "item.completed"):
            return self._item(kind == "item.completed", e.get("item") or {})
        return []

    def _item(self, completed: bool, item: dict[str, Any]) -> list[Event]:
        item_id = str(item.get("id", ""))
        item_type = item.get("type", "")
        if item_type == "agent_message":
            if not completed or not item.get("text"):
                return []
            text = str(item["text"])
            if self._last_was_text:
                text = "\n\n" + text
            self._last_was_text = True
            return [{"type": "text", "text": text}]
        if item_type == "reasoning":
            return [{"type": "thinking", "text": str(item["text"])}] if completed and item.get("text") else []

        self._last_was_text = False
        out: list[Event] = []
        if item_id not in self._started:
            self._started.add(item_id)
            out.append({"type": "tool_start", "id": item_id, "name": _codex_tool_name(item_type)})
            out.append({"type": "tool_input", "id": item_id, "input": _codex_tool_input(item_type, item)})
        if completed:
            output = str(item.get("aggregated_output") or "")
            exit_code = item.get("exit_code")
            is_error = item.get("status") == "failed" or (isinstance(exit_code, int) and exit_code != 0)
            out.append({"type": "tool_end", "id": item_id, "output": clip(output), "is_error": is_error})
        return out


def _codex_tool_name(item_type: str) -> str:
    return {"command_execution": "shell", "file_change": "patch", "todo_list": "plan"}.get(item_type, item_type)


def _codex_tool_input(item_type: str, item: dict[str, Any]) -> Any:
    if item_type == "command_execution":
        return {"command": unwrap_shell(str(item.get("command", "")))}
    if item_type == "file_change":
        return {"changes": [f"{c.get('kind', '?')} {c.get('path', '?')}" for c in item.get("changes", [])]}
    if item_type == "todo_list":
        return {
            "items": [
                f"[{'x' if i.get('completed') else ' '}] {i.get('text', '')}"
                for i in item.get("items", [])
                if isinstance(i, dict)
            ]
        }
    return {k: v for k, v in item.items() if k not in ("id", "type", "status")}


# -- process plumbing -----------------------------------------------------


@dataclass
class TurnRequest:
    prompt: str
    cwd: Path
    model: str = ""
    effort: str = ""
    session_id: Optional[str] = None
    extra_dirs: list[str] = field(default_factory=list)


async def _drain(stream: asyncio.StreamReader, tail: deque[str]) -> None:
    # Must be consumed concurrently: a chatty stderr would otherwise fill the
    # pipe and block the child while we're still waiting on its stdout.
    total = 0
    async for raw in stream:
        line = raw.decode(errors="replace")[-STDERR_TAIL:]
        tail.append(line)
        total += len(line)
        while total > STDERR_TAIL and len(tail) > 1:
            total -= len(tail.popleft())


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    # start_new_session made the agent a process-group leader, so this also
    # takes down whatever it spawned (dev servers, test runners).
    for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(proc.wait(), grace)
            return
        except asyncio.TimeoutError:
            continue


async def stream_process(
    args: list[str],
    cwd: Path,
    stdin_text: str,
    feed,
    timeout_seconds: float,
) -> AsyncIterator[tuple[Event, Optional[int], str]]:
    """Run args with stdin_text as input.

    Yields (event, None, "") for every event `feed` produces from stdout, then
    exactly one closing ({}, returncode, stderr_tail). Cancelling the consumer
    (the Stop button) kills the agent's whole process group."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,
        start_new_session=True,
    )
    tail: deque[str] = deque()
    stderr_task = asyncio.create_task(_drain(proc.stderr, tail))
    timed_out = False
    try:
        try:
            proc.stdin.write(stdin_text.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the process died early; its exit code/stderr explain why

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), remaining)
            except asyncio.TimeoutError:
                timed_out = True
                break
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                for event in feed(parsed):
                    yield event, None, ""
        if timed_out:
            await _terminate(proc)
        returncode = await proc.wait()
    finally:
        # Reached on normal exit, timeout, and cancellation (Stop button).
        await _terminate(proc)
        await asyncio.gather(stderr_task, return_exceptions=True)

    stderr = "".join(tail)
    if timed_out:
        stderr = "timeout"
    yield {}, returncode, stderr


class ClaudeDriver:
    name = "claude"

    def __init__(self, allowed_tools: list[str], permission_mode: str, timeout_seconds: float):
        self._allowed_tools = allowed_tools
        self._permission_mode = permission_mode
        self._timeout = timeout_seconds

    def build_args(self, req: TurnRequest) -> list[str]:
        args = [
            "claude", "-p",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--permission-mode", self._permission_mode,
            "--append-system-prompt", RESPONSE_LANGUAGE_INSTRUCTION,
        ]
        if req.model:
            args.extend(["--model", req.model])
        if req.effort:
            args.extend(["--effort", req.effort])
        if req.session_id:
            args.extend(["--resume", req.session_id])
        if self._allowed_tools:
            args.extend(["--allowedTools", ",".join(self._allowed_tools)])
        for directory in req.extra_dirs:
            args.extend(["--add-dir", directory])
        return args

    async def run(self, req: TurnRequest) -> AsyncIterator[Event]:
        normalizer = ClaudeNormalizer()
        done_seen = False
        async for event, returncode, stderr in stream_process(
            self.build_args(req), req.cwd, req.prompt, normalizer.feed, self._timeout
        ):
            if returncode is None:
                done_seen = done_seen or event["type"] == "done"
                yield event
                continue
            if not done_seen:
                yield {"type": "done", "ok": False, "error": _exit_error("claude", returncode, stderr)}


class CodexDriver:
    name = "codex"

    def __init__(self, sandbox: str, timeout_seconds: float):
        self._sandbox = sandbox
        self._timeout = timeout_seconds

    def build_args(self, req: TurnRequest) -> list[str]:
        if req.session_id:
            args = ["codex", "exec", "resume", req.session_id, "--json"]
            # `exec resume` has no --add-dir, but -c can still widen the
            # sandbox per call (adds to cwd, doesn't replace it).
            if req.extra_dirs:
                roots = ", ".join(json.dumps(d) for d in req.extra_dirs)
                args.extend(["-c", f"sandbox_workspace_write.writable_roots=[{roots}]"])
        else:
            args = ["codex", "exec", "--json", "-s", self._sandbox]
            for directory in req.extra_dirs:
                args.extend(["--add-dir", directory])
        if req.model:
            args.extend(["-m", req.model])
        if req.effort:
            args.extend(["-c", f"model_reasoning_effort={json.dumps(req.effort)}"])
        args.append("-")  # prompt from stdin
        return args

    async def run(self, req: TurnRequest) -> AsyncIterator[Event]:
        normalizer = CodexNormalizer()
        # codex has no separate system-prompt flag; the instruction rides
        # along with the prompt text.
        prompt = f"{RESPONSE_LANGUAGE_INSTRUCTION}\n\n{req.prompt}"
        async for event, returncode, stderr in stream_process(
            self.build_args(req), req.cwd, prompt, normalizer.feed, self._timeout
        ):
            if returncode is None:
                yield event
                continue
            if returncode == 0 and normalizer.error is None:
                yield {"type": "done", "ok": True, "error": None}
            else:
                yield {
                    "type": "done",
                    "ok": False,
                    "error": normalizer.error or _exit_error("codex", returncode, stderr),
                }


def _exit_error(name: str, returncode: int, stderr: str) -> str:
    if stderr == "timeout":
        return f"{name}: превышено время ожидания хода"
    errors = [line for line in stderr.splitlines() if "ERROR" in line or "error" in line.lower()]
    detail = "\n".join(errors[-5:]) or stderr.strip()[-600:]
    return f"{name} завершился с кодом {returncode}" + (f": {detail}" if detail else "")
