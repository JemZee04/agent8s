from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Optional

from .base import AgentResult, AgentRunner, ProgressCallback

DEFAULT_TIMEOUT_SECONDS = 20 * 60
MAX_DETAIL_LEN = 150


class CodexAgent(AgentRunner):
    name = "codex"

    def __init__(self, sandbox: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self._sandbox = sandbox
        self._timeout_seconds = timeout_seconds

    async def start(self, prompt: str, cwd: Path, on_progress: Optional[ProgressCallback] = None) -> AgentResult:
        args = ["codex", "exec", "--json", "-s", self._sandbox]
        return await self._run(args, prompt, cwd, on_progress)

    async def resume(
        self, session_id: str, prompt: str, cwd: Path, on_progress: Optional[ProgressCallback] = None
    ) -> AgentResult:
        args = ["codex", "exec", "resume", session_id, "--json"]
        return await self._run(args, prompt, cwd, on_progress)

    async def _run(
        self, args: list[str], prompt: str, cwd: Path, on_progress: Optional[ProgressCallback]
    ) -> AgentResult:
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

        thread_id: Optional[str] = None
        raw_lines: list[str] = []

        async def read_stdout() -> None:
            nonlocal thread_id
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                raw_lines.append(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                elif event.get("type") == "item.completed" and on_progress:
                    text = _describe_item(event.get("item", {}))
                    if text:
                        await on_progress(text)

        try:
            async with asyncio.timeout(self._timeout_seconds):
                await read_stdout()
                await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            last_message_path.unlink(missing_ok=True)
            return AgentResult(success=False, session_id=None, summary="codex timed out", raw_output="")

        raw = "\n".join(raw_lines)

        try:
            summary = last_message_path.read_text().strip()
        except OSError:
            summary = ""
        finally:
            last_message_path.unlink(missing_ok=True)

        if proc.returncode != 0:
            stderr = (await proc.stderr.read()).decode(errors="replace")
            return AgentResult(
                success=False,
                session_id=thread_id,
                summary=summary or f"codex exited with code {proc.returncode}: {stderr[:500]}",
                raw_output=raw,
            )

        return AgentResult(success=True, session_id=thread_id, summary=summary, raw_output=raw)


def _describe_item(item: dict) -> Optional[str]:
    item_type = item.get("type")
    if item_type == "command_execution":
        command = str(item.get("command", ""))
        return f"🔧 {_truncate(command)}"
    if item_type == "file_change":
        changes = item.get("changes", [])
        if not changes:
            return None
        parts = [f"{c.get('kind', '?')} {Path(c.get('path', '?')).name}" for c in changes]
        return f"📝 {_truncate(', '.join(parts))}"
    if item_type == "agent_message":
        text = str(item.get("text", "")).strip()
        if text:
            return f"💬 {_truncate(text)}"
    return None


def _truncate(text: str) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= MAX_DETAIL_LEN else text[:MAX_DETAIL_LEN] + "…"
