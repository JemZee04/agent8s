import asyncio
import os
import signal
import sys

import pytest

from agent8s.desktop.drivers import stream_process

SCRIPT = (
    "sleep 300 & echo $! > {pidfile}; "
    "echo '{{\"type\": \"hello\"}}'; "
    "wait"
)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_cancelling_kills_the_agents_whole_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    seen = []

    async def consume():
        async for event, returncode, _ in stream_process(
            ["sh", "-c", SCRIPT.format(pidfile=pidfile)], tmp_path, "", lambda e: [e], 60
        ):
            seen.append(event)

    task = asyncio.create_task(consume())
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.05)
    assert seen == [{"type": "hello"}]
    child = int(pidfile.read_text())
    assert alive(child)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.2)
    assert not alive(child), "the grandchild survived Stop"


async def test_timeout_kills_and_reports(tmp_path):
    results = []
    async for event, returncode, stderr in stream_process(["sh", "-c", "sleep 300"], tmp_path, "", lambda e: [e], 0.3):
        results.append((returncode, stderr))
    assert results[-1][1] == "timeout" and results[-1][0] != 0


async def test_chatty_stderr_does_not_deadlock(tmp_path):
    # ~2 MB on stderr, far beyond the OS pipe buffer, before any stdout.
    script = "python3 -c \"import sys; sys.stderr.write('e'*2000000); print('{}')\""
    out = [r async for r in stream_process(["sh", "-c", script], tmp_path, "", lambda e: [e], 30)]
    _, returncode, stderr = out[-1]
    assert returncode == 0 and 0 < len(stderr) <= 8_000  # tail is kept, but bounded


async def test_large_json_line_is_not_truncated(tmp_path):
    script = "python3 -c \"print('{\\\"v\\\": \\\"' + 'x'*3000000 + '\\\"}')\""
    events = [e async for e, rc, _ in stream_process(["sh", "-c", script], tmp_path, "", lambda e: [e], 30) if rc is None]
    assert len(events[0]["v"]) == 3000000
