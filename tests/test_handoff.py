from agent8s.desktop.handoff import build_turn_prompt, render_transcript
from agent8s.desktop.runner import apply_event
from agent8s.desktop.store import Message


def msg(i, role, text, agent=None, model=None, parts=None, status="done"):
    return Message(i, 1, role, agent, model, parts or [{"type": "text", "text": text}], status, "t")


def test_no_history_means_plain_prompt():
    assert build_turn_prompt("hello", [], fresh=True) == "hello"


def test_transcript_attributes_agents_and_summarizes_tools():
    history = [
        msg(1, "user", "fix the bug"),
        msg(2, "assistant", "", agent="codex", model="gpt-x", parts=[
            {"type": "tool", "id": "a", "name": "shell", "input": {"command": "pytest -q"}, "output": "x" * 5000,
             "is_error": True, "status": "done"},
            {"type": "thinking", "text": "secret reasoning"},
            {"type": "text", "text": "tests fail"},
        ]),
        msg(3, "system", "Переключено на Claude"),
    ]
    out = render_transcript(history)
    assert "[user]\nfix the bug" in out
    assert "[codex · gpt-x]" in out
    assert "· shell: pytest -q (ошибка)" in out
    assert "tests fail" in out
    assert "secret reasoning" not in out and "x" * 100 not in out  # outputs and thinking are omitted
    assert "Переключено" not in out  # UI-only system notes are not part of the conversation


def test_budget_drops_oldest_first():
    history = [msg(i, "user", f"message-{i} " + "z" * 300) for i in range(1, 21)]
    out = render_transcript(history, budget=1500)
    assert "message-20" in out and "message-1 " not in out
    assert out.startswith("[… ") and "более ранних" in out


def test_prompt_wraps_context_and_keeps_request_last():
    prompt = build_turn_prompt("now do X", [msg(1, "user", "earlier")], fresh=False, workspace_note=" M a.py")
    assert prompt.startswith("<conversation_context>")
    assert "While you were away" in prompt and "M a.py" in prompt
    assert prompt.endswith("now do X")
    assert "no memory" in build_turn_prompt("x", [msg(1, "user", "e")], fresh=True)


def test_interrupted_turns_are_flagged():
    out = render_transcript([msg(1, "assistant", "partial", agent="claude", status="interrupted")])
    assert "прерван" in out


# -- the reducer that builds message parts ------------------------------------


def test_reducer_builds_parts_and_ops():
    parts = []
    assert apply_event(parts, {"type": "text", "text": "he"})[0]["op"] == "set"
    assert apply_event(parts, {"type": "text", "text": "llo"}) == [{"op": "append", "i": 0, "text": "llo"}]
    apply_event(parts, {"type": "tool_start", "id": "t", "name": "Bash"})
    apply_event(parts, {"type": "tool_input", "id": "t", "input": {"command": "ls"}})
    apply_event(parts, {"type": "tool_end", "id": "t", "output": "ok", "is_error": False})
    apply_event(parts, {"type": "text", "text": "after"})  # a tool in between starts a new text part
    assert [p["type"] for p in parts] == ["text", "tool", "text"]
    assert parts[0]["text"] == "hello"
    assert parts[1]["status"] == "done" and parts[1]["input"] == {"command": "ls"}


def test_reducer_error_and_usage_parts():
    parts = []
    apply_event(parts, {"type": "usage", "input_tokens": 5, "cost_usd": None})
    apply_event(parts, {"type": "done", "ok": False, "error": "boom"})
    assert parts == [{"type": "usage", "input_tokens": 5}, {"type": "error", "text": "boom"}]
    assert apply_event([], {"type": "done", "ok": True, "error": None}) == []
    assert apply_event([], {"type": "tool_end", "id": "ghost", "output": "", "is_error": False}) == []
