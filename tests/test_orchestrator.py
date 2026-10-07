import asyncio
import subprocess
from pathlib import Path

import pytest

from agent8s.config import DesktopConfig
from agent8s.db import Database
from agent8s.desktop.runner import Busy, Hub, Orchestrator, UserError
from agent8s.desktop.store import Store


class FakeDriver:
    """Scripted stand-in for a CLI: records requests, replays canned events."""

    def __init__(self, name, scripts):
        self.name = name
        self.scripts = list(scripts)
        self.requests = []

    async def run(self, req):
        self.requests.append(req)
        script = self.scripts.pop(0)
        for event in script:
            if event == "hang":
                await asyncio.sleep(3600)
            yield event


def reply(text, sid):
    return [
        {"type": "session", "id": sid},
        {"type": "text", "text": text},
        {"type": "done", "ok": True, "error": None},
    ]


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    config = DesktopConfig(tmp_path / "data", tmp_path / "wt", tmp_path, [], "acceptEdits", "workspace-write", 60)
    config.data_dir.mkdir()
    db = Database(config.db_path)
    store = Store(config.db_path)
    orch = Orchestrator(config, db, store, Hub())
    orch.catalog = {"agents": [{"id": "claude", "label": "Claude Code", "models": [{"id": "", "label": "d"}]},
                               {"id": "codex", "label": "Codex", "models": [{"id": "", "label": "d"}]}]}
    project = orch.add_project("proj", str(repo))
    return orch, store, project, repo


async def settle(orch, chat_id):
    live = orch._live.get(chat_id)
    if live:
        await live.task


async def test_worktree_chat_roundtrip(env):
    orch, store, project, _ = env
    orch._drivers = {"claude": FakeDriver("claude", [reply("hi", "S1")])}
    chat = await orch.create_chat(project.id, "claude")
    assert Path(chat["worktree_path"]).is_dir() and chat["branch"] == f"agent8s/chat-{chat['id']}"

    await orch.send(chat["id"], "hello there")
    await settle(orch, chat["id"])

    data = orch.get_chat_with_messages(chat["id"])
    assert data["chat"]["title"] == "hello there" and data["chat"]["status"] == "idle"
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
    assert data["messages"][1]["parts"][0]["text"] == "hi" and data["messages"][1]["status"] == "done"


async def test_switching_agent_hands_over_the_history(env):
    orch, store, project, _ = env
    claude = FakeDriver("claude", [reply("claude answer", "C1"), reply("claude again", "C1")])
    codex = FakeDriver("codex", [reply("codex answer", "X1")])
    orch._drivers = {"claude": claude, "codex": codex}
    chat = await orch.create_chat(project.id, "claude")
    cid = chat["id"]

    await orch.send(cid, "first question")
    await settle(orch, cid)

    orch.update_chat(cid, None, "codex", None, None)
    await orch.send(cid, "second question")
    await settle(orch, cid)
    # codex has no native session yet: it must be told the whole earlier conversation
    prompt = codex.requests[0].prompt
    assert codex.requests[0].session_id is None
    assert "first question" in prompt and "claude answer" in prompt and prompt.endswith("second question")

    orch.update_chat(cid, None, "claude", None, None)
    await orch.send(cid, "third question")
    await settle(orch, cid)
    # claude resumes its own session, and is told only what it missed (codex's turn)
    resumed = claude.requests[1]
    assert resumed.session_id == "C1"
    assert "codex answer" in resumed.prompt and "first question" not in resumed.prompt
    assert resumed.prompt.endswith("third question")
    # the "Switched to ..." notes are UI-only
    assert "Переключено" not in resumed.prompt


async def test_stale_session_is_retried_from_scratch(env):
    orch, store, project, _ = env
    failing = [{"type": "done", "ok": False, "error": "No conversation found with session ID: dead"}]
    driver = FakeDriver("claude", [reply("one", "OLD"), failing, reply("recovered", "NEW")])
    orch._drivers = {"claude": driver}
    chat = await orch.create_chat(project.id, "claude")
    cid = chat["id"]
    await orch.send(cid, "q1")
    await settle(orch, cid)
    await orch.send(cid, "q2")
    await settle(orch, cid)

    assert driver.requests[1].session_id == "OLD"
    assert driver.requests[2].session_id is None and "q1" in driver.requests[2].prompt
    last = orch.get_chat_with_messages(cid)["messages"][-1]
    assert last["status"] == "done" and last["parts"][0]["text"] == "recovered"
    assert store.get_chat(cid).sessions["claude"]["id"] == "NEW"


async def test_failure_is_recorded_and_chat_is_not_stuck(env):
    orch, store, project, _ = env
    orch._drivers = {"claude": FakeDriver("claude", [[{"type": "done", "ok": False, "error": "auth expired"}]])}
    chat = await orch.create_chat(project.id, "claude")
    await orch.send(chat["id"], "go")
    await settle(orch, chat["id"])

    data = orch.get_chat_with_messages(chat["id"])
    assert data["chat"]["status"] == "error" and not data["chat"]["running"]
    last = data["messages"][-1]
    assert last["status"] == "error" and last["parts"][-1] == {"type": "error", "text": "auth expired"}


async def test_driver_crash_does_not_leave_chat_running(env):
    orch, store, project, _ = env

    class Exploding:
        async def run(self, req):
            raise FileNotFoundError("claude")
            yield  # pragma: no cover

    orch._drivers = {"claude": Exploding()}
    chat = await orch.create_chat(project.id, "claude")
    await orch.send(chat["id"], "go")
    await settle(orch, chat["id"])
    assert orch.get_chat_with_messages(chat["id"])["chat"]["status"] == "error"


async def test_stop_interrupts_and_keeps_partial_output(env):
    orch, store, project, _ = env
    orch._drivers = {"claude": FakeDriver("claude", [[{"type": "session", "id": "S"},
                                                      {"type": "text", "text": "partial"}, "hang"]])}
    chat = await orch.create_chat(project.id, "claude")
    await orch.send(chat["id"], "go")
    await asyncio.sleep(0.05)

    with pytest.raises(Busy):  # one turn at a time per chat
        await orch.send(chat["id"], "again")
    assert await orch.stop(chat["id"]) is True

    data = orch.get_chat_with_messages(chat["id"])
    assert data["chat"]["status"] == "idle" and not data["chat"]["running"]
    last = data["messages"][-1]
    assert last["status"] == "interrupted" and last["parts"][0]["text"] == "partial"


async def test_chats_run_in_parallel(env):
    orch, store, project, _ = env
    orch._drivers = {"claude": FakeDriver("claude", [[{"type": "session", "id": "A"}, "hang"],
                                                      [{"type": "session", "id": "B"}, "hang"]])}
    a = await orch.create_chat(project.id, "claude")
    b = await orch.create_chat(project.id, "claude")
    await orch.send(a["id"], "one")
    await orch.send(b["id"], "two")
    await asyncio.sleep(0.05)
    assert {c["id"] for c in orch.list_chats() if c["running"]} == {a["id"], b["id"]}
    await orch.shutdown()


async def test_switch_is_refused_while_running(env):
    orch, store, project, _ = env
    orch._drivers = {"claude": FakeDriver("claude", [[{"type": "session", "id": "S"}, "hang"]])}
    chat = await orch.create_chat(project.id, "claude")
    await orch.send(chat["id"], "go")
    await asyncio.sleep(0.05)
    with pytest.raises(Busy):
        orch.update_chat(chat["id"], None, "codex", None, None)
    await orch.shutdown()


async def test_commit_merge_and_discard(env):
    orch, store, project, repo = env
    orch._drivers = {"claude": FakeDriver("claude", [reply("ok", "S")])}
    chat = await orch.create_chat(project.id, "claude")
    worktree = Path(chat["worktree_path"])
    (worktree / "new.txt").write_text("data")

    diff = await orch.diff(chat["id"])
    assert "new.txt" in diff["diff"] and "new.txt" in diff["status"]
    assert store.get_chat(chat["id"]).changes == 1

    await orch.merge(chat["id"], "feat: add new.txt")
    assert (repo / "new.txt").read_text() == "data"

    await orch.discard(chat["id"])
    assert not worktree.exists() and store.get_chat(chat["id"]) is None


async def test_merge_conflict_is_aborted_cleanly(env):
    orch, store, project, repo = env
    chat = await orch.create_chat(project.id, "claude")
    worktree = Path(chat["worktree_path"])
    (repo / "f.txt").write_text("main\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "main side"], cwd=repo, check=True)
    (worktree / "f.txt").write_text("branch\n")

    with pytest.raises(UserError):
        await orch.merge(chat["id"], "conflicting")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert status.strip() == ""  # no half-merged state left in the user's repo


async def test_validation(env):
    orch, store, project, _ = env
    with pytest.raises(UserError):
        await orch.create_chat(project.id, "gemini")
    with pytest.raises(UserError):
        await orch.create_chat(project.id, "claude", model="--dangerous")
    with pytest.raises(UserError):
        orch.add_project("x", "relative/path")


async def test_restart_reconciliation(env):
    orch, store, project, _ = env
    chat = await orch.create_chat(project.id, "claude")
    msg = store.add_message(chat["id"], "assistant", [], status="running")
    store.update_chat(chat["id"], status="running")
    assert store.reconcile_interrupted() == 1
    assert store.get_message(msg.id).status == "interrupted" and store.get_chat(chat["id"]).status == "idle"


async def test_a_sign_in_failure_tells_you_how_to_fix_it(env):
    orch, store, project, _ = env
    orch._drivers = {"claude": FakeDriver("claude", [[
        {"type": "done", "ok": False, "error": "Failed to authenticate: OAuth session expired and could not be refreshed"}]])}
    chat = await orch.create_chat(project.id, "claude")
    await orch.send(chat["id"], "привет")
    await settle(orch, chat["id"])
    error = orch.get_chat_with_messages(chat["id"])["messages"][-1]["parts"][-1]
    assert error["type"] == "error" and "OAuth session expired" in error["text"] and "claude auth login" in error["text"]
