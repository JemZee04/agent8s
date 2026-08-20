from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .base import AgentResult, AgentRunner

DEFAULT_TIMEOUT_SECONDS = 20 * 60


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

    async def start(self, prompt: str, cwd: Path) -> AgentResult:
        args = self._base_args(prompt)
        return await self._run(args, cwd)

    async def resume(self, session_id: str, prompt: str, cwd: Path) -> AgentResult:
        args = self._base_args(prompt)
        args.extend(["--resume", session_id])
        return await self._run(args, cwd)

    def _base_args(self, prompt: str) -> list[str]:
        # prompt must precede --allowedTools: it's a variadic flag and will
        # swallow the next bare token (including the prompt) as a tool name.
        args = ["claude", "-p", prompt, "--output-format", "json", "--permission-mode", self._permission_mode]
        if self._allowed_tools:
            args.extend(["--allowedTools", ",".join(self._allowed_tools)])
        return args

    async def _run(self, args: list[str], cwd: Path) -> AgentResult:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return AgentResult(success=False, session_id=None, summary="claude timed out", raw_output="")

        raw = stdout.decode(errors="replace")
        if proc.returncode != 0:
            return AgentResult(
                success=False,
                session_id=None,
                summary=f"claude exited with code {proc.returncode}: {stderr.decode(errors='replace')[:500]}",
                raw_output=raw,
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return AgentResult(success=False, session_id=None, summary="could not parse claude output", raw_output=raw)

        return AgentResult(
            success=not data.get("is_error", False),
            session_id=data.get("session_id"),
            summary=str(data.get("result", "")),
            raw_output=raw,
        )
