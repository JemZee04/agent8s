import json
import subprocess
from pathlib import Path

import pytest

from agent8s.config import DesktopConfig
from agent8s.db import Database
from agent8s.desktop import importer, runner as runner_module
from agent8s.desktop.runner import Hub, Orchestrator, UserError
from agent8s.desktop.store import Store


def entry(kind, content, **extra):
    base = {"type": kind, "message": {"role": kind, "content": content}, "sessionId": "S", "isSidechain": False}
    base.update(extra)
    return base


def write_session(root: Path, cwd: Path, lines: list[dict], session_id="11111111-aaaa-bbbb-cccc-222222222222"):
    folder = root / importer.encode_cwd(str(cwd))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def conversation(cwd):
    c = {"cwd": str(cwd), "gitBranch": "main"}
    return [
        {"type": "mode", "mode": "x", "sessionId": "S"},
        entry("user", "Почини баг в парсере", timestamp="2026-01-01T10:00:00Z", **c),
        # streamed assistant reply: one entry per content block, same message id
        entry("assistant", [{"type": "thinking", "thinking": "", "signature": "zzz"}], timestamp="2026-01-01T10:00:01Z", **c),
        {**entry("assistant", [{"type": "text", "text": "Смотрю файл."}], timestamp="2026-01-01T10:00:02Z", **c),
         "message": {"id": "m1", "model": "claude-opus-5", "content": [{"type": "text", "text": "Смотрю файл."}]}},
        entry("assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}], timestamp="2026-01-01T10:00:03Z", **c),
        entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 9000}], toolUseResult={"a": 1}, timestamp="2026-01-01T10:00:04Z", **c),
        entry("assistant", [{"type": "text", "text": "Готово, исправил."}], timestamp="2026-01-01T10:00:05Z", **c),
        entry("user", "<local-command-caveat>ignore me</local-command-caveat>", isMeta=True, **c),
        entry("user", "<command-name>/compact</command-name><command-message>compact</command-message><command-args>keep tests</command-args>", timestamp="2026-01-01T10:01:00Z", **c),
        entry("assistant", [{"type": "text", "text": "побочная цепочка"}], isSidechain=True, **c),
        entry("user", "<system-reminder>noise</system-reminder>А теперь тесты", timestamp="2026-01-01T10:02:00Z", **c),
        entry("assistant", [{"type": "text", "text": "Пишу тесты"}], isAbortedMidStream=True, timestamp="2026-01-01T10:02:05Z", **c),
        {"type": "ai-title", "aiTitle": "Правка парсера", "sessionId": "S"},
    ]


def test_encode_cwd_matches_claudes_folder_names():
    assert importer.encode_cwd("/Users/me/go/src/aero-bot") == "-Users-me-go-src-aero-bot"
    assert importer.encode_cwd("/a/b.c/d") == "-a-b-c-d"


def test_parse_folds_events_into_turns(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    path = write_session(tmp_path / "claude", cwd, conversation(cwd))
    parsed = importer.parse_session(path)

    assert parsed.title == "Правка парсера" and parsed.cwd == str(cwd) and parsed.branch == "main"
    roles = [m.role for m in parsed.messages]
    assert roles == ["user", "assistant", "user", "user", "assistant"]  # sidechain, meta, bookkeeping dropped
    assert parsed.messages[0].parts[0]["text"] == "Почини баг в парсере"

    turn = parsed.messages[1]
    assert [p["type"] for p in turn.parts] == ["text", "tool", "text"]  # empty thinking skipped, one turn
    assert turn.model == "claude-opus-5"
    tool = turn.parts[1]
    assert tool["name"] == "Read" and tool["status"] == "done" and len(tool["output"]) < 4200  # result attached, clipped

    assert parsed.messages[2].parts[0]["text"] == "/compact keep tests"  # slash command, not XML soup
    assert parsed.messages[3].parts[0]["text"] == "А теперь тесты"  # wrappers stripped
    assert parsed.messages[4].status == "interrupted"
    assert parsed.first_at == "2026-01-01T10:00:00Z" and parsed.last_at == "2026-01-01T10:02:05Z"


def test_list_sessions_reads_only_metadata_and_prefers_custom_title(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    lines = conversation(cwd) + [{"type": "custom-title", "customTitle": "Моё название", "sessionId": "S"}]
    path = write_session(tmp_path / "claude", cwd, lines)
    (path.parent / "broken.jsonl").write_text("not json at all\n")

    sessions = {s.id: s for s in importer.list_sessions(tmp_path / "claude")}
    info = sessions["11111111-aaaa-bbbb-cccc-222222222222"]
    assert info.title == "Моё название" and info.cwd == str(cwd) and info.first_prompt == "Почини баг в парсере"
    assert sessions["broken"].title == "broken"  # unreadable files don't break the listing


def test_launch_cwd_is_the_directory_the_project_folder_encodes(tmp_path):
    cwd = tmp_path / "proj"
    sub = cwd / "subdir"
    sub.mkdir(parents=True)
    lines = [entry("user", "hi", cwd=str(sub), timestamp="t"), entry("user", "again", cwd=str(cwd), timestamp="t")]
    path = write_session(tmp_path / "claude", cwd, lines)  # folder name encodes `cwd`, not `sub`
    assert importer.parse_session(path).cwd == str(cwd)


# -- end to end through the orchestrator ------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    root = tmp_path / "claude"
    monkeypatch.setattr(importer, "CLAUDE_ROOT", root)
    # pytest's own tmp dirs live under /var/folders, which production code treats as "temporary"
    monkeypatch.setattr(runner_module, "TEMP_PREFIXES", ())
    config = DesktopConfig(tmp_path / "data", tmp_path / "wt", tmp_path, [], "acceptEdits", "workspace-write", 60)
    config.data_dir.mkdir()
    db, store = Database(config.db_path), Store(config.db_path)
    return Orchestrator(config, db, store, Hub()), store, db, repo, root


async def test_import_creates_a_resumable_direct_chat(env):
    orch, store, db, repo, root = env
    path = write_session(root, repo, conversation(repo))

    listed = {s["id"]: s for s in orch.claude_sessions()}
    sid = path.stem
    assert listed[sid]["importable"] and listed[sid]["title"] == "Правка парсера"

    results = await orch.import_claude([sid])
    assert results == [{"id": sid, "ok": True, "chat_id": results[0]["chat_id"]}]

    chat = store.get_chat(results[0]["chat_id"])
    assert chat.mode == "direct" and chat.worktree_path == str(repo) and chat.agent == "claude"
    assert chat.title == "Правка парсера" and chat.branch == "main"
    assert db.get_project(chat.project_id).path == str(repo.resolve())  # the repo was registered as a project

    messages, _ = store.list_messages(chat.id)
    assert [m.role for m in messages] == ["user", "assistant", "user", "user", "assistant"]
    assert messages[1].agent == "claude" and messages[1].model == "claude-opus-5"
    assert messages[0].created_at == "2026-01-01T10:00:00Z"  # original dates survive
    # the native session is wired up, so the next claude turn is a real --resume with nothing re-told
    assert chat.sessions["claude"] == {"id": sid, "seen": messages[-1].id}

    assert orch.claude_sessions()[0]["imported"] is True
    again = await orch.import_claude([sid])
    assert again[0]["ok"] is False and "уже" in again[0]["error"]
    assert len(store.list_chats()) == 1  # no duplicate


async def test_import_reuses_a_registered_project_accepts_plain_folders_and_reports_unusable_sessions(env, tmp_path):
    orch, store, db, repo, root = env
    registered = orch.add_project("mine", str(repo))
    ok = write_session(root, repo, conversation(repo), "aaaaaaaa-0000-0000-0000-000000000001")

    plain = tmp_path / "not-git"
    plain.mkdir()
    not_git = write_session(root, plain, conversation(plain), "aaaaaaaa-0000-0000-0000-000000000002")

    gone = tmp_path / "deleted"
    gone.mkdir()
    gone_path = write_session(root, gone, conversation(gone), "aaaaaaaa-0000-0000-0000-000000000003")
    gone.rmdir()  # the working directory no longer exists

    listed = {s["id"]: s for s in orch.claude_sessions()}
    assert listed[gone_path.stem]["importable"] is False and "не существует" in listed[gone_path.stem]["reason"]

    results = {r["id"]: r for r in await orch.import_claude([ok.stem, not_git.stem, gone_path.stem, "missing"])}
    assert results[ok.stem]["ok"] and store.get_chat(results[ok.stem]["chat_id"]).project_id == registered.id
    # a folder that is not a repository still imports: the chat works in place, just without git tools
    assert results[not_git.stem]["ok"]
    plain_chat = store.get_chat(results[not_git.stem]["chat_id"])
    assert plain_chat.branch == "" and plain_chat.worktree_path == str(plain) and plain_chat.mode == "direct"
    assert db.get_project(plain_chat.project_id).path == str(plain)
    assert results[gone_path.stem]["ok"] is False
    assert results["missing"]["error"] == "Сессия не найдена."
    assert len(db.list_projects()) == 2  # the repo and the plain folder; nothing registered for the failures


async def test_messages_are_paged_for_big_histories(env):
    orch, store, db, repo, root = env
    project = orch.add_project("p", str(repo))
    chat = store.create_chat(project.id, "big", "direct", "main", str(repo), "claude")
    ids = [store.add_message(chat.id, "user", [{"type": "text", "text": f"m{i}"}]).id for i in range(25)]

    newest = orch.get_chat_with_messages(chat.id, limit=10)
    assert [m["id"] for m in newest["messages"]] == ids[-10:] and newest["has_more"] is True
    older = orch.get_chat_with_messages(chat.id, limit=10, before=ids[-10])
    assert [m["id"] for m in older["messages"]] == ids[-20:-10] and older["has_more"] is True
    oldest = orch.get_chat_with_messages(chat.id, limit=10, before=ids[-20])
    assert [m["id"] for m in oldest["messages"]] == ids[:5] and oldest["has_more"] is False
    assert len(orch.get_chat_with_messages(chat.id)["messages"]) == 25  # no limit: everything, as before


async def test_sessions_from_temporary_folders_are_not_offered(env, tmp_path, monkeypatch):
    orch, store, db, repo, root = env
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    path = write_session(root, scratch, conversation(scratch), "bbbbbbbb-0000-0000-0000-000000000001")
    assert {s["id"]: s for s in orch.claude_sessions()}[path.stem]["importable"] is True

    monkeypatch.setattr(runner_module, "TEMP_PREFIXES", (str(tmp_path) + "/",))
    entry_ = {s["id"]: s for s in orch.claude_sessions()}[path.stem]
    assert entry_["importable"] is False and "временная" in entry_["reason"]


async def test_listing_says_which_chat_an_imported_session_became(env):
    orch, store, db, repo, root = env
    path = write_session(root, repo, conversation(repo))
    before = {s["id"]: s for s in orch.claude_sessions()}[path.stem]
    assert before["chat_id"] is None and before["imported"] is False
    chat_id = (await orch.import_claude([path.stem]))[0]["chat_id"]
    after = {s["id"]: s for s in orch.claude_sessions()}[path.stem]
    assert after["imported"] is True and after["chat_id"] == chat_id  # the UI opens this chat on a tap


async def test_other_folders_inside_a_chats_folder_are_found(env, tmp_path):
    orch, store, db, repo, root = env
    outer = orch.add_project("outer", str(repo))
    inner_dir = repo / "inner"
    inner_dir.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=inner_dir, check=True)
    inner = orch.add_project("inner", str(inner_dir))
    chat = store.create_chat(outer.id, "outer chat", "direct", "main", str(repo), "claude")
    store.create_chat(inner.id, "inner chat", "direct", "main", str(inner_dir), "claude")
    assert orch.other_folders_inside(chat.id) == [str(inner_dir.resolve())]
