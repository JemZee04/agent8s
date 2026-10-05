"""Context handoff between agents.

claude and codex keep their conversations in incompatible native formats, so
a chat can't be moved between them by copying a session. Instead the app owns
the canonical history and, when an agent resumes (or starts) a chat that
contains turns it never saw, prepends a compact transcript of those turns to
its next prompt. The files in the worktree carry the real state; the
transcript carries the intent and the decisions.
"""
from __future__ import annotations

import json
from typing import Any

from .store import Message

DEFAULT_BUDGET = 48_000
TEXT_CAP = {"user": 6_000, "assistant": 8_000}
TOOL_LINE_CAP = 200
_INPUT_KEYS = ("command", "file_path", "path", "pattern", "description", "url", "query")


def _cap(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f" … [обрезано, ещё {len(text) - limit} символов]"


def _tool_summary(part: dict[str, Any]) -> str:
    name = part.get("name", "tool")
    tool_input = part.get("input")
    detail = ""
    if isinstance(tool_input, dict):
        for key in _INPUT_KEYS:
            if tool_input.get(key):
                detail = str(tool_input[key])
                break
        else:
            if tool_input.get("changes"):
                detail = ", ".join(map(str, tool_input["changes"]))
            elif tool_input:
                detail = json.dumps(tool_input, ensure_ascii=False)
    elif tool_input:
        detail = str(tool_input)
    line = f"· {name}" + (f": {detail}" if detail else "")
    line = _cap(line.replace("\n", " "), TOOL_LINE_CAP)
    return line + (" (ошибка)" if part.get("is_error") else "")


def _render_message(message: Message) -> str:
    if message.role == "user":
        header = "[user]"
    else:
        header = f"[{message.agent or 'agent'}" + (f" · {message.model}]" if message.model else "]")
    lines = [header]
    for part in message.parts:
        if part.get("type") == "text" and part.get("text", "").strip():
            lines.append(_cap(part["text"], TEXT_CAP.get(message.role, 6_000)))
        elif part.get("type") == "tool":
            lines.append(_tool_summary(part))
    if message.status in ("interrupted", "error"):
        lines.append("(ход был прерван или завершился ошибкой)")
    return "\n".join(lines)


def render_transcript(messages: list[Message], budget: int = DEFAULT_BUDGET) -> str:
    entries = [_render_message(m) for m in messages if m.role in ("user", "assistant")]
    kept: list[str] = []
    used = 0
    for entry in reversed(entries):
        if kept and used + len(entry) > budget:
            break
        kept.append(entry)
        used += len(entry)
    kept.reverse()
    omitted = len(entries) - len(kept)
    if omitted:
        kept.insert(0, f"[… {omitted} более ранних сообщений опущено …]")
    return "\n\n".join(kept)


def build_turn_prompt(
    user_text: str,
    unseen: list[Message],
    fresh: bool,
    workspace_note: str = "",
    budget: int = DEFAULT_BUDGET,
) -> str:
    """The prompt to send for this turn.

    unseen: earlier messages the target agent's native session has not seen.
    fresh:  True when there is no native session at all (new chat for this
            agent, or switched over from another one).
    """
    transcript = render_transcript(unseen, budget)
    if not transcript:
        return user_text
    reason = (
        "You have no memory of the earlier turns — they happened in other sessions or were handled by another agent."
        if fresh
        else "While you were away, the turns below were handled by other agents, so you have not seen them."
    )
    note = f"\n\nWorking tree right now:\n{workspace_note.strip()}" if workspace_note.strip() else ""
    return (
        "<conversation_context>\n"
        f"You are continuing an existing conversation in this working directory. {reason} "
        "Earlier turns, oldest first (tool outputs omitted; the files on disk are the source of truth):\n\n"
        f"{transcript}{note}\n"
        "</conversation_context>\n\n"
        f"{user_text}"
    )
