# agent8s

Инструкция на русском: [README.ru.md](README.ru.md)

Telegram bot that drives headless coding agents (Claude Code, Codex — more
pluggable via `src/agent8s/agents/`) against local git repos, one isolated
`git worktree` per task, with all state in SQLite and git instead of the
model's context.

```
Telegram ── aiogram bot ── SQLite (projects, tasks, session_id) ── git worktree ── claude -p / codex exec
```

This is the Этап 0+1+2+3+4+5 slice, plus self-maintenance on top: register or
scaffold projects, run tasks in worktrees with live streamed progress,
inspect diffs, approve (merge) or drop them, switch between agents, pull
Jira context straight into a task, get calendar reminders, ask read-only
questions without opening a task, and point the bot at diagnosing and fixing
its own code. No push/deploy — merges stay local. Task queueing and
concurrent worktrees (the other half of Этап 5) aren't done — still one
active task per chat at a time (plus one *parked* task, see `/diagnose`
below).

**The bot's own Telegram UI (every command's replies) is in Russian** — this
README stays in English as the technical reference; [README.ru.md](README.ru.md)
is both the practical Russian guide and a closer match to what you'll
actually see in the chat.

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

On every startup the bot registers its command list with Telegram
(`bot.set_my_commands`, see `BOT_COMMANDS` in `src/agent8s/bot/handlers.py`)
so they show up with descriptions in the client's `/` menu — no separate
step needed, and it stays in sync automatically whenever a command is
added or reworded.

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
- `/continue` — see "Interrupted and parked tasks" below.

## Ad hoc questions: /ask

```
/ask what's the auth flow in this project?
/ask find every place we call the YandexGPT API
```

`/ask <text>` runs the current project's agent directly against the real
checkout (no worktree, no branch, no task tracking) in a hard read-only
mode — `--permission-mode plan` for claude, `-s read-only` for codex.
Verified live: both refuse to write anything (claude explains it can't exit
plan mode to apply an edit; codex's sandbox rejects the write outright), so
this is safe to run against your actual working copy, not just a worktree.
For anything that should actually change files, use free text (a real task)
instead.

## Interrupted and parked tasks: /continue

Two situations leave a task off to the side instead of active or gone:

- **Interrupted.** If the bot process dies mid-task (crash, force-kill) the
  task can't have survived — on the next startup it's marked `interrupted`
  (not `failed`) as long as it reached at least one turn and has a
  `session_id`, since claude/codex persist that session to disk independent
  of our process. `failed` instead means there's truly nothing to resume.
- **Parked.** `/diagnose` (below) temporarily sets aside whatever task was
  active so it can use the chat's task slot for a self-fix, without losing
  track of what you were doing.

`/continue` restores whichever applies: the chat's parked task if there is
one, otherwise the most recently touched `active`/`interrupted` task for
that chat. It re-attaches the task to the normal slot and shows its current
diff stat — your next message continues it exactly like any other follow-up.

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

## Self-maintenance: /diagnose, /improve, /restart, /autostart

```
/diagnose bot got stuck for 17 hours with no error, task #3 status was "running" forever
/improve add a /whoami command that echoes the chat's ALLOWED_CHAT_IDS entry
/restart
/autostart on
```

`/diagnose [symptom]` and `/improve <what to add/change>` both point the bot
at its own source — same underlying `_run_self_task`, different prompt
framing (bug-hunt-with-log-context vs. feature-shaped-like-the-existing-code),
picked by which one matches what you actually want. Both register the bot
as a project (name `agent8s`, path resolved from `__file__` — no config
needed) the first time either is used, and run a normal task against it —
worktree, branch, live progress, `/diff`, `/approve`, all the same machinery
as any other task. `/diagnose` additionally feeds the agent the tail of
`data/bot.log` (rotating file handler, persisted across restarts — not just
stdout, which disappears with the terminal). Both tell the agent explicitly
not to merge or restart on its own, and `/improve` also points it at
`handlers.py`/`agents/` as the style reference and asks it to update the
READMEs when the change is worth documenting. If some other task was active
for the chat, it gets *parked* (see `/continue` above) rather than blocked
on or lost, so you can work on the bot without losing your place on whatever
else you were doing.

`/approve`-ing a self-fix or self-improvement only merges the branch — the
running process is still executing the old code from memory (Python doesn't
hot-reload).
`/restart` re-execs the process in place (`os.execv`, same PID, keeps the
singleton lock) so the merged change actually takes effect; it's a separate,
explicit step on purpose — you decide when, not the moment a fix is merged.

`/autostart on|off|status` wraps a macOS LaunchAgent
(`~/Library/LaunchAgents/com.agent8s.bot.plist`, `RunAtLoad` + `KeepAlive`)
so the bot survives logins and crashes without a terminal open — CLI
equivalents are `scripts/install_launchagent.sh` /
`scripts/uninstall_launchagent.sh`. **Known issue, not resolved**: on at
least one machine, a process started *by launchd* hangs indefinitely during
plain Python interpreter startup (stuck reading `.venv/pyvenv.cfg`, confirmed
via `sample` on the stuck PID) — the exact same binary invoked identically
but *not* through launchd starts in under a second. Strong suspicion is
macOS TCC/sandboxing blocking file access under `~/Documents` for a
LaunchAgent with no interactive consent context, but that's unconfirmed; the
fix would be a manual System Settings → Privacy & Security grant, which
can't be done non-interactively. Until this is root-caused, treat
`/autostart` as installed-but-unverified and keep using `./scripts/run.sh`
manually; `/restart` is unaffected (it re-execs an already-running,
already-permitted process rather than a fresh launchd spawn).

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
  are reconciled to `interrupted` (recoverable via `/continue` — see above)
  or `failed` (no session ever came back, nothing to resume), their chat's
  active task is cleared, and the affected chats get a message explaining
  why.
- Progress-update Telegram calls (`ProgressReporter`) now catch
  `TelegramAPIError` broadly instead of just `TelegramBadRequest` — a flood
  wave of tool-call updates hitting Telegram's edit rate limit could raise
  `TelegramRetryAfter`, which used to propagate up and kill the task's
  coroutine outright. And the `agent.start()`/`.resume()` call itself is now
  wrapped so *any* unexpected exception becomes a normal failed result
  instead of an unhandled crash — the whole point being that a task can no
  longer die silently with nothing to show for it.
- Both agent subprocess readers now pass `limit=16 * 1024 * 1024` to
  `asyncio.create_subprocess_exec` (`STREAM_LIMIT` in `claude_agent.py` /
  `codex_agent.py`). Caught live via the exception-safety net above: a
  `/improve` task reading its own (large) `handlers.py` produced a
  stream-json line past asyncio's default 64KiB-per-line `StreamReader`
  limit, raising `LimitOverrunError` — silently turned into a failed result
  instead of a stuck task, but worth actually fixing rather than leaving
  every large-file read as a coin flip.

## Roadmap

Not implemented yet: a task queue and running multiple worktrees
concurrently — right now a chat can only have one active task at a time.
