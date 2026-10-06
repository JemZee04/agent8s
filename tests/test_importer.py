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
    assert chat.sessions["claude"] == {"id": sid, "seen": messages[-1].id, "offset": path.stat().st_size}

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


# -- bringing a chat up to date with its session file -----------------------------------------------


def append(path: Path, lines: list[dict]):
    with path.open("a") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def turn(cwd, prompt, answer, stamp, *, stop="end_turn"):
    c = {"cwd": str(cwd), "gitBranch": "main"}
    return [
        entry("user", prompt, timestamp=f"{stamp}:00Z", **c),
        {**entry("assistant", [{"type": "text", "text": answer}], timestamp=f"{stamp}:05Z", **c),
         "message": {"id": "m" + stamp, "model": "claude-opus-5", "stop_reason": stop, "content": [{"type": "text", "text": answer}]}},
    ]


@pytest.fixture
def synced(env, monkeypatch):
    """A chat imported from a session that ended with a finished turn."""
    import os
    orch, store, db, repo, root = env
    path = write_session(root, repo, turn(repo, "первый вопрос", "первый ответ", "2026-02-01T10:00"))
    os.utime(path, (1, 1))  # an old file: nothing in it can be "still being written"
    return orch, store, path, repo


async def import_it(orch, path):
    return (await orch.import_claude([path.stem]))[0]["chat_id"]


async def test_continuing_in_the_terminal_shows_up_after_a_refresh(synced):
    orch, store, path, repo = synced
    chat_id = await import_it(orch, path)
    assert orch.get_chat(chat_id)["claude_stale"] is False

    append(path, turn(repo, "вопрос из терминала", "ответ из терминала", "2026-02-01T11:00"))
    assert orch.get_chat(chat_id)["claude_stale"] is True  # the button can say "there is news"

    assert await orch.sync_claude(chat_id) == {"added": 2, "pending": False}
    texts = [m.parts[0]["text"] for m in store.list_messages(chat_id)[0]]
    assert texts == ["первый вопрос", "первый ответ", "вопрос из терминала", "ответ из терминала"]
    assert orch.get_chat(chat_id)["claude_stale"] is False
    assert await orch.sync_claude(chat_id) == {"added": 0, "pending": False}  # nothing twice
    assert len(store.list_messages(chat_id)[0]) == 4


async def test_a_turn_still_being_written_arrives_whole_later(synced):
    orch, store, path, repo = synced
    chat_id = await import_it(orch, path)
    append(path, turn(repo, "длинная задача", "промежуточный шаг", "2026-02-01T12:00", stop="tool_use"))  # not finished
    # the file is fresh, so the turn is taken as running: leave it for later instead of cutting it in two
    assert await orch.sync_claude(chat_id) == {"added": 0, "pending": True}
    append(path, [turn(repo, "x", "готово", "2026-02-01T12:09")[1]])  # the final answer lands
    assert await orch.sync_claude(chat_id) == {"added": 2, "pending": False}
    messages = store.list_messages(chat_id)[0]
    assert messages[-2].parts[0]["text"] == "длинная задача"
    assert [p["text"] for p in messages[-1].parts if p["type"] == "text"] == ["промежуточный шаг\n\nготово"]


async def test_a_session_that_was_merely_interrupted_is_not_held_back_forever(synced):
    import os
    orch, store, path, repo = synced
    chat_id = await import_it(orch, path)
    append(path, turn(repo, "оборвалось", "половина ответа", "2026-02-01T13:00", stop=None))
    os.utime(path, (1, 1))  # untouched for ages: nobody is writing it any more
    assert await orch.sync_claude(chat_id) == {"added": 2, "pending": False}


async def test_a_turn_made_in_the_app_is_not_imported_back_as_a_duplicate(synced):
    orch, store, path, repo = synced
    chat_id = await import_it(orch, path)

    class FileWritingDriver:  # like claude --resume: answers, and the session file grows
        async def run(self, req):
            append(path, turn(repo, "из приложения", "ответ приложению", "2026-02-01T14:00"))
            yield {"type": "session", "id": path.stem}
            yield {"type": "text", "text": "ответ приложению"}
            yield {"type": "done", "ok": True, "error": None}

    orch._drivers = {"claude": FileWritingDriver()}
    await orch.send(chat_id, "из приложения")
    await orch._live[chat_id].task if chat_id in orch._live else None
    assert orch.get_chat(chat_id)["claude_stale"] is False  # the file grew, but we wrote that ourselves
    assert await orch.sync_claude(chat_id) == {"added": 0, "pending": False}
    users = [m.parts[0]["text"] for m in store.list_messages(chat_id)[0] if m.role == "user"]
    assert users == ["первый вопрос", "из приложения"]


async def test_terminal_work_is_pulled_in_before_the_next_turn_so_order_stays_right(synced):
    orch, store, path, repo = synced
    chat_id = await import_it(orch, path)
    append(path, turn(repo, "пока вы были в терминале", "терминальный ответ", "2026-02-01T15:00"))

    class QuietDriver:
        async def run(self, req):
            yield {"type": "session", "id": path.stem}
            yield {"type": "done", "ok": True, "error": None}

    orch._drivers = {"claude": QuietDriver()}
    await orch.send(chat_id, "а теперь из приложения")
    await orch._live[chat_id].task if chat_id in orch._live else None
    users = [m.parts[0]["text"] for m in store.list_messages(chat_id)[0] if m.role == "user"]
    assert users == ["первый вопрос", "пока вы были в терминале", "а теперь из приложения"]  # not the other way round


async def test_another_agents_turn_is_still_told_to_claude_after_a_refresh(synced):
    orch, store, path, repo = synced
    chat_id = await import_it(orch, path)
    codex_msg = store.add_message(chat_id, "user", [{"type": "text", "text": "вопрос codex"}])
    codex_answer = store.add_message(chat_id, "assistant", [{"type": "text", "text": "ответ codex"}], agent="codex")
    append(path, turn(repo, "из терминала", "ответ claude", "2026-02-01T16:00"))
    await orch.sync_claude(chat_id)
    # claude never saw the codex turn, so its `seen` marker must not jump over it
    assert store.get_chat(chat_id).sessions["claude"]["seen"] < codex_msg.id


async def test_legacy_chats_get_their_offset_and_an_app_turn_is_not_duplicated(env):
    import os
    orch, store, db, repo, root = env
    path = write_session(root, repo, turn(repo, "импортировано", "ответ", "2026-03-01T10:00"))
    os.utime(path, (1, 1))
    chat_id = await import_it(orch, path)
    # simulate a chat from before offsets existed, with one turn made in the app (stamped by us)
    session = store.get_chat(chat_id).sessions["claude"]
    store.set_session(chat_id, "claude", session["id"], seen=session["seen"])
    with store._connect() as conn:
        conn.execute("UPDATE chats SET sessions = ? WHERE id = ?", (json.dumps({"claude": {"id": session["id"], "seen": session["seen"]}}), chat_id))
    store.add_message(chat_id, "user", [{"type": "text", "text": "из приложения"}])
    store.add_message(chat_id, "assistant", [{"type": "text", "text": "ответ приложению"}], agent="claude")
    append(path, turn(repo, "из приложения", "ответ приложению", "2026-03-01T11:00"))  # what claude --resume wrote
    append(path, turn(repo, "потом в терминале", "терминальный ответ", "2026-03-01T12:00"))

    assert orch.calibrate_claude_offsets() == 0  # turns made here are in the file too: left for the careful path
    assert "offset" not in store.get_chat(chat_id).sessions["claude"]
    assert orch.get_chat(chat_id)["claude_stale"] is True  # so the button invites one refresh
    assert await orch.sync_claude(chat_id) == {"added": 2, "pending": False}  # only the terminal turn is new
    assert "offset" in store.get_chat(chat_id).sessions["claude"] and orch.get_chat(chat_id)["claude_stale"] is False
    users = [m.parts[0]["text"] for m in store.list_messages(chat_id)[0] if m.role == "user"]
    assert users == ["импортировано", "из приложения", "потом в терминале"]


async def test_a_legacy_chat_without_app_turns_is_calibrated_silently(env):
    import os
    orch, store, db, repo, root = env
    path = write_session(root, repo, turn(repo, "q", "a", "2026-03-02T10:00") + [{"type": "ai-title", "aiTitle": "t", "sessionId": "S"}])
    os.utime(path, (1, 1))
    chat_id = await import_it(orch, path)
    session = store.get_chat(chat_id).sessions["claude"]
    with store._connect() as conn:
        conn.execute("UPDATE chats SET sessions = ? WHERE id = ?", (json.dumps({"claude": {"id": session["id"], "seen": session["seen"]}}), chat_id))
    assert orch.calibrate_claude_offsets() == 1
    assert orch.get_chat(chat_id)["claude_stale"] is False  # trailing bookkeeping lines do not count as news
    assert await orch.sync_claude(chat_id) == {"added": 0, "pending": False}


async def test_refresh_refuses_chats_that_are_not_from_claude_or_are_busy(env):
    orch, store, db, repo, root = env
    project = orch.add_project("p", str(repo))
    plain = store.create_chat(project.id, "plain", "direct", "main", str(repo), "codex")
    with pytest.raises(UserError, match="не связан"):
        await orch.sync_claude(plain.id)
    assert orch.get_chat(plain.id)["claude_stale"] is False


async def test_a_long_final_answer_is_not_re_imported_for_a_legacy_chat(env):
    """Regression (found on real data): the last answer *starts* long before it ends; a timestamp
    boundary would cut it in two and add its tail as an extra message."""
    import os
    orch, store, db, repo, root = env
    c = {"cwd": str(repo), "gitBranch": "main"}
    lines = [
        entry("user", "долгая задача", timestamp="2026-04-01T10:00:00Z", **c),
        entry("assistant", [{"type": "text", "text": "начинаю"}], timestamp="2026-04-01T10:00:05Z", **c),
        entry("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make"}}], timestamp="2026-04-01T10:02:00Z", **c),
        entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}], timestamp="2026-04-01T10:09:00Z", **c),
        {**entry("assistant", [{"type": "text", "text": "готово"}], timestamp="2026-04-01T10:09:30Z", **c),
         "message": {"id": "m9", "stop_reason": "end_turn", "content": [{"type": "text", "text": "готово"}]}},
    ]
    path = write_session(root, repo, lines)
    os.utime(path, (1, 1))
    chat_id = await import_it(orch, path)
    session = store.get_chat(chat_id).sessions["claude"]
    with store._connect() as conn:  # as if imported by the version that recorded no offset
        conn.execute("UPDATE chats SET sessions = ? WHERE id = ?", (json.dumps({"claude": {"id": session["id"], "seen": session["seen"]}}), chat_id))
    before = len(store.list_messages(chat_id)[0])
    assert orch.calibrate_claude_offsets() == 1
    assert orch.get_chat(chat_id)["claude_stale"] is False
    assert await orch.sync_claude(chat_id) == {"added": 0, "pending": False}
    assert len(store.list_messages(chat_id)[0]) == before
