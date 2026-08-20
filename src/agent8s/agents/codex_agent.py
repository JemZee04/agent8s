from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from .base import AgentResult, AgentRunner

DEFAULT_TIMEOUT_SECONDS = 20 * 60


class CodexAgent(AgentRunner):
    name = "codex"

    def __init__(self, sandbox: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self._sandbox = sandbox
        self._timeout_seconds = timeout_seconds

    async def start(self, prompt: str, cwd: Path) -> AgentResult:
        args = ["codex", "exec", "--json", "-s", self._sandbox]
        return await self._run(args, prompt, cwd)

    async def resume(self, session_id: str, prompt: str, cwd: Path) -> AgentResult:
        args = ["codex", "exec", "resume", session_id, "--json"]
        return await self._run(args, prompt, cwd)

    async def _run(self, args: list[str], prompt: str, cwd: Path) -> AgentResult:
        with tempfile.NamedTemporaryFile(prefix="agent8s-codex-", suffix=".txt", delete=False) as tmp:
            last_message_path = Path(tmp.name)

        full_args = [*args, "-o", str(last_message_path), prompt]
        proc = await asyncio.create_subprocess_exec(
            *full_args,
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
            last_message_path.unlink(missing_ok=True)
            return AgentResult(success=False, session_id=None, summary="codex timed out", raw_output="")

        raw = stdout.decode(errors="replace")
        thread_id = self._extract_thread_id(raw)

        try:
            summary = last_message_path.read_text().strip()
        except OSError:
            summary = ""
        finally:
            last_message_path.unlink(missing_ok=True)

        if proc.returncode != 0:
            return AgentResult(
                success=False,
                session_id=thread_id,
                summary=summary or f"codex exited with code {proc.returncode}: {stderr.decode(errors='replace')[:500]}",
                raw_output=raw,
            )

        return AgentResult(success=True, session_id=thread_id, summary=summary, raw_output=raw)

    @staticmethod
    def _extract_thread_id(raw_jsonl: str) -> str | None:
        for line in raw_jsonl.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                return event.get("thread_id")
        return None
