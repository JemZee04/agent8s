from __future__ import annotations

import re
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from .. import atlassian, git_ops, scaffold
from ..agents import AGENT_NAMES, build_agent
from ..config import Config
from ..db import Database, Task

router = Router()

TELEGRAM_TEXT_LIMIT = 4000  # leave headroom below Telegram's 4096 hard cap
PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
JIRA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


async def cmd_start(message: Message) -> None:
    await message.answer(
        "agent8s — headless coding agents over Telegram.\n\n"
        "/projects — list registered projects\n"
        "/add_project <name> <absolute_path> [branch] — register an existing repo\n"
        "/new <name> <description> — scaffold a brand-new repo and register it\n"
        "/use <name> — pick the active project for this chat\n"
        "/agents — list agents, /agent <name> — switch (claude/codex)\n"
        "/status — current project, agent, active task\n"
        "/diff — full diff of the active task, as a file\n"
        "/approve — commit + merge the active task into the default branch\n"
        "/drop — discard the active task and its worktree\n"
        "/context <ISSUE-KEY> — pull a Jira ticket's description into the chat\n"
        "/task <ISSUE-KEY> [instructions] — pull the ticket and start a task from it\n\n"
        "Any other text is either a new task (if none is active) or a "
        "follow-up to the active one."
    )


async def cmd_projects(message: Message, db: Database) -> None:
    projects = db.list_projects()
    if not projects:
        await message.answer("No projects registered yet. Use /add_project <name> <absolute_path> [branch].")
        return
    lines = [f"• {p.name} — {p.path} (default: {p.default_branch})" for p in projects]
    await message.answer("\n".join(lines))


async def cmd_add_project(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args:
        await message.answer("Usage: /add_project <name> <absolute_path> [branch]")
        return
    parts = command.args.split()
    if len(parts) < 2:
        await message.answer("Usage: /add_project <name> <absolute_path> [branch]")
        return
    name, raw_path = parts[0], parts[1]
    path = Path(raw_path).expanduser().resolve()

    if db.get_project_by_name(name) is not None:
        await message.answer(f"A project named '{name}' already exists.")
        return

    check = git_ops.check_repo(path)
    if not check.ok:
        await message.answer(f"Can't register {path}: {check.reason}")
        return

    branch = parts[2] if len(parts) > 2 else git_ops.default_branch(path)
    db.add_project(name, str(path), branch)
    await message.answer(f"Registered '{name}' → {path} (default branch: {branch})")


async def cmd_new(message: Message, command: CommandObject, db: Database, config: Config) -> None:
    if not command.args or " " not in command.args:
        await message.answer("Usage: /new <name> <description>")
        return
    name, description = command.args.split(" ", 1)
    name = name.strip()
    description = description.strip()

    if not PROJECT_NAME_RE.match(name):
        await message.answer("Project name must start with a letter/digit and contain only letters, digits, - or _.")
        return
    if db.get_project_by_name(name) is not None:
        await message.answer(f"A project named '{name}' already exists.")
        return

    path = config.projects_dir / name
    if path.exists():
        await message.answer(f"{path} already exists — pick another name or /add_project it directly.")
        return

    branch = "main"
    try:
        path.mkdir(parents=True)
        scaffold.write_skeleton(path, name, description)
        git_ops.init_repo(path, branch)
        git_ops.commit_all(path, "Initial scaffold")
    except (OSError, git_ops.GitError) as e:
        await message.answer(f"Failed to scaffold {path}: {e}")
        return

    project = db.add_project(name, str(path), branch)
    db.set_current_project(message.chat.id, project.id)
    await message.answer(
        f"Created and registered '{name}' → {path}\n"
        f"README.md + CLAUDE.md scaffolded, first commit made, set as active project.\n"
        f"Send a message to give it its first real task."
    )


async def cmd_use(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args:
        await message.answer("Usage: /use <project name>")
        return
    name = command.args.strip()
    project = db.get_project_by_name(name)
    if project is None:
        await message.answer(f"No such project: {name}. See /projects.")
        return
    db.set_current_project(message.chat.id, project.id)
    await message.answer(f"Active project: {project.name}")


async def cmd_agents(message: Message, db: Database) -> None:
    state = db.get_chat_state(message.chat.id)
    lines = [f"{'→ ' if a == state.current_agent else '  '}{a}" for a in AGENT_NAMES]
    await message.answer("\n".join(lines))


async def cmd_agent(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args or command.args.strip() not in AGENT_NAMES:
        await message.answer(f"Usage: /agent <{'|'.join(AGENT_NAMES)}>")
        return
    name = command.args.strip()
    db.set_current_agent(message.chat.id, name)
    await message.answer(f"Active agent: {name}")


async def cmd_status(message: Message, db: Database) -> None:
    state = db.get_chat_state(message.chat.id)
    project = db.get_project(state.current_project_id) if state.current_project_id else None
    lines = [
        f"Project: {project.name if project else '(none — use /use)'}",
        f"Agent: {state.current_agent}",
    ]
    if state.active_task_id:
        task = db.get_task(state.active_task_id)
        lines.append(f"Active task: #{task.id} on {task.branch} ({task.status}), agent={task.agent_name}")
    else:
        lines.append("Active task: none")
    await message.answer("\n".join(lines))


async def cmd_diff(message: Message, db: Database) -> None:
    task = await _require_active_task(message, db)
    if task is None:
        return
    diff = git_ops.diff_full(Path(task.worktree_path))
    if not diff.strip():
        await message.answer("No changes yet.")
        return
    await message.answer_document(
        BufferedInputFile(diff.encode(), filename=f"task-{task.id}.diff"),
        caption=f"task #{task.id} — {task.branch}",
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
        await message.answer(f"Merge failed, worktree kept for manual resolution:\n{e}")
        return

    git_ops.remove_worktree(project_path, worktree_path, task.branch)
    db.update_task_status(task.id, "approved")
    db.set_active_task(message.chat.id, None)
    await message.answer(f"Merged task #{task.id} into {project.default_branch} and cleaned up the worktree.")


async def cmd_drop(message: Message, db: Database) -> None:
    task = await _require_active_task(message, db)
    if task is None:
        return
    project = db.get_project(task.project_id)
    git_ops.remove_worktree(Path(project.path), Path(task.worktree_path), task.branch)
    db.update_task_status(task.id, "dropped")
    db.set_active_task(message.chat.id, None)
    await message.answer(f"Dropped task #{task.id} ({task.branch}). Worktree removed, changes discarded.")


async def cmd_context(message: Message, command: CommandObject, config: Config) -> None:
    if not config.jira_configured:
        await message.answer("Jira is not configured (JIRA_URL / JIRA_PERSONAL_TOKEN missing in .env).")
        return
    if not command.args:
        await message.answer("Usage: /context <ISSUE-KEY>")
        return
    key = command.args.strip().split()[0].upper()
    if not JIRA_KEY_RE.match(key):
        await message.answer(f"'{key}' doesn't look like a Jira issue key (e.g. PROJ-123).")
        return

    try:
        issue = atlassian.fetch_issue(config, key)
    except atlassian.AtlassianError as e:
        await message.answer(f"Could not fetch {key}: {e}")
        return

    await message.answer(_truncate(issue.as_context_text()))


async def cmd_task(message: Message, command: CommandObject, db: Database, config: Config) -> None:
    if not config.jira_configured:
        await message.answer("Jira is not configured (JIRA_URL / JIRA_PERSONAL_TOKEN missing in .env).")
        return
    if not command.args:
        await message.answer("Usage: /task <ISSUE-KEY> [extra instructions]")
        return

    parts = command.args.strip().split(maxsplit=1)
    key = parts[0].upper()
    extra = parts[1] if len(parts) > 1 else None
    if not JIRA_KEY_RE.match(key):
        await message.answer(f"'{key}' doesn't look like a Jira issue key (e.g. PROJ-123).")
        return

    state = db.get_chat_state(message.chat.id)
    if state.current_project_id is None:
        await message.answer("No active project. Pick one with /use <name> first (see /projects).")
        return
    if state.active_task_id is not None:
        await message.answer("A task is already active in this chat — /approve or /drop it first.")
        return
    project = db.get_project(state.current_project_id)

    try:
        issue = atlassian.fetch_issue(config, key)
    except atlassian.AtlassianError as e:
        await message.answer(f"Could not fetch {key}: {e}")
        return

    prompt = issue.as_context_text() + "\n\n---\nTask: " + (extra or "Implement what's described in the Jira ticket above.")
    await message.answer(f"Pulled {key} from Jira" + (f" + {len(issue.confluence_pages)} Confluence page(s)" if issue.confluence_pages else "") + ". Starting task...")

    await _run_new_task(message, db, config, project_id=project.id, project_path=project.path,
                         default_branch=project.default_branch, agent_name=state.current_agent, prompt=prompt)


async def handle_free_text(message: Message, db: Database, config: Config) -> None:
    if not message.text or message.text.startswith("/"):
        return

    state = db.get_chat_state(message.chat.id)
    if state.current_project_id is None:
        await message.answer("No active project. Pick one with /use <name> (see /projects).")
        return
    project = db.get_project(state.current_project_id)

    if state.active_task_id is not None:
        task = db.get_task(state.active_task_id)
        await _run_followup(message, db, config, task)
        return

    await _run_new_task(message, db, config, project_id=project.id, project_path=project.path,
                         default_branch=project.default_branch, agent_name=state.current_agent, prompt=message.text)


async def _run_new_task(
    message: Message, db: Database, config: Config, *, project_id: int, project_path: str,
    default_branch: str, agent_name: str, prompt: str,
) -> None:
    project_root = Path(project_path)
    check = git_ops.check_repo(project_root)
    if not check.ok:
        await message.answer(f"Project repo looks broken: {check.reason}")
        return

    task = db.create_task(project_id, message.chat.id, agent_name, branch="", worktree_path="", prompt=prompt)
    branch = f"agent8s/task-{task.id}"
    worktree_path = config.worktree_dir / Path(project_path).name / f"task-{task.id}"
    db.set_task_branch_and_worktree(task.id, branch, str(worktree_path))
    task.branch = branch
    task.worktree_path = str(worktree_path)

    await message.answer(f"⏳ {agent_name} is starting task #{task.id} on a new branch ({branch})...")

    try:
        git_ops.create_worktree(project_root, branch, worktree_path, default_branch)
    except git_ops.GitError as e:
        db.update_task_status(task.id, "failed")
        await message.answer(f"Could not create worktree: {e}")
        return

    db.set_active_task(message.chat.id, task.id)

    agent = build_agent(agent_name, config)
    result = await agent.start(prompt, worktree_path)
    await _finish_agent_turn(message, db, task, worktree_path, result)


async def _run_followup(message: Message, db: Database, config: Config, task: Task) -> None:
    if not task.session_id:
        await message.answer("Active task has no session yet — try again in a moment, or /drop it.")
        return
    worktree_path = Path(task.worktree_path)
    await message.answer(f"⏳ {task.agent_name} is continuing task #{task.id}...")

    agent = build_agent(task.agent_name, config)
    result = await agent.resume(task.session_id, message.text, worktree_path)
    await _finish_agent_turn(message, db, task, worktree_path, result)


async def _finish_agent_turn(message: Message, db: Database, task: Task, worktree_path: Path, result) -> None:
    if result.session_id:
        db.update_task_session(task.id, result.session_id)

    if not result.success:
        db.update_task_status(task.id, "failed")
        await message.answer(f"❌ task #{task.id} failed:\n{_truncate(result.summary or 'no output')}")
        return

    db.update_task_status(task.id, "active")

    stat = git_ops.diff_stat(worktree_path) or "(no file changes)"
    reply = f"✅ task #{task.id}\n\n{_truncate(result.summary)}\n\n{_truncate(stat)}"
    await message.answer(reply)


async def _require_active_task(message: Message, db: Database) -> Task | None:
    state = db.get_chat_state(message.chat.id)
    if state.active_task_id is None:
        await message.answer("No active task for this chat.")
        return None
    return db.get_task(state.active_task_id)


def _truncate(text: str) -> str:
    if len(text) <= TELEGRAM_TEXT_LIMIT:
        return text
    return text[:TELEGRAM_TEXT_LIMIT] + "\n… (truncated, see /diff)"


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
    router.message.register(cmd_context, Command("context"))
    router.message.register(cmd_task, Command("task"))
    router.message.register(handle_free_text)
    return router
