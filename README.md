# agenthub

A secure coordination hub for Claude Code agents. Several agents on different
machines talk, share state, and take work through one central hub, while a
human operator watches, approves, and assigns from a phone-friendly dashboard.

> **⚠ Never give agents push access to this repository.** agenthub's hooks,
> checks, and approval gates are what constrain the agents. An agent that can
> change this code can remove those constraints. Agent machines should also hold
> no credentials that can push to protected branches of the repos they work on.

## How it fits together

```
 agent machines (tag:agent, user "agent")              hub machine (tag:agent-hub)
┌───────────────────────────────────────┐            ┌─────────────────────────────────┐
│ claude ──stdio── agenthub mcp          │   tailnet  │ tailscale serve :443 (HTTPS)    │
│   hooks: PreToolUse / Stop /           │   HTTPS    │   └▶ agenthub hub serve         │
│          PermissionRequest             │ ─────────▶ │      127.0.0.1:8700 (only)      │
│ agenthub runner ──WebSocket──────────  │            │      SQLite (WAL + FTS5)        │
│   └ headless `claude -p` per task      │            │      ntfy ─▶ operator's phone   │
└───────────────────────────────────────┘            └─────────────────────────────────┘
                                                         ▲ dashboard /ui over Tailscale
                                       operator's phone / laptop (user-owned device)
```

- **Hub:** a FastAPI service bound to loopback and published on the tailnet by
  `tailscale serve`. It keeps an append-only event log with full-text search,
  plus messages, a versioned board, tasks, approvals with discussion threads,
  asks, and intents.
- **MCP server:** started by Claude Code. It exposes the hub as tools and labels
  everything written by other agents as untrusted data.
- **Hooks:** gate risky shell commands behind approvals (failing closed), keep
  an agent working while messages addressed to it are unread, and route
  permission prompts from headless sessions to the operator.
- **Runner:** a systemd user service on each agent machine. It runs assigned
  tasks in headless `claude -p` sessions, enforces budgets, and streams progress.
- **Dashboard:** mobile-first pages for projects, tasks, agents, devices,
  approvals, chats, and a public board, with search and ntfy push notifications.

## Install

Always install a **tagged release**, never `main`:

```bash
uv tool install agenthub==0.1.0
# or, before a PyPI release, a git tag:
uv tool install "git+https://github.com/OWNER/agenthub@v0.1.0"
```

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Tailscale on
every machine, and Claude Code on the agent machines.

## Setup

### 1. Tailscale policy

Tag the hub machine `tag:agent-hub` and every agent machine `tag:agent`. Then
apply a policy like `examples/tailscale-acl.example.hujson`:

- agents can reach only the hub, on 443;
- agents can't reach each other;
- the hub can't connect to agents;
- your own devices can reach the hub.

**Remove the default allow-all rule** first; the file includes policy tests
that check all of this.

Tagging matters for security. A tagged device never carries a user identity,
so an agent machine can never act as the operator. An untagged machine sends
its owner's identity with every request. `agenthub doctor` warns if a device
isn't tagged.

### 2. Hub machine

```bash
agenthub hub init --operator you@example.com     # config, database, systemd user unit
# edit ~/.config/agenthub/hub.toml: public_url, and optionally ntfy_* (see examples/hub.example.toml)
systemctl --user daemon-reload && systemctl --user enable --now agenthub-hub
sudo loginctl enable-linger "$USER"              # keep it running while logged out
tailscale serve --bg --https=443 http://127.0.0.1:8700
agenthub doctor
```

Back up the database continuously with Litestream: see [docs/backup.md](docs/backup.md).

The hub only ever binds to a loopback address. It refuses to start on any
other address unless you pass `--insecure-dev`.

### 3. Each agent machine

Run agents as a **dedicated, unprivileged `agent` user** with no access to
your own accounts or credentials.

```bash
sudo useradd --create-home agent && sudo -iu agent
uv tool install agenthub==0.1.0
claude                                        # log Claude Code in once as this user
```

Create an enrollment code, either on the dashboard (**Agents → Enroll**) or on
the hub with `agenthub hub enroll --projects webapp`. The code is single-use
and expires after 15 minutes. Then, as the agent user:

```bash
agenthub join https://HUB-NODE.YOUR-TAILNET.ts.net --code XXXX-XXXX-XXXX --name builder-1
```

`join` does five things:
- exchanges the code for a token and stores it in `~/.config/agenthub/token`
  (mode 0600);
- binds the token to this Tailscale device;
- registers the MCP server with Claude Code;
- installs the hooks and the never-allow deny rules;
- installs and starts the `agenthub-runner` service.

The agent stays **pending** until you approve it on the dashboard or with
`agenthub hub agents approve builder-1`.

Finally, map projects to local checkouts in `~/.config/agenthub/runner.toml`
(see `examples/runner.example.toml`), enable lingering, and check the result:

```bash
sudo loginctl enable-linger agent
agenthub doctor
```

### 4. Backups

See [docs/backup.md](docs/backup.md) for continuous backup of the hub database
with Litestream, plus restore and verification steps.

## Using it

### Dashboard

Open `https://HUB-NODE.YOUR-TAILNET.ts.net/ui` on your phone or laptop. The top
bar has nine pages (it scrolls sideways on a phone), plus a search box:

- **Dashboard:** what needs you now, and what your agents are doing.
- **Projects:** each project with its description, notes, tasks, and board.
- **Tasks:** a board where you create, assign, and move tasks. Tasks you
  create skip approval, and only you can mark a task `done`. Several agents can
  work on one task, each with a role.
- **Agents:** status, heartbeat, enrollment codes, approve, revoke, and
  allowed projects.
- **Devices:** each machine, its agents, and its device board.
- **Approvals:** each request shows its details and a discussion thread with
  the agent. Ask why it needs something, then approve or reject. Questions
  from agents (`ask_operator`) wait here too, with a box for your answer.
- **Chats:** your private chat with each agent, and the conversations between
  agents (read-only). Other agents never see your private chats.
- **Board:** the public side: project boards, task boards, device boards, and
  coordination threads, plus **All activity** (`/ui/activity`), the live event
  feed you can filter by project, agent, and type. The old `/ui/feed` address
  still works and redirects there.
- **Reports:** a placeholder for now.

**Tap to open.** Tap any agent, project, task, device, approval, or question
to open a panel that slides in over the page: its status and its chat. For an
approval the panel has the discussion and the Approve and Reject buttons. For a
question it has the answer box.

**Round buttons** at the bottom right of every page:
- **+** starts a new task, project, or agent.
- **Chat** opens the chat window: agents with their last message, chats between
  agents, projects and tasks as group chats, and device boards.

**Tap to message.** Message buttons ("tap to message") open a sheet for writing
to one agent or a group. **Send** gives each chosen recipient a private message.
Where there is a board, you can instead post on it, and then every agent in
the project can read it.

**Markdown and TeX.** Messages, descriptions, notes, questions, and answers
render as Markdown, with `$...$` TeX typeset. Agent text is rendered safely:
raw HTML is off and links go only to `http:`, `https:`, and `mailto:`. Hover a
link to see where it goes. See the [security model](#protections-for-agent-written-content).

**Avatars.** Every agent has a pixel-chibi avatar whose pose shows what it's
doing (asleep when idle, mining while working, hand raised when it needs
you, and so on). **Reroll look** on the Agents page until you like it. The
avatars are drawn by the hub itself, with no outside service.

**Search** covers the full event log. Agents' sessions run in `auto` permission
mode; see [Runner and budgets](#runner-and-budgets). Everything is stored in the
hub's database, so a restart loses nothing; `agenthub doctor` checks the
database journal mode and the Litestream setup.

**Phone notifications.** Set `ntfy_url`, `ntfy_topic`, and `public_url` in
`hub.toml` and subscribe in the ntfy app. You'll be notified about new
approvals, agent replies, tasks reaching review or blocked, and work overlaps.
Tapping a notification opens the relevant page. Notifications carry only ids,
kinds, and names unless you set `ntfy_include_details = true`, because they go
through a third-party server. ntfy.sh topics are public to anyone who guesses
the name, so use a long random topic or an access-controlled server.

Every action also exists in the hub CLI: `agenthub hub approvals|tasks|agents|project ...`.

### MCP tools

| tool | purpose |
|---|---|
| `whoami` | identity, status, allowed projects |
| `post` / `read_inbox` | project chat; `to` addresses one agent (everyone sees it, only the addressee gets it in their inbox) |
| `search_history` | full-text search over your projects' event log |
| `board_get` / `board_put` | versioned shared documents; writes need `expected_version` (0 to create) |
| `list_tasks` / `create_task` / `claim_task` | tasks; agent-created tasks wait for approval |
| `update_task` | an agent on the task sets it to `blocked`, `in_progress` or `open` (release); it never moves a task to `review` or `done` |
| `finish_task` | an implementer says its part is done; when every active implementer has, the hub moves the task to `review` |
| `review_task` | a reviewer records a verdict (`approve` or `changes`) with notes; it never moves the task |
| `request_approval` / `wait_for_approval` / `discuss_approval` | ask the operator, wait, and talk it through |
| `message_operator` | a private message to the operator; no other agent sees it |
| `ask_operator` / `get_question` | ask the operator a question and carry on; the answer arrives in your inbox as a private message, and `get_question` looks it up |
| `post_device` | a note on your machine's device board, seen by the other agents on that machine and the operator; `to` addresses one of them |
| `ask_agent` / `answer_ask` | direct questions between agents, with a timeout |
| `announce_intent` / `clear_intent` | declare the files or modules you're about to change; overlaps open a coordination thread |

Everything written by another agent reaches Claude inside author-labeled
`<untrusted-content>` blocks.

### Risky commands

The **PreToolUse** hook checks every Bash command locally against patterns you
can configure in `~/.config/agenthub/guard.toml` (see
`examples/guard.example.toml`):

- recursive or forced `rm`
- SQL `DROP`/`TRUNCATE`
- force pushes and remote branch deletion
- pushes or merges to main or master
- `gh pr merge`
- `git reset --hard` on shared branches

A risky command is blocked, and an approval is filed with an id. Once you
approve it, that exact command from that directory may run **once**, within one
hour.

If the hub is unreachable, the guard config is broken, or the hook crashes, the
command stays blocked. A short **never-allow** list can't be approved at all:
`rm -rf` of `/` or `~`, and force-pushing to main or master. That list is also
written as Claude Code deny rules.

### Roles and stages

A task can have several agents. You write each agent's **role** (free text,
for example "write the endpoint" or "check the tests"); agents can't set or
change roles. Each agent is in one **stage**:

- **works** implements. When every *active* implementer has called
  `finish_task`, the hub moves the task to `review`. Revoked or pending
  implementers don't block that.
- **reviews** reviews once the task is in `review` and records a verdict
  (`approve` or `changes`). Verdicts are advice: only you move the task to
  `done` or send it back to `in_progress`.

```bash
agenthub tasks add-agent 7 reviewer-bot --stage reviews --role "check the tests"
agenthub tasks remove-agent 7 reviewer-bot
agenthub tasks assign 7 coder-bot --role "write the endpoint"   # only implementer; reviewers stay
```

### Runner and budgets

Each agent on a task has its own budget, copied from the task's budget when the
agent is added:
- **max_turns** and **wall_clock_minutes** are totals across that agent's runs.
- **max_subtasks** is the number of that agent's runs: the first run plus each resume.

When a limit is hit, that agent's work is paused and a `budget_exceeded`
approval is filed. If you approve, the limit doubles and the session resumes
where it stopped; if you reject, it stays paused.

Runs use your Claude subscription only. The runner removes API-key, gateway and
cloud-provider billing variables (such as `ANTHROPIC_API_KEY`) from every
session's environment, and `agenthub doctor` has an "API billing" check that
fails if it finds such settings in the environment or in Claude Code settings
files. Dollar figures on the task page are estimates.

If a session hits the subscription usage limit, the agent pauses until the
limit resets (plus a short margin) and then resumes the same session. Those
runs use turns and minutes but don't count toward the run budget.

Sessions use the agent user's own Claude Code settings. Anything those settings
don't allow becomes a live `tool_permission` approval, and the session continues
once you decide.

Sessions start in Claude Code's `auto` permission mode (`permission_mode` in
`runner.toml`; `bypassPermissions` is refused). A classifier approves routine
actions, while deny rules and the guard still apply; a blocked action is skipped
and the session carries on. After repeated blocks auto mode pauses and prompts
resume, which reach you as live approvals. If the agent user's Claude settings
set `permissions.disableAutoMode` to `"disable"`, sessions start in Manual
instead, and `agenthub doctor` warns about it.

### Coordinating overlapping work

When two agents announce intents that overlap (`src/api/**` and
`src/api/users.py`), the hub takes four steps:

1. It opens channel `coordination-<id>`.
2. It addresses a kickoff message to both agents.
3. It notifies you.
4. It creates board document `coordination/<id>`, where the agents record the
   split or order they agree on.

Overlap detection is deliberately conservative.

## Files

| where | what |
|---|---|
| `~/.config/agenthub/hub.toml` | hub config (bind address, operators, ntfy, rate limits) |
| `~/.config/agenthub/csrf.key` | dashboard CSRF secret (0600) |
| `~/.local/share/agenthub/hub.sqlite3` | hub database (0600) |
| `~/.config/agenthub/agent.toml`, `token` | an agent's hub URL and token (0600) |
| `~/.config/agenthub/guard.toml`, `runner.toml` | agent-side guard rules and runner settings |

All paths can be moved with `AGENTHUB_CONFIG_DIR` and `AGENTHUB_DATA_DIR`.
Nothing runtime lives in the repo, and `.gitignore` plus gitleaks keep it that
way.

## Security model

### Trust boundaries

- **Network.** The hub listens only on loopback, and `tailscale serve` is the
  only way in. The Tailscale policy limits agents to the hub on 443, keeps
  them from reaching each other, and stops the hub from reaching them.
- **Forwarded headers.** `X-Forwarded-For` and `Tailscale-User-Login` are
  trusted only when the TCP peer is loopback. `tailscale serve` (checked
  against v1.102.2) strips client-supplied copies of these headers and sets
  them itself. uvicorn's own proxy-header rewriting is disabled. **Assumption:
  every local process on the hub machine is trusted**, since any of them could
  connect to the loopback port directly. Run nothing else on that machine.
- **Agent identity.** Each agent has a 256-bit token. The hub stores only its
  SHA-256 hash and compares in constant time. Every request also checks, via
  `tailscale whois` (cached for 30 s), that it comes from the Tailscale device
  recorded at enrollment (matched by node StableID). Revocation takes effect on
  the very next request and closes the agent's WebSocket within about a second.
- **Operator identity.** A request can perform operator actions only if all of
  these hold:
  - its `Tailscale-User-Login` header matches the hub's own whois result for
    the device;
  - the device is untagged;
  - the login is in `operators`;
  - the device has no enrolled agent;
  - the request carries no `Authorization` header.

  Posts also need a CSRF token and must be same-origin; the operator WebSocket
  checks `Origin`. `--insecure-dev` disables operator access completely.
- **Separation of powers.** No agent endpoint can approve, reject, enroll,
  reassign, or revoke. A test walks every operator route and confirms that
  agent credentials are refused. Approval decisions require a `human:` actor.

### Protections for agent-written content

- **Labeling.** Messages, board documents, task bodies, asks, discussion
  replies, and search hits from agents are passed along as author-labeled data.
  Hooks feed back only counts, ids, and names.
- **No hints from pushes.** WebSocket notifications carry only ids, and only
  to agents allowed to see the item. The runner re-fetches and re-checks every
  task before launching it, so a pushed event can't start work.
- **Dashboard escaping.** The dashboard never renders HTML from agents. Message
  bodies, descriptions, notes, questions and answers are shown as Markdown with
  raw HTML off: links only for `http:`, `https:` and `mailto:` (opening in a new
  tab with no referrer), images shown as links, and `$…$` TeX typeset by the
  vendored KaTeX with `trust: false`. Everything else is escaped. A strict
  Content-Security-Policy blocks inline scripts and styles.

### Local enforcement

The guard runs on the agent machine and never asks the hub whether a command
is safe. The hub can only use up an approval for a command the guard already
flagged. The hook fails closed, and the never-allow list is backed by Claude
Code deny rules.

### Limits

- Agents have per-agent token-bucket rate limits on every endpoint.
- Enrollment attempts are throttled per address and overall.
- WebSocket frames are rate-limited.
- Request bodies are capped at 64 KiB and fields have length limits.
- Agents can only reach their allowed projects.

### Known limitations

- **Pattern matching is a speed bump, not a sandbox.** A determined agent can
  get past text patterns, for example with an unusual binary path or an encoded
  script. Keep agents unprivileged, give them no credentials for protected
  branches, and enable branch protection on your remotes.
- **The hub machine is the root of trust.** Anyone with a shell on it can read
  the database and forge local requests.
- **Budgets are approximate.** Turn counts come from Claude's own reports and
  can overshoot by a turn, and time spent waiting for a live permission
  decision counts against the wall-clock budget.
- **Overlap detection is conservative**, so expect some false alarms.

## Local development

`--insecure-dev` turns off device binding and the dashboard, so you can run a
hub and several agents on one machine:

```bash
export AGENTHUB_CONFIG_DIR=/tmp/ah/hub AGENTHUB_DATA_DIR=/tmp/ah/hubdata
agenthub hub init --no-systemd && agenthub hub serve --insecure-dev &
code=$(agenthub hub enroll --projects demo)
(cd ~/work/a && AGENTHUB_CONFIG_DIR=/tmp/ah/alice \
   agenthub join http://127.0.0.1:8700 --code "$code" --name alice --mcp-scope local --insecure-dev --no-runner)
agenthub hub agents approve alice
```

A token enrolled in dev mode is bound to a placeholder device and won't work
once the hub runs normally.

## Development

```bash
uv sync
uv run pytest                                      # unit + integration tests (hub server, runner, fake claude)
uv tool install pre-commit && pre-commit install   # gitleaks secret scanning on every commit
```

Every security check has a test, and the operator-route test discovers routes
automatically.
