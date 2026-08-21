from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from .base import AgentResult, AgentRunner, ProgressCallback

DEFAULT_TIMEOUT_SECONDS = 20 * 60
MAX_DETAIL_LEN = 150
# asyncio's default StreamReader limit is 64KiB per line — a single
# stream-json line can easily exceed that (e.g. a tool_result embedding a
# large file's contents), raising LimitOverrunError and killing the read
# loop. Bump it well above anything a single JSON event should hit.
STREAM_LIMIT = 16 * 1024 * 1024


class ClaudeAgent(AgentRunner):
    name = "claude"

    def __init__(
        self,
        allowed_tools: list[str],
        permission_mode: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self._allowed_tools = allowed_tools
        self._permission_mode = permission_mode
        self._timeout_seconds = timeout_seconds

    async def start(self, prompt: str, cwd: Path, on_progress: Optional[ProgressCallback] = None) -> AgentResult:
        args = self._base_args(prompt)
        return await self._run(args, cwd, on_progress)

    async def resume(
        self, session_id: str, prompt: str, cwd: Path, on_progress: Optional[ProgressCallback] = None
    ) -> AgentResult:
        args = self._base_args(prompt)
        args.extend(["--resume", session_id])
        return await self._run(args, cwd, on_progress)

    def _base_args(self, prompt: str) -> list[str]:
        # prompt must precede --allowedTools: it's a variadic flag and will
        # swallow the next bare token (including the prompt) as a tool name.
        args = [
            "claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", self._permission_mode,
        ]
        if self._allowed_tools:
            args.extend(["--allowedTools", ",".join(self._allowed_tools)])
        return args

    async def _run(self, args: list[str], cwd: Path, on_progress: Optional[ProgressCallback]) -> AgentResult:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )

        final_event: Optional[dict] = None
        raw_lines: list[str] = []

        async def read_stdout() -> None:
            nonlocal final_event
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                raw_lines.append(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result":
                    final_event = event
                elif on_progress:
                    text = _describe_event(event)
                    if text:
                        await on_progress(text)

        try:
            async with asyncio.timeout(self._timeout_seconds):
                await read_stdout()
                await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return AgentResult(success=False, session_id=None, summary="claude timed out", raw_output="")

        raw = "\n".join(raw_lines)

        if final_event is None:
            stderr = (await proc.stderr.read()).decode(errors="replace")
            return AgentResult(
                success=False,
                session_id=None,
                summary=f"claude exited with code {proc.returncode} without a result event: {stderr[:500]}",
                raw_output=raw,
            )

        return AgentResult(
            success=not final_event.get("is_error", False),
            session_id=final_event.get("session_id"),
            summary=str(final_event.get("result", "")),
            raw_output=raw,
        )


def _describe_event(event: dict) -> Optional[str]:
    if event.get("type") != "assistant":
        return None
    for block in event.get("message", {}).get("content", []):
        block_type = block.get("type")
        if block_type == "tool_use":
            name = block.get("name", "?")
            detail = _tool_detail(block.get("input", {}))
            text = f"🔧 {name}"
            if detail:
                text += f": {_truncate(detail)}"
            return text
        if block_type == "text":
            text = block.get("text", "").strip()
            if text:
                return f"💬 {_truncate(text)}"
    return None


def _tool_detail(tool_input: dict) -> str:
    for key in ("file_path", "pattern", "description", "command"):
        if key in tool_input and tool_input[key]:
            return str(tool_input[key])
    return ""


def _truncate(text: str) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= MAX_DETAIL_LEN else text[:MAX_DETAIL_LEN] + "…"
