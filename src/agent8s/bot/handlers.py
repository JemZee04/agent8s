from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BufferedInputFile, Message

from .. import atlassian, calendar_client, git_ops, launchagent, scaffold, selfrepo
from ..agents import AGENT_NAMES, build_agent
from ..agents.base import AgentResult
from ..config import Config
from ..db import Database, Project, Task
from .progress import ProgressReporter

router = Router()

TELEGRAM_TEXT_LIMIT = 4000  # запас ниже жёсткого лимита Telegram в 4096
PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

# Показывается в меню команд Telegram (кнопка "/" у поля ввода).
BOT_COMMANDS: list[tuple[str, str]] = [
    ("help", "Список команд и инструкция"),
    ("projects", "Список проектов"),
    ("use", "Выбрать активный проект"),
    ("new", "Создать новый проект"),
    ("add_project", "Зарегистрировать существующий репозиторий"),
    ("agent", "Переключить агента (claude/codex)"),
    ("agents", "Список доступных агентов"),
    ("status", "Текущий проект, агент, активная задача"),
    ("ask", "Вопрос без создания задачи (только чтение)"),
    ("diff", "Diff активной задачи файлом"),
    ("approve", "Смержить активную задачу"),
    ("drop", "Отменить активную задачу"),
    ("continue", "Продолжить отложенную/прерванную задачу"),
    ("context", "Подтянуть тикет Jira в чат"),
    ("task", "Начать задачу по тикету Jira"),
    ("today", "События на сегодня из Яндекс.Календаря"),
    ("diagnose", "Диагностировать и починить сам бот"),
    ("restart", "Перезапустить бота (применить смерженный фикс)"),
    ("autostart", "Автозапуск бота: on|off|status"),
]


def get_bot_commands() -> list[BotCommand]:
    return [BotCommand(command=name, description=description) for name, description in BOT_COMMANDS]
JIRA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


async def cmd_start(message: Message) -> None:
    await message.answer(
        "agent8s — headless coding-агенты через Telegram.\n\n"
        "Проекты:\n"
        "/projects — список зарегистрированных проектов\n"
        "/add_project <имя> <абс.путь> [ветка] — зарегистрировать существующий репозиторий\n"
        "/new <имя> <описание> — создать новый репозиторий с нуля и зарегистрировать\n"
        "/use <имя> — выбрать активный проект для этого чата\n"
        "/agents — список агентов, /agent <имя> — переключить (claude/codex)\n\n"
        "Задачи:\n"
        "любой текст — новая задача (если нет активной) или уточнение к активной\n"
        "/ask <вопрос> — спросить/поручить без создания задачи (только чтение, файлы не трогает)\n"
        "/status — текущий проект, агент, активная задача\n"
        "/diff — полный diff активной задачи, файлом\n"
        "/approve — закоммитить и смержить активную задачу в основную ветку\n"
        "/drop — отменить активную задачу и её worktree\n"
        "/continue — вернуться к отложенной или прерванной задаче\n\n"
        "Jira / Confluence:\n"
        "/context <КЛЮЧ> — подтянуть описание тикета в чат\n"
        "/task <КЛЮЧ> [инструкции] — подтянуть тикет и начать по нему задачу\n\n"
        "Календарь:\n"
        "/today — сегодняшние события из Яндекс.Календаря\n\n"
        "Сам бот:\n"
        "/diagnose [описание проблемы] — диагностировать и починить сам бот (отдельно от текущей задачи)\n"
        "/restart — перезапустить бот, чтобы применились смерженные изменения в его коде\n"
        "/autostart on|off|status — автозапуск бота при логине и автоперезапуск при краше\n"
        "/help — эта инструкция"
    )


async def cmd_projects(message: Message, db: Database) -> None:
    projects = db.list_projects()
    if not projects:
        await message.answer("Проектов пока нет. Используй /add_project <имя> <абс.путь> [ветка].")
        return
    lines = [f"• {p.name} — {p.path} (основная ветка: {p.default_branch})" for p in projects]
    await message.answer("\n".join(lines))


async def cmd_add_project(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args:
        await message.answer("Использование: /add_project <имя> <абс.путь> [ветка]")
        return
    parts = command.args.split()
    if len(parts) < 2:
        await message.answer("Использование: /add_project <имя> <абс.путь> [ветка]")
        return
    name, raw_path = parts[0], parts[1]
    path = Path(raw_path).expanduser().resolve()

    if db.get_project_by_name(name) is not None:
        await message.answer(f"Проект с именем '{name}' уже существует.")
        return

    check = git_ops.check_repo(path)
    if not check.ok:
        await message.answer(f"Не могу зарегистрировать {path}: {check.reason}")
        return

    branch = parts[2] if len(parts) > 2 else git_ops.default_branch(path)
    db.add_project(name, str(path), branch)
    await message.answer(f"Зарегистрирован '{name}' → {path} (основная ветка: {branch})")


async def cmd_new(message: Message, command: CommandObject, db: Database, config: Config) -> None:
    if not command.args or " " not in command.args:
        await message.answer("Использование: /new <имя> <описание>")
        return
    name, description = command.args.split(" ", 1)
    name = name.strip()
    description = description.strip()

    if not PROJECT_NAME_RE.match(name):
        await message.answer("Имя проекта должно начинаться с буквы/цифры и содержать только буквы, цифры, - или _.")
        return
    if db.get_project_by_name(name) is not None:
        await message.answer(f"Проект с именем '{name}' уже существует.")
        return

    path = config.projects_dir / name
    if path.exists():
        await message.answer(f"{path} уже существует — выбери другое имя или зарегистрируй через /add_project.")
        return

    branch = "main"
    try:
        path.mkdir(parents=True)
        scaffold.write_skeleton(path, name, description)
        git_ops.init_repo(path, branch)
        git_ops.commit_all(path, "Initial scaffold")
    except (OSError, git_ops.GitError) as e:
        await message.answer(f"Не удалось создать {path}: {e}")
        return

    project = db.add_project(name, str(path), branch)
    db.set_current_project(message.chat.id, project.id)
    await message.answer(
        f"Создал и зарегистрировал '{name}' → {path}\n"
        f"README.md + CLAUDE.md готовы, первый коммит сделан, проект выбран активным.\n"
        f"Напиши, что сделать в первую очередь."
    )


async def cmd_use(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args:
        await message.answer("Использование: /use <имя проекта>")
        return
    name = command.args.strip()
    project = db.get_project_by_name(name)
    if project is None:
        await message.answer(f"Нет такого проекта: {name}. Смотри /projects.")
        return
    db.set_current_project(message.chat.id, project.id)
    await message.answer(f"Активный проект: {project.name}")


async def cmd_agents(message: Message, db: Database) -> None:
    state = db.get_chat_state(message.chat.id)
    lines = [f"{'→ ' if a == state.current_agent else '  '}{a}" for a in AGENT_NAMES]
    await message.answer("\n".join(lines))


async def cmd_agent(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args or command.args.strip() not in AGENT_NAMES:
        await message.answer(f"Использование: /agent <{'|'.join(AGENT_NAMES)}>")
        return
    name = command.args.strip()
    db.set_current_agent(message.chat.id, name)
    await message.answer(f"Активный агент: {name}")


async def cmd_status(message: Message, db: Database) -> None:
    state = db.get_chat_state(message.chat.id)
    project = db.get_project(state.current_project_id) if state.current_project_id else None
    lines = [
        f"Проект: {project.name if project else '(нет — используй /use)'}",
        f"Агент: {state.current_agent}",
    ]
    if state.active_task_id:
        task = db.get_task(state.active_task_id)
        lines.append(f"Активная задача: #{task.id} на {task.branch} ({task.status}), агент={task.agent_name}")
    else:
        lines.append("Активная задача: нет")
    if state.parked_task_id:
        parked = db.get_task(state.parked_task_id)
        lines.append(f"Отложена (см. /continue): #{parked.id} на {parked.branch}")
    await message.answer("\n".join(lines))


async def cmd_diff(message: Message, db: Database) -> None:
    task = await _require_active_task(message, db)
    if task is None:
        return
    diff = git_ops.diff_full(Path(task.worktree_path))
    if not diff.strip():
        await message.answer("Изменений пока нет.")
        return
    await message.answer_document(
        BufferedInputFile(diff.encode(), filename=f"task-{task.id}.diff"),
        caption=f"задача #{task.id} — {task.branch}",
    )


async def cmd_approve(message: Message, db: Database) -> None:
    task = await _require_active_task(message, db)
    if task is None:
        return
    project = db.get_project(task.project_id)
    worktree_path = Path(task.worktree_path)
    project_path = Path(project.path)

    if git_ops.has_changes(worktree_path):
        git_ops.commit_all(worktree_path, f"agent8s: task #{task.id} ({task.agent_name})\n\n{task.prompt}")

    try:
        git_ops.merge_branch(project_path, task.branch, f"Merge agent8s task #{task.id}: {task.prompt[:72]}")
    except git_ops.GitError as e:
        await message.answer(f"Merge не удался, worktree оставлен для ручного разбора:\n{e}")
        return

    git_ops.remove_worktree(project_path, worktree_path, task.branch)
    db.update_task_status(task.id, "approved")
    db.set_active_task(message.chat.id, None)

    note = ""
    if project.name == selfrepo.SELF_PROJECT_NAME:
        note = "\n\nЭто был фикс самого бота — код в main пока тот же, что и в работающем процессе. Запусти /restart, когда будешь готов применить изменения."
    await message.answer(f"Смержил задачу #{task.id} в {project.default_branch} и убрал worktree.{note}")


async def cmd_drop(message: Message, db: Database) -> None:
    task = await _require_active_task(message, db)
    if task is None:
        return
    project = db.get_project(task.project_id)
    git_ops.remove_worktree(Path(project.path), Path(task.worktree_path), task.branch)
    db.update_task_status(task.id, "dropped")
    db.set_active_task(message.chat.id, None)
    await message.answer(f"Задача #{task.id} ({task.branch}) отменена. Worktree удалён, изменения потеряны.")


async def cmd_continue(message: Message, db: Database) -> None:
    state = db.get_chat_state(message.chat.id)
    if state.active_task_id is not None:
        await message.answer("В этом чате уже есть активная задача — сначала /approve или /drop её.")
        return

    task: Task | None = None
    if state.parked_task_id is not None:
        task = db.get_task(state.parked_task_id)
        db.set_parked_task(message.chat.id, None)
    else:
        task = db.find_resumable_task(message.chat.id)

    if task is None:
        await message.answer("Нечего продолжать — нет отложенных или прерванных задач.")
        return

    if not Path(task.worktree_path).exists():
        await message.answer(
            f"Задача #{task.id} была бы кандидатом на продолжение, но её worktree больше не существует на диске."
        )
        return

    project = db.get_project(task.project_id)
    if project.id != state.current_project_id:
        db.set_current_project(message.chat.id, project.id)
    db.set_active_task(message.chat.id, task.id)
    db.update_task_status(task.id, "active")

    stat = git_ops.diff_stat(Path(task.worktree_path)) or "(изменений пока нет)"
    await message.answer(
        f"▶️ Возвращаюсь к задаче #{task.id} ({project.name}, {task.branch})\n\n"
        f"{_truncate(stat)}\n\nСледующее сообщение продолжит её."
    )


async def cmd_context(message: Message, command: CommandObject, config: Config) -> None:
    if not config.jira_configured:
        await message.answer("Jira не настроена (нет JIRA_URL / JIRA_PERSONAL_TOKEN в .env).")
        return
    if not command.args:
        await message.answer("Использование: /context <КЛЮЧ-ТИКЕТА>")
        return
    key = command.args.strip().split()[0].upper()
    if not JIRA_KEY_RE.match(key):
        await message.answer(f"'{key}' не похоже на ключ тикета Jira (например, PROJ-123).")
        return

    try:
        issue = atlassian.fetch_issue(config, key)
    except atlassian.AtlassianError as e:
        await message.answer(f"Не удалось получить {key}: {e}")
        return

    await message.answer(_truncate(issue.as_context_text()))


async def cmd_task(message: Message, command: CommandObject, db: Database, config: Config) -> None:
    if not config.jira_configured:
        await message.answer("Jira не настроена (нет JIRA_URL / JIRA_PERSONAL_TOKEN в .env).")
        return
    if not command.args:
        await message.answer("Использование: /task <КЛЮЧ-ТИКЕТА> [доп. инструкции]")
        return

    parts = command.args.strip().split(maxsplit=1)
    key = parts[0].upper()
    extra = parts[1] if len(parts) > 1 else None
    if not JIRA_KEY_RE.match(key):
        await message.answer(f"'{key}' не похоже на ключ тикета Jira (например, PROJ-123).")
        return

    state = db.get_chat_state(message.chat.id)
    if state.current_project_id is None:
        await message.answer("Нет активного проекта. Сначала /use <имя> (см. /projects).")
        return
    if state.active_task_id is not None:
        await message.answer("В этом чате уже есть активная задача — сначала /approve или /drop её.")
        return
    project = db.get_project(state.current_project_id)

    try:
        issue = atlassian.fetch_issue(config, key)
    except atlassian.AtlassianError as e:
        await message.answer(f"Не удалось получить {key}: {e}")
        return

    prompt = issue.as_context_text() + "\n\n---\nЗадача: " + (extra or "Реализуй то, что описано в тикете Jira выше.")
    pages_note = f" + {len(issue.confluence_pages)} стр. Confluence" if issue.confluence_pages else ""
    await message.answer(f"Подтянул {key} из Jira{pages_note}. Начинаю задачу...")

    await _run_new_task(message, db, config, project_id=project.id, project_path=project.path,
                         default_branch=project.default_branch, agent_name=state.current_agent, prompt=prompt)


async def cmd_today(message: Message, config: Config) -> None:
    if not config.caldav_configured:
        await message.answer("Яндекс.Календарь не настроен (нет YANDEX_CALDAV_URL / _LOGIN / _PASSWORD в .env).")
        return

    local_start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)

    try:
        events = await asyncio.to_thread(calendar_client.fetch_events, config, local_start, local_end)
    except calendar_client.CalendarError as e:
        await message.answer(f"Не удалось получить календарь: {e}")
        return

    if not events:
        await message.answer("Сегодня событий нет.")
        return

    lines = []
    for event in events:
        line = f"{event.start.astimezone().strftime('%H:%M')} — {event.summary}"
        if event.location:
            line += f" ({event.location})"
        lines.append(line)
    await message.answer(_truncate("\n".join(lines)))


async def cmd_ask(message: Message, command: CommandObject, db: Database, config: Config) -> None:
    if not command.args:
        await message.answer("Использование: /ask <вопрос или поручение>")
        return
    state = db.get_chat_state(message.chat.id)
    if state.current_project_id is None:
        await message.answer("Нет активного проекта. Сначала /use <имя> (см. /projects).")
        return
    project = db.get_project(state.current_project_id)

    reporter = ProgressReporter(message, f"🔎 {state.current_agent} разбирается (только чтение, файлы не меняет)...")
    await reporter.start()

    agent = build_agent(state.current_agent, config, readonly=True)
    result = await _call_agent(agent.start, command.args, Path(project.path), on_progress=reporter.update)

    if not result.success:
        await reporter.finish(f"❌ Не получилось:\n{_truncate(result.summary or 'нет ответа')}")
        return
    await reporter.finish(_truncate(result.summary or "(пустой ответ)"))


async def cmd_diagnose(message: Message, command: CommandObject, db: Database, config: Config) -> None:
    self_project = _ensure_self_project(db, config)
    state = db.get_chat_state(message.chat.id)

    if state.active_task_id is not None:
        active_task = db.get_task(state.active_task_id)
        if active_task.project_id == self_project.id and active_task.status in ("running", "active"):
            if command.args:
                await _run_followup(message, db, config, active_task, prompt=command.args)
            else:
                await message.answer(f"Уже чиню себя в задаче #{active_task.id} — просто напиши, что уточнить.")
            return
        db.set_parked_task(message.chat.id, state.active_task_id)
        db.set_active_task(message.chat.id, None)
        parked_note = f" Текущая задача #{active_task.id} отложена — верни её через /continue."
    else:
        parked_note = ""

    log_tail = selfrepo.tail_log(config.data_dir)
    symptom = command.args or "бот завис или повёл себя неожиданно — деталей не указано, разберись по логам"
    prompt = (
        "Ты чинишь код своего собственного проекта agent8s — Telegram-бота, которым сейчас управляешь.\n\n"
        f"Симптом от пользователя: {symptom}\n\n"
        f"Последние строки лог-файла бота (data/bot.log):\n```\n{log_tail}\n```\n\n"
        "Разберись в первопричине через код и git-историю (git log, git blame), исправь. "
        "Изменение смержится через /approve, а применится через отдельную команду /restart — "
        "сам ничего не мержи и не перезапускай, просто внеси и объясни исправление."
    )
    await message.answer(f"🔧 Начинаю диагностику agent8s.{parked_note}")
    await _run_new_task(message, db, config, project_id=self_project.id, project_path=self_project.path,
                         default_branch=self_project.default_branch, agent_name=state.current_agent, prompt=prompt)


async def cmd_restart(message: Message) -> None:
    await message.answer("🔄 Перезапускаюсь, чтобы применить изменения в коде бота...")
    await asyncio.sleep(0.5)
    python = sys.executable
    os.execv(python, [python, "-m", "agent8s.bot.main"])


async def cmd_autostart(message: Message, command: CommandObject) -> None:
    action = (command.args or "").strip().lower()
    if action not in ("on", "off", "status"):
        await message.answer(f"Использование: /autostart on|off|status\nСейчас: {launchagent.status()}")
        return

    if action == "status":
        await message.answer(f"Автозапуск: {launchagent.status()}")
        return

    if action == "on":
        try:
            path = launchagent.install()
        except launchagent.LaunchAgentError as e:
            await message.answer(f"Не удалось включить автозапуск: {e}")
            return
        await message.answer(
            f"Автозапуск включён ({path}). Бот теперь стартует при логине и перезапускается при краше.\n"
            "Сейчас выхожу, чтобы launchd поднял управляемый им процесс — секунд через 10 бот вернётся сам."
        )
        await asyncio.sleep(0.5)
        sys.exit(0)

    await message.answer("Выключаю автозапуск...")
    launchagent.uninstall()
    # если это был launchd-процесс, следующая строка может не успеть отправиться —
    # unload уже мог прислать сигнал завершения раньше


async def handle_free_text(message: Message, db: Database, config: Config) -> None:
    if not message.text or message.text.startswith("/"):
        return

    state = db.get_chat_state(message.chat.id)
    if state.current_project_id is None:
        await message.answer("Нет активного проекта. Сначала /use <имя> (см. /projects).")
        return
    project = db.get_project(state.current_project_id)

    if state.active_task_id is not None:
        task = db.get_task(state.active_task_id)
        await _run_followup(message, db, config, task)
        return

    await _run_new_task(message, db, config, project_id=project.id, project_path=project.path,
                         default_branch=project.default_branch, agent_name=state.current_agent, prompt=message.text)


def _ensure_self_project(db: Database, config: Config) -> Project:
    project = db.get_project_by_name(selfrepo.SELF_PROJECT_NAME)
    if project is not None:
        return project
    path = selfrepo.self_repo_path()
    branch = git_ops.default_branch(path)
    return db.add_project(selfrepo.SELF_PROJECT_NAME, str(path), branch)


async def _run_new_task(
    message: Message, db: Database, config: Config, *, project_id: int, project_path: str,
    default_branch: str, agent_name: str, prompt: str,
) -> None:
    project_root = Path(project_path)
    check = git_ops.check_repo(project_root)
    if not check.ok:
        await message.answer(f"С репозиторием проекта что-то не так: {check.reason}")
        return

    task = db.create_task(project_id, message.chat.id, agent_name, branch="", worktree_path="", prompt=prompt)
    branch = f"agent8s/task-{task.id}"
    worktree_path = config.worktree_dir / Path(project_path).name / f"task-{task.id}"
    db.set_task_branch_and_worktree(task.id, branch, str(worktree_path))
    task.branch = branch
    task.worktree_path = str(worktree_path)

    reporter = ProgressReporter(message, f"⏳ {agent_name} начинает задачу #{task.id} в новой ветке ({branch})...")
    await reporter.start()

    try:
        git_ops.create_worktree(project_root, branch, worktree_path, default_branch)
    except git_ops.GitError as e:
        db.update_task_status(task.id, "failed")
        await reporter.finish(f"Не удалось создать worktree: {e}")
        return

    db.set_active_task(message.chat.id, task.id)

    agent = build_agent(agent_name, config)
    result = await _call_agent(agent.start, prompt, worktree_path, on_progress=reporter.update)
    await _finish_agent_turn(reporter, db, task, worktree_path, result)


async def _run_followup(message: Message, db: Database, config: Config, task: Task, prompt: str | None = None) -> None:
    prompt = prompt if prompt is not None else message.text
    if not task.session_id:
        await message.answer("У активной задачи ещё нет сессии — попробуй чуть позже, либо /drop её.")
        return
    worktree_path = Path(task.worktree_path)
    reporter = ProgressReporter(message, f"⏳ {task.agent_name} продолжает задачу #{task.id}...")
    await reporter.start()

    agent = build_agent(task.agent_name, config)
    result = await _call_agent(
        agent.resume, task.session_id, prompt, worktree_path, on_progress=reporter.update
    )
    await _finish_agent_turn(reporter, db, task, worktree_path, result)


async def _call_agent(fn, *args, **kwargs):
    # Задача не должна зависать в "running" навсегда просто потому, что
    # что-то неожиданное упало посреди стрима (например, ошибка Telegram API
    # при отправке прогресса) — это блокирует чат без единого сообщения об
    # ошибке. Всё, что здесь падает, превращается в обычный неуспешный
    # результат вместо исключения, которое молча убивает корутину.
    try:
        return await fn(*args, **kwargs)
    except Exception as e:
        return AgentResult(success=False, session_id=None, summary=f"неожиданная ошибка: {e}", raw_output="")


async def _finish_agent_turn(reporter: ProgressReporter, db: Database, task: Task, worktree_path: Path, result) -> None:
    if result.session_id:
        db.update_task_session(task.id, result.session_id)

    if not result.success:
        db.update_task_status(task.id, "failed")
        if not result.session_id:
            db.set_active_task(task.chat_id, None)
        await reporter.finish(f"❌ задача #{task.id} завершилась с ошибкой:\n{_truncate(result.summary or 'нет вывода')}")
        return

    db.update_task_status(task.id, "active")

    stat = git_ops.diff_stat(worktree_path) or "(изменений в файлах нет)"
    reply = f"✅ задача #{task.id}\n\n{_truncate(result.summary)}\n\n{_truncate(stat)}"
    await reporter.finish(reply)


async def _require_active_task(message: Message, db: Database) -> Task | None:
    state = db.get_chat_state(message.chat.id)
    if state.active_task_id is None:
        await message.answer("В этом чате нет активной задачи.")
        return None
    return db.get_task(state.active_task_id)


def _truncate(text: str) -> str:
    if len(text) <= TELEGRAM_TEXT_LIMIT:
        return text
    return text[:TELEGRAM_TEXT_LIMIT] + "\n… (обрезано, см. /diff)"


def register_handlers() -> Router:
    router.message.register(cmd_start, Command("start", "help"))
    router.message.register(cmd_projects, Command("projects"))
    router.message.register(cmd_add_project, Command("add_project"))
    router.message.register(cmd_new, Command("new"))
    router.message.register(cmd_use, Command("use"))
    router.message.register(cmd_agents, Command("agents"))
    router.message.register(cmd_agent, Command("agent"))
    router.message.register(cmd_status, Command("status"))
    router.message.register(cmd_diff, Command("diff"))
    router.message.register(cmd_approve, Command("approve"))
    router.message.register(cmd_drop, Command("drop"))
    router.message.register(cmd_continue, Command("continue"))
    router.message.register(cmd_context, Command("context"))
    router.message.register(cmd_task, Command("task"))
    router.message.register(cmd_today, Command("today"))
    router.message.register(cmd_ask, Command("ask"))
    router.message.register(cmd_diagnose, Command("diagnose"))
    router.message.register(cmd_restart, Command("restart"))
    router.message.register(cmd_autostart, Command("autostart"))
    router.message.register(handle_free_text)
    return router
