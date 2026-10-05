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
actually see in the chat. **The agents' own output is Russian too**, not
just the bot's chrome around it — both `AgentRunner`s carry a
`RESPONSE_LANGUAGE_INSTRUCTION` (`src/agent8s/agents/base.py`) that overrides
the model's default lean toward English for anything code-shaped, even when
prompted in English; verified live for both claude and codex against an
English prompt. Code identifiers/comments are explicitly exempted — the
instruction only covers what the agent says to the user, not the code it
writes, so existing codebase conventions aren't disturbed.

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
- `/grant <path>` — see "Writable directories outside the worktree" below.

## Writable directories outside the worktree: /grant

codex actually sandboxes writes to `cwd` (`AGENT8S_CODEX_SANDBOX`,
`workspace-write` by default) — confirmed live: a task asked to write
outside its worktree got a real `operation not permitted` from the sandbox,
not a hallucinated excuse. claude has no equivalent restriction (its Bash/
Edit/Write tools follow any path the OS user can reach — see the security
note above), so this only matters for codex tasks.

```
/grant /Users/you/go/src/some-other-service
```

`codex exec resume` has no `--add-dir` (checked its `--help` directly — the
option only exists on the initial `exec`, not `resume`), but its sandbox can
still be widened per-call via `-c sandbox_workspace_write.writable_roots=[...]`
— confirmed live that this *adds* to the default writable roots (cwd stays
writable) rather than replacing them. `/grant` records the directory against
the task; the very next message applies it, whether that message resumes the
existing session or starts a new one — no session or conversational memory
is lost. Verified live: denied without the grant, the exact same write
succeeds immediately after. For claude, `/grant` still records the directory
(passed along as `--add-dir` on the next call) but says so plainly — there's
no sandbox being widened, since there wasn't one restricting it in the first
place.

Do **not** act on an agent's own suggestion to fix a permission denial by
running `codex resume` (or anything else) directly in a terminal — that
bypasses agent8s's tracking entirely, and the bot's view of the task's
session/diff state will drift from whatever actually happened on disk.

## Desktop app

A native window with full chats (like the Codex / Claude desktop apps) on top of the same
orchestration: several chats run in parallel, each in its own worktree, and **you can switch the
agent and model inside one chat without losing context**.

```bash
uv sync --extra desktop                      # pywebview (native WKWebView window, no Electron)
(cd desktop-ui && npm install && npm run build)
uv run agent8s-desktop                       # --no-window prints a URL for a browser instead
```

Needs no Telegram credentials; it shares the database and the project list with the bot.

- **Switching agents.** claude and codex keep sessions in incompatible native formats, so the app
  owns the canonical history. When an agent picks a chat up (or starts it) and missed turns,
  its next prompt is prefixed with a compact transcript of exactly those turns plus the
  working-tree status. Switching only the model (same agent) resumes the native session as-is.
  Tool outputs are left out of the transcript on purpose: the files on disk are the source of truth.
- **Where is my code?** The header always shows the project, branch and the worktree path
  (click to copy, "open folder" for Finder; Terminal / VS Code in the ⋯ menu). "Direct" mode works
  in the project folder itself instead of a worktree.
- **Changes panel** (⇧⌘D): live diff, commit, and "merge into <default branch>" (aborted cleanly on conflict).
- **Live progress:** token streaming, every tool call as it happens, elapsed time, Stop (⌘.) which
  kills the agent's whole process group.
- Shortcuts: ⌘N new chat, ⌘1…9 jump to a chat, ⇧⌘D changes, ⌘. stop.
- Security: the server listens on 127.0.0.1 only and requires a per-launch token (kept in the URL
  fragment, never on disk) plus Host/Origin checks, so web pages open in your browser cannot drive
  your agents. Agent output is rendered through an escaping Markdown renderer under a strict CSP.
- UI development: `agent8s-desktop --no-window --port 8765 --token dev` and `npm run dev` in `desktop-ui`.
- Tests: `uv run pytest`.

Not done yet: importing Codex sessions (Claude Code sessions are covered, see below), LLM-written
handoff summaries (the transcript is truncated, not summarised), queuing a message while an agent works.

### Run it in the background (autostart)

```bash
uv run agent8s-desktop --install-service     # --uninstall-service, --service-status
```

Installs a macOS LaunchAgent: a headless `agent8s-desktop` starts at login, restarts after a crash and keeps
the phone connected whether or not a window is open. `agent8s-desktop` (no flags) then just opens a window on
that running service. Things worth knowing:

- The service lives **outside the repository**: its own virtualenv copy of the code, database and worktrees
  under `~/Library/Application Support/agent8s`. (A process started by launchd cannot read `~/Documents`
  without a permission prompt nobody can answer, so it would hang on its own venv; this avoids that. The first
  install copies your existing projects and chats into the service's database, after which it has its own.)
- After changing the code, run `--install-service` again to update the copy (it is safe while running).
- Settings from `.env` that matter (`AGENT8S_RELAY_URL`, `AGENT8S_PREVIEW_ORIGIN`, `AGENT8S_PROJECTS_DIR`,
  `AGENT8S_CLAUDE_*`, `AGENT8S_CODEX_SANDBOX`) are copied into the service at install time; the service cannot
  read the repo's `.env` (it holds secrets it does not need, such as the Telegram token).
- For development use `agent8s-desktop --standalone`: it starts its own server on the repo's data instead.

### Import chats from Claude Code

Sidebar → "⇩ Import" lists your sessions from `~/.claude/projects`; pick some (or "select all new") and they become
chats with their full history. The chat works in place in the folder the session ran in (`claude --resume` only
finds a session from there), the next Claude turn resumes that very session, and switching to Codex hands it the
history like any other chat. Folders that are not git repositories import fine (just without diff/commit tools),
sessions whose folder no longer exists or that were launched from temp folders are greyed out. The session files
are only read, never changed. Long histories load a page at a time ("Show earlier messages").

### Read an HTML file on the phone

The same **🌐 Preview** dialog lists the HTML files in the chat's folder (newest first, so what the agent just generated
is on top) and takes a path to any other `.html` file. The Mac serves the file together with what a web page uses next to
it (images, styles, scripts, fonts, `.json`/`.txt`/`.md`/`.pdf`) so it renders properly and links between pages work. The
folder is *not* shared as a whole: dotfiles, `..`, symlinks pointing out of it and anything else (keys, `.env`,
databases) answer 404. Same link lifetime, relay trust and second hostname as the site previews below.

### Open a site the agent runs on `localhost` on the phone

Ask the agent to "run the site on localhost", then open **🌐 Preview** in the chat (desktop or phone): it lists what
listens on this Mac (processes from the chat's folder first, anything that looks like a dev server next, the rest
folded away) or takes a port. The link lives 8 hours; when it is created on the desktop the phone gets an "Open"
notification. How it works and what to know:

- Requests from the phone's browser go phone → relay → your Mac → `localhost:PORT` and back. Unlike chat traffic this
  is **not end-to-end encrypted** (the relay has to read pages to serve them), so previews trust the relay.
- The page is served from a **second hostname** of the same server (default: `www.` + your domain, or set
  `AGENT8S_PREVIEW_ORIGIN`). That makes it a different browser origin than the app, so scripts of the previewed
  site cannot reach the app's stored key. Sites use absolute links (`/app.js`), so the preview takes the root of that
  hostname and is selected by a host-only `HttpOnly` cookie that the entry link sets; without the cookie, visitors of
  that hostname are redirected to the main domain. One preview per browser at a time.
- Not supported: WebSockets (a dev server's hot reload will not live-update), streaming/SSE, responses over 5 MB.
  Redirects to `localhost:PORT` and `Domain=` cookies are rewritten so login flows keep working.
- Needs the second hostname to resolve to the same nginx and be covered by its certificate; add this `server` next to
  the one from the Phone section (relay container `agent8s-relay`, cookie `a8p`):

```nginx
limit_req_zone $binary_remote_addr zone=agent8s_pv:2m rate=100r/s;       # in http { }

server {
    listen 443 ssl;
    server_name www.example.com;
    # ssl_certificate ... (the same certificate)
    location ^~ /agent8s/p/ {                      # entry link: sets the cookie
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $agent8s_relay http://agent8s-relay:8765;
        proxy_pass $agent8s_relay;
        proxy_http_version 1.1; proxy_set_header Connection "";
        proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location / {
        if ($cookie_a8p = "") { return 301 https://example.com$request_uri; }   # not a preview visitor
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $agent8s_relay http://agent8s-relay:8765;
        proxy_pass $agent8s_relay/agent8s/pv$request_uri;
        limit_req zone=agent8s_pv burst=300 nodelay;
        proxy_http_version 1.1; proxy_set_header Connection "";
        proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 90s; client_max_body_size 1m;
    }
}
```

## Phone

The phone shows the same chats and projects as the desktop app and drives the same agents. It is a
PWA (the desktop UI in a mobile layout), so there is nothing to build or sign: open a link in Safari
and add it to the Home Screen.

```
iPhone (Safari / Home Screen app) --WSS--> relay on your server <--WSS-- Mac (agent8s-desktop)
              \_____________ end-to-end encrypted, the relay only forwards bytes _____________/
```

Both sides connect **outbound** over ordinary HTTPS (port 443), so it works behind home NAT and on
mobile networks that block SSH; at home it uses the same path, there is no separate LAN mode.

**Set up the relay** (once). It is one small container behind the nginx you already run:

1. Deploy it: `PROXY_NETWORK=<docker network of your nginx> relay/deploy.sh <ssh host>`
   (builds the UI, copies it with `server.py`, runs `docker compose up`; no port is published on the host).
2. Route a path to it in that nginx (inside the `server { listen 443 ssl; }` block):

```nginx
# in http { }
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
limit_req_zone  $binary_remote_addr zone=agent8s:1m rate=30r/s;
limit_conn_zone $binary_remote_addr zone=agent8s_conn:1m;

# in server { }
location = /agent8s { return 308 /agent8s/; }
location ^~ /agent8s/ {
    resolver 127.0.0.11 valid=10s ipv6=off;       # resolved per request: nginx still starts if the relay is down
    set $agent8s_relay http://agent8s-relay:8765;
    proxy_pass $agent8s_relay;
    limit_req zone=agent8s burst=60 nodelay;
    limit_conn agent8s_conn 40;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
}
```

**Pair a phone:** set `AGENT8S_RELAY_URL=https://your.domain/agent8s` in `.env`, open the desktop app, click
"Phone", create the QR code, scan it with the iPhone camera and open the link in Safari. To install it as an
app, tap "Copy key" on the page that appears, then Share → Add to Home Screen, open it and tap "Paste key"
(iOS keeps Home Screen apps' storage separate from Safari, so the key has to be handed over once).
While pairing is enabled the app keeps the Mac from idle-sleeping (`caffeinate`); the desktop app must be
running (`agent8s-desktop --no-window` is enough).

**Security model.** The pairing key is 32 random bytes shown only in the QR code. From it both sides derive
the rendezvous id and an AES-256-GCM key (HKDF-SHA256); the relay sees neither. Frames bind direction and room
into the authenticated data, every request carries a per-connection nonce issued by the Mac and a strictly
increasing counter (captured frames cannot be replayed, even after a restart), and the Mac only forwards a fixed
list of `/api/...` endpoints. A compromised relay can drop or delay traffic but cannot read or forge it. The
room id travels in the first WebSocket message, not in the URL, so it is absent from access logs. In the
browser the key is a non-extractable `CryptoKey` in IndexedDB. Caveat: if the relay shares an origin with
other sites on the same domain (a path, not a subdomain), a script injected into one of *those* pages could
use the key; serving the relay on its own subdomain removes that. "New QR code" rotates the key and
disconnects every phone; "Disconnect" deletes it. Anyone holding the QR code can run agents on your Mac:
treat it like an SSH key.

Not done yet: a direct same-network mode, push notifications (iOS limits them for Home Screen web apps).
To have the Mac side start by itself at login, see "Run it in the background" above.

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

## Yandex Cloud billing

```
/balance
```

Researched this before building it — Yandex Cloud has no actual webhook
mechanism for billing (Monitoring's alert channels are email/SMS/push/
Telegram/Cloud Functions, not an arbitrary URL; the `Budget` resource's own
notifications only reach Yandex Cloud user accounts, not external
endpoints), and no live per-service consumption REST API either — a daily
breakdown by service is only available via a CSV export to Object Storage,
set up once in the console. Given that setup wasn't wanted for the first
pass, this is balance-only: same polling-loop pattern as the calendar
reminders, approximating spend from balance deltas over time instead of a
real per-service breakdown.

The Billing API needs a real IAM token, not a static API key (confirmed:
Billing isn't among the services API keys work with), and Yandex's own docs
say personal OAuth tokens aren't for automation — so this needs a **service
account** with an **authorized key** (JWT-signed, PS256, exchanged for a
~12h IAM token, auto-refreshed):

1. Console → the cloud your billing account belongs to → Service accounts →
   Create → grant it the `billing.accounts.viewer` role on the billing
   account.
2. That service account → Create new key → Create authorized key → download
   the JSON file, drop it in `./data/` (gitignored) — matches
   `YC_SERVICE_ACCOUNT_KEY_FILE`.
3. Billing account ID is in the console URL / the Billing section.

```
YC_SERVICE_ACCOUNT_KEY_FILE=./data/yc-authorized-key.json
YC_BILLING_ACCOUNT_ID=
YC_BALANCE_ALERT_THRESHOLD=
```

Verified the JWT-signing and token-exchange mechanics live against the real
`iam.api.cloud.yandex.net` endpoint (with a throwaway key pair, so the
exchange itself correctly failed on an unknown subject) — the plumbing is
right; a real service account key is what's needed to actually pull a
balance.

- `/balance` — current balance, plus spend/top-up over the last ~24h
  (nearest recorded snapshot to 24h ago vs. now — an approximation, not a
  ledger, since there's no per-transaction API).
- Background loop polls every `YC_BALANCE_POLL_SECONDS` (default 1800),
  records a snapshot, and messages every chat in `ALLOWED_CHAT_IDS` once
  balance drops below `YC_BALANCE_ALERT_THRESHOLD` — at most once per
  `YC_BALANCE_ALERT_COOLDOWN_HOURS` (default 12) so it nags, not spams,
  while you're low.

Leave `YC_SERVICE_ACCOUNT_KEY_FILE`/`YC_BILLING_ACCOUNT_ID` unset to skip
entirely: `/balance` says so, and the loop exits immediately at startup.

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
