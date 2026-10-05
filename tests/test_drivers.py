import json
from pathlib import Path

from agent8s.desktop.drivers import (
    ClaudeDriver,
    ClaudeNormalizer,
    CodexDriver,
    CodexNormalizer,
    TurnRequest,
    unwrap_shell,
)

FIXTURES = Path(__file__).parent / "fixtures"


def replay(normalizer, name):
    events = []
    for line in (FIXTURES / name).read_text().splitlines():
        events.extend(normalizer.feed(json.loads(line)))
    return events


def test_claude_stream_is_normalized_in_order():
    events = replay(ClaudeNormalizer(), "claude_stream.jsonl")
    kinds = [e["type"] for e in events]

    assert kinds[0] == "session"
    assert kinds[-1] == "done" and events[-1]["ok"] is True
    # tool lifecycle: start -> input -> end(output "hi")
    start = next(e for e in events if e["type"] == "tool_start")
    assert start["name"] == "Bash"
    assert next(e for e in events if e["type"] == "tool_input")["id"] == start["id"]
    end = next(e for e in events if e["type"] == "tool_end")
    assert (end["id"], end["output"], end["is_error"]) == (start["id"], "hi", False)
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Done."
    assert kinds.index("tool_start") < kinds.index("tool_end") < kinds.index("done")


def test_claude_does_not_duplicate_text_already_streamed():
    # The complete `assistant` message repeats text that arrived as deltas.
    n = ClaudeNormalizer()
    n.feed({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}})
    streamed = n.feed(
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}}
    )
    repeated = n.feed({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "hi"}]}})
    assert streamed == [{"type": "text", "text": "hi"}]
    assert repeated == []


def test_claude_falls_back_to_complete_message_without_partials():
    n = ClaudeNormalizer()
    out = n.feed({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "hello"}]}})
    assert out == [{"type": "text", "text": "hello"}]


def test_claude_ignores_subagent_events():
    n = ClaudeNormalizer()
    out = n.feed({"type": "assistant", "parent_tool_use_id": "t1",
                  "message": {"id": "m", "content": [{"type": "text", "text": "inner"}]}})
    assert out == []


def test_claude_error_result():
    out = ClaudeNormalizer().feed({"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 400"})
    assert out[-1] == {"type": "done", "ok": False, "error": "API Error: 400"}


def test_codex_stream_is_normalized():
    events = replay(CodexNormalizer(), "codex_stream.jsonl")
    assert events[0] == {"type": "session", "id": "01a10bab-1264-7381-b1ed-00c0ff6aa322"}
    tools = [e for e in events if e["type"] == "tool_input"]
    assert tools and tools[0]["input"]["command"].startswith("sed -n")  # /bin/zsh -lc "…" unwrapped
    assert any(e["type"] == "tool_end" and e["output"] for e in events)
    assert any(e["type"] == "text" for e in events)
    assert events[-1]["type"] == "usage"


def test_codex_consecutive_messages_are_separated():
    n = CodexNormalizer()
    first = n.feed({"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "a"}})
    second = n.feed({"type": "item.completed", "item": {"id": "2", "type": "agent_message", "text": "b"}})
    assert first[0]["text"] == "a" and second[0]["text"] == "\n\nb"


def test_codex_failed_command_is_an_error():
    n = CodexNormalizer()
    out = n.feed({"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": "false",
                                                     "aggregated_output": "", "exit_code": 1, "status": "failed"}})
    assert out[-1]["is_error"] is True


def test_codex_turn_failure_is_recorded():
    n = CodexNormalizer()
    n.feed({"type": "turn.failed", "error": {"message": "boom"}})
    assert n.error == "boom"


def test_unwrap_shell():
    assert unwrap_shell("/bin/zsh -lc \"echo 'a b'\"") == "echo 'a b'"
    assert unwrap_shell("ls -la") == "ls -la"


def test_claude_args(tmp_path):
    driver = ClaudeDriver(["Bash", "Edit"], "acceptEdits", 10)
    args = driver.build_args(TurnRequest("p", tmp_path, model="opus", effort="high", session_id="S", extra_dirs=["/x"]))
    assert args[:2] == ["claude", "-p"]
    assert args[args.index("--model") + 1] == "opus"
    assert args[args.index("--effort") + 1] == "high"
    assert args[args.index("--resume") + 1] == "S"
    assert args[args.index("--allowedTools") + 1] == "Bash,Edit"
    assert "p" not in args  # the prompt goes through stdin, not argv


def test_codex_args_fresh_vs_resume(tmp_path):
    driver = CodexDriver("workspace-write", 10)
    fresh = driver.build_args(TurnRequest("p", tmp_path, model="m", extra_dirs=["/x"]))
    assert fresh[:5] == ["codex", "exec", "--json", "-s", "workspace-write"]
    assert fresh[fresh.index("--add-dir") + 1] == "/x" and fresh[-1] == "-"

    resumed = driver.build_args(TurnRequest("p", tmp_path, session_id="S", effort="high", extra_dirs=["/x"]))
    assert resumed[:4] == ["codex", "exec", "resume", "S"]
    assert "--add-dir" not in resumed and "-s" not in resumed
    assert 'sandbox_workspace_write.writable_roots=["/x"]' in resumed
    assert 'model_reasoning_effort="high"' in resumed
