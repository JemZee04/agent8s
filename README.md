# agent8s

Инструкция на русском: [README.ru.md](README.ru.md)

Telegram bot that drives headless coding agents (Claude Code, Codex — more
pluggable via `src/agent8s/agents/`) against local git repos, one isolated
`git worktree` per task, with all state in SQLite and git instead of the
model's context.

```
Telegram ── aiogram bot ── SQLite (projects, tasks, session_id) ── git worktree ── claude -p / codex exec
```

This is the Этап 0+1+2+3+4+5 slice: register or scaffold projects, run tasks
in worktrees with live streamed progress, inspect diffs, approve (merge) or
drop them, switch between agents, pull Jira context straight into a task,
and get calendar reminders. No push/deploy — merges stay local. Task queueing
and concurrent worktrees (the other half of Этап 5) aren't done — still one
active task per chat at a time.

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

or start from nothing:

```
/new invoice-svc a small service that generates PDF invoices
```

`/new <name> <description>` creates `$AGENT8S_PROJECTS_DIR/<name>` (default
`~/Documents/<name>`), `git init`s it, writes a `README.md` and a `CLAUDE.md`
seeded with the name/description (stack and commands left as TBD for the
first real task to fill in), makes the initial commit, registers it, and
sets it as the chat's active project. No external credentials needed — it's
purely local scaffolding.

Free text with no active task starts a new one: creates
`agent8s/task-<id>` as a branch + worktree, and runs the selected agent's
headless mode (`claude -p ... --output-format stream-json --verbose` /
`codex exec --json ...`) with the message as the prompt. While it runs, the
bot edits a single message live with each step as it happens — `🔧 Bash: npm
test`, `📝 add src/foo.py`, `💬 <a thinking-out-loud note>` — instead of going
silent until the whole task finishes; edits are throttled client-side
(roughly every 2s) since Telegram rate-limits message edits, but no step is
ever dropped, just coalesced into the next edit. When it's done that same
message is replaced with the agent's own summary plus `git diff --stat` —
never the agent's self-report of what it changed.

Free text while a task is active is a follow-up: it resumes the same agent
session (`--resume` / `codex exec resume`) in the same worktree with the same
live progress, so "actually, extract that into its own function" continues
the conversation instead of starting over.

- `/diff` — full `git diff` of the active task, sent as a file (Telegram's
  4096-char message limit makes anything non-trivial unreadable inline).
- `/approve` — commits any uncommitted changes in the worktree, merges the
  task branch into the project's default branch with `--no-ff`, removes the
  worktree. Local only — nothing is pushed anywhere.
- `/drop` — discards the task: removes the worktree and branch.
- `/status` — current project, agent, and active task for this chat.

## Jira context

```
/context PROJ-123
/task PROJ-123 also add a unit test for the edge case
```

Set `JIRA_URL` / `JIRA_PERSONAL_TOKEN` (and `CONFLUENCE_URL` /
`CONFLUENCE_PERSONAL_TOKEN` if you want linked pages pulled in too) in
`.env` — Server/Data Center only, Bearer-token auth via a Personal Access
Token (avatar → Profile → Personal Access Tokens → Create token). Leave
them blank to skip Atlassian entirely; `/context` and `/task` will just say
it's not configured.

- `/context <KEY>` — fetches the issue's summary/description/status and any
  Confluence pages linked to it via Jira remote links, and posts it as plain
  text. No agent call, no worktree — just a readable restatement of the
  ticket.
- `/task <KEY> [instructions]` — same fetch, then starts a task (like free
  text with no active task) using the ticket + linked pages as context,
  followed by your instructions or a default "implement what's described
  above". Requires an active project (`/use`) and no already-active task.

This fetches Jira/Confluence over REST from the orchestrator, deterministically,
rather than giving the agent MCP tools to search on its own — cheaper, and
`/context` shows you exactly what the agent is about to see before it starts.
Wiring an actual Atlassian MCP server into the agent (so it can search
Confluence beyond what's directly linked) is a possible later upgrade, not
done here.

## Calendar

```
/today
```

Set `YANDEX_CALDAV_URL` / `YANDEX_CALDAV_LOGIN` / `YANDEX_CALDAV_PASSWORD` in
`.env` — the URL is Calendar → Settings → Export in the Yandex web UI, and
the password is an *app password* (id.yandex.ru → Security → App passwords),
not your normal account password. For a corporate Yandex 360 domain, IMAP/
CalDAV protocol access may need to be turned on by the domain admin before an
app password will actually authenticate — a `401` right after creating one
usually means that, not a typo. `scripts/check_caldav.py` does a bare auth
check against the configured URL without pulling in the full bot, useful
while waiting for that to take effect.

- `/today` — lists today's events (time, title, location) from the
  configured calendar.
- Reminders run as a background loop inside the same bot process (not a
  separate cron job — simpler to run manually, still fully LLM-free): every
  `AGENT8S_REMINDER_POLL_SECONDS` (default 300) it checks for events starting
  within `AGENT8S_REMINDER_LEAD_MINUTES` (default 15) and messages every chat
  in `ALLOWED_CHAT_IDS`. Each event+start time is recorded in SQLite once
  sent so it's never repeated across polls. No agent, no prompt — pure
  CalDAV read + `bot.send_message`.

Leave the CalDAV variables blank to skip this entirely: `/today` says it's
not configured, and the reminder loop exits immediately at startup.

## Skills

`AGENT8S_CLAUDE_ALLOWED_TOOLS` includes `Skill` by default, so headless
`claude -p` picks up whatever skills you have configured — user-level
(`~/.claude/skills`), project-level, or via plugins — and decides on its own
when a task matches one, exactly like an interactive session. Verified this
directly: a headless run from inside a worktree lists the same skill set as
an interactive session on this machine (`tdd`, `code-review`, the
`mattpocock-skills` plugin set, etc.) once `Skill` is allowed — without it,
the tool call would just get silently denied since there's no one in
headless mode to approve an unlisted tool. Codex has no equivalent mechanism
today, so this only affects the `claude` agent.

## Adding another agent

Subclass `AgentRunner` in `src/agent8s/agents/` (see `claude_agent.py` /
`codex_agent.py` for the shape: `start(prompt, cwd, on_progress)` and
`resume(session_id, prompt, cwd, on_progress)` — `on_progress` is an optional
`async def(line: str)` called for each live step, both returning session id +
summary text), then add one line to `AGENT_NAMES` and `build_agent()` in
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
- The worktree is where a task's own git diff comes from, not a filesystem
  sandbox — `Bash`/`Edit`/`Write` can still follow an absolute path anywhere
  the OS user can reach, worktree or not, if the prompt asks for it (e.g.
  "also branch and edit the libs this depends on"). That's sometimes exactly
  what you want, but it means such edits land directly in your real working
  copy, outside `/diff` / `/approve` / `/drop` entirely — review a task's
  prompt with that in mind before sending it.

## Reliability

A stuck-forever task on 2026-08-21 turned up two real bugs worth naming, not
just fixing quietly:

- **Single-instance lock.** Nothing stopped two `agent8s-bot` processes from
  polling the same bot token at once (they did, four of them, accumulated
  over a few days of restarting in new terminals without killing the old
  one) — which is exactly the kind of thing that makes "why didn't this
  work" impossible to debug from symptoms alone. Startup now takes an
  exclusive flock on `<data dir>/bot.lock`; a second instance refuses to
  start with a clear message instead of silently racing the first one for
  updates.
- **Startup reconciliation.** A task's status only ever left `running` when
  its own handler coroutine finished — normally fine, but if that coroutine
  dies without warning (crash, force-kill, an unhandled exception mid-run)
  the task sits `running` forever and the chat stays blocked on it, with no
  failure ever reported. A `running` task cannot have survived past the
  process that started it, so on every startup any leftover `running` tasks
  are marked `failed`, their chat's active task is cleared, and the affected
  chats get a message explaining why.
- Progress-update Telegram calls (`ProgressReporter`) now catch
  `TelegramAPIError` broadly instead of just `TelegramBadRequest` — a flood
  wave of tool-call updates hitting Telegram's edit rate limit could raise
  `TelegramRetryAfter`, which used to propagate up and kill the task's
  coroutine outright. And the `agent.start()`/`.resume()` call itself is now
  wrapped so *any* unexpected exception becomes a normal failed result
  instead of an unhandled crash — the whole point being that a task can no
  longer die silently with nothing to show for it.

## Roadmap

Not implemented yet: a task queue and running multiple worktrees
concurrently — right now a chat can only have one active task at a time.
