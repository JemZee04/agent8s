# agent8s

Telegram bot that drives headless coding agents (Claude Code, Codex — more
pluggable via `src/agent8s/agents/`) against local git repos, one isolated
`git worktree` per task, with all state in SQLite and git instead of the
model's context.

```
Telegram ── aiogram bot ── SQLite (projects, tasks, session_id) ── git worktree ── claude -p / codex exec
```

This is the Этап 0+1 (MVP) slice: register projects, run tasks in worktrees,
inspect diffs, approve (merge) or drop them, and switch between agents. No
Jira/Confluence/calendar integration yet, no push/deploy — merges stay local.

## Setup

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you don't have uv yet
cp .env.example .env                              # then fill in the values below
uv sync
```

Required in `.env` (never commit this file — it holds a live bot token):

- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather) (`/newbot`)
- `ALLOWED_CHAT_IDS` — comma-separated Telegram numeric user IDs allowed to
  talk to the bot (get yours from [@userinfobot](https://t.me/userinfobot)).
  This is a hard whitelist checked before any handler runs — the bot refuses
  to start without it.

Everything else in `.env.example` has a working default.

The bot shells out to the `claude` and `codex` CLIs, so both need to already
be installed and authenticated on this machine (`claude` via its normal
login, `codex` via its own login) — the bot itself does no auth handling.

## Run

```bash
./scripts/run.sh
```

Runs in the foreground via long polling — no inbound ports, no server to
expose. Stop with Ctrl-C.

## Using it

```
/add_project quick-hop /Users/you/Documents/quick-hop
/use quick-hop
/agent claude              # or: codex
add a health check endpoint returning {"status": "ok"}
```

Free text with no active task starts a new one: creates
`agent8s/task-<id>` as a branch + worktree, and runs the selected agent's
headless mode (`claude -p ... --output-format json` /
`codex exec --json ...`) with the message as the prompt. The reply is the
agent's own summary plus `git diff --stat` — never the agent's self-report of
what it changed.

Free text while a task is active is a follow-up: it resumes the same agent
session (`--resume` / `codex exec resume`) in the same worktree, so
"actually, extract that into its own function" continues the conversation
instead of starting over.

- `/diff` — full `git diff` of the active task, sent as a file (Telegram's
  4096-char message limit makes anything non-trivial unreadable inline).
- `/approve` — commits any uncommitted changes in the worktree, merges the
  task branch into the project's default branch with `--no-ff`, removes the
  worktree. Local only — nothing is pushed anywhere.
- `/drop` — discards the task: removes the worktree and branch.
- `/status` — current project, agent, and active task for this chat.

## Adding another agent

Subclass `AgentRunner` in `src/agent8s/agents/` (see `claude_agent.py` /
`codex_agent.py` for the shape: `start(prompt, cwd)` and
`resume(session_id, prompt, cwd)`, both returning session id + summary text),
then add one line to `AGENT_NAMES` and `build_agent()` in
`src/agent8s/agents/registry.py`. Nothing in the bot or database layer needs
to change.

## Security notes

This machine executes shell commands — via whichever agent CLI you pick —
against your local repos, triggered by whoever can message the bot. Beyond
the `ALLOWED_CHAT_IDS` whitelist:

- `AGENT8S_CLAUDE_ALLOWED_TOOLS` / `AGENT8S_CLAUDE_PERMISSION_MODE` and
  `AGENT8S_CODEX_SANDBOX` in `.env` bound what the agent can do without a
  human in the loop to ask — headless mode has no one to prompt.
- `/approve` never pushes or deploys; it only merges locally. Treat pushing
  as a manual, separate step until that's deliberately wired up.
- Only register projects (`/add_project`) you're fine having an LLM run
  shell commands against.

## Roadmap

Later stages from the original plan (not implemented yet): `/new` project
scaffolding, Atlassian MCP (Jira/Confluence context pulled into tasks),
Yandex Calendar reminders via CalDAV + cron (deliberately kept LLM-free), and
streamed progress (`stream-json`) with multiple concurrent worktrees.
