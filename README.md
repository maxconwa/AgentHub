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
 agent machines (tag:agent, any account)               hub machine (tag:agent-hub)
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
  plus chats (one per agent, task and project), versioned board documents,
  tasks, approvals with discussion threads, asks, and intents.
- **MCP server:** started by Claude Code. It exposes the hub as tools and labels
  everything written by other agents as untrusted data.
- **Hooks:** keep agents off the operator's dashboard, keep an agent working
  while messages addressed to it are unread, and route permission prompts from
  headless sessions to the operator.
- **Runner:** a systemd user service (a launchd agent on macOS) on each agent machine. It runs assigned
  tasks in headless `claude -p` sessions until they're done, hands your chat
  messages to the agent's sessions (into a running one, mid-turn), and streams
  every session's text and tool calls live into the chats.
- **Dashboard:** mobile-first pages for projects, tasks, agents, devices,
  approvals and chats, with search and ntfy push notifications.

## Install

Always install a **tagged release**, never `main`:

```bash
uv tool install "git+https://github.com/maxconwa/AgentHub@v0.3.8"
```

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Tailscale on
every machine, and Claude Code on the agent machines. agenthub is not on PyPI:
a package named `agenthub` there is not this project, so don't install it by
name. uv installs without root
(`curl -LsSf https://astral.sh/uv/install.sh | sh`, into `~/.local/bin`), as
does agenthub; only Tailscale and lingering need `sudo`.

### Upgrading

**Upgrade the runners and the hub together.** Install the same release on the
hub machine and on every agent machine, then restart the hub and each runner.
The hub migrates its database on start. Mixed versions are not supported.

**0.3.9.0 lets names have capitals and lifts the length limit on what you
write.** A 0.3.8 runner doesn't know names with capitals (it builds units and
folders from the raw name, and matches `runner.toml` keys exactly), so upgrade
the runners with the hub. Migration 23 only adds; existing lowercase names,
units, folders and branches stay as they are.

**0.3.8 replaces prompts with chats**, so this matters more than usual:

- A 0.3.7 runner on a 0.3.8 hub gets no prompts and never sees your chat
  messages. They wait, undelivered, until its runner is upgraded.
- A 0.3.8 runner on a 0.3.7 hub logs "upgrade the hub too" once and only runs
  tasks.
- The agents' MCP tools changed too: `chat_post`, `chat_history`, `take_task`
  and `sit_out` are new, and `post`, `prompt_status`, `claim_share` and
  `decline_share` are gone. Each agent machine's `agenthub mcp` is the
  installed release, so upgrading the machine upgrades its tools.

On its first start, a 0.3.8 hub copies your old prompts, private messages,
board threads and other channel posts, and the approvals and questions, into the
matching chats. It doesn't change or delete the originals. A prompt that was
still pending or running reaches its agents as a chat message instead.

**Rolling back to 0.3.8** is possible because migration 23 only adds. Respell
names with capitals in lowercase first, while 0.3.9.0 is still installed
(`agenthub hub agents rename`, `agenthub hub project rename`), then roll back
the hub and the runners together.
[docs/rollback-0.3.8.md](docs/rollback-0.3.8.md) covers the steps.

**Rolling back to 0.3.7** is possible because the database changes only add.
Roll back the hub and the runners together. Messages written in chats under
0.3.8 aren't visible to 0.3.7. [docs/rollback-0.3.7.md](docs/rollback-0.3.7.md)
covers the steps and everything rolling back loses.

**Rolling back further, to v0.1.0:** a v0.1.0 hub rejects unknown keys in
`hub.toml`, so remove `prompt_discussion_minutes` from it first, or the hub
won't start. A hub older than 0.3.6.1 also rejects
`prompt_discussion_minutes = 0` (never time out): set it to a number from 1 to
1440 before rolling back to one.

**Rolling back from 0.3.9.1 to 0.3.9.0:** hand over the agents' messages that
are still waiting, and remove `[wakes]` from `hub.toml`, before you install
0.3.9.0. [docs/rollback-0.3.9.0.md](docs/rollback-0.3.9.0.md) has the steps.

## Setup

### 1. Tailscale policy

Tag the hub machine `tag:agent-hub` and every agent machine `tag:agent`. Then
apply a policy like `examples/tailscale-acl.example.hujson`:

- agents can reach only the hub, on 443;
- agents can't reach each other;
- the hub can't connect to agents;
- your own devices can reach the hub.

**Remove the default allow-all rule** first; the file includes policy tests
that check all of this. The file also has commented-out grants for keeping your
own devices connected to each other and for SSH; read the warnings before
enabling the agent-to-agent one.

Save the `tagOwners` block **before** tagging a machine. Until both tags are
defined, `tailscale up --advertise-tags=...` fails with `requested tags [...]
are invalid or not permitted`.

**Keep at least one device untagged** (your phone, say). Only an untagged
device can use the dashboard as the operator, so if every device is tagged,
nobody can approve anything.

Tagging matters for security. A tagged device never carries a user identity,
so an agent machine can never act as the operator. An untagged machine sends
its owner's identity with every request. `agenthub doctor` warns if a device
isn't tagged.

### 2. Hub machine

The hub runs as an ordinary user account; it needs no dedicated user.

```bash
sudo tailscale up --advertise-tags=tag:agent-hub
sudo tailscale set --operator="$USER"            # lets this account run `tailscale serve` without sudo
sudo loginctl enable-linger "$USER"              # keep it running while logged out
agenthub hub init --operator you@example.com     # config, database, systemd user unit
# edit ~/.config/agenthub/hub.toml: public_url, and optionally ntfy_* (see examples/hub.example.toml)
systemctl --user daemon-reload && systemctl --user enable --now agenthub-hub
tailscale serve --bg --https=443 http://127.0.0.1:8700
agenthub doctor
```

- `--operator` is the **Tailscale login** of your own devices, as shown in the
  USER column of `tailscale status` (e.g. `you@gmail.com`), not necessarily the
  email you use elsewhere.
- `public_url` is `https://` plus this machine's MagicDNS name
  (`tailscale status --self`).
- The first `tailscale serve` on a tailnet prints a link to enable Serve and
  HTTPS certificates; open it, then rerun the command.
- Open the dashboard from one of your **untagged** devices. On the hub itself
  (or any tagged machine) it shows "Not allowed: no Tailscale user identity".
  That's expected: tagged devices carry no user identity.

Back up the database continuously with Litestream: see [docs/backup.md](docs/backup.md).

The hub only ever binds to a loopback address. It refuses to start on any
other address unless you pass `--insecure-dev`.

### 3. Each agent machine

agenthub runs under whichever account you install it in. Nothing depends on
the account's name, and everything it writes lives in that account's home
directory. Tag the machine first:

```bash
sudo tailscale up --advertise-tags=tag:agent
```

Then choose the account:

- **A dedicated, unprivileged account (recommended).** For example `agent`, created
  with `sudo useradd --create-home agent && sudo -iu agent` on Linux, or a
  Standard user in System Settings → Users & Groups on macOS. Claude then can't
  read your files, SSH keys, or git and cloud credentials, and can't `sudo`. Keep
  your home directory closed to it (`chmod 750 ~`) and don't add it to your
  groups. The rest of this README calls this account `agent`.
- **Your own account.** This also works and is simpler, but every agent session
  then has everything your account has. Only choose it if nothing your account
  can reach matters.

Each agent has its own config directory (`join` stores one token per config
directory). To run more agents on the same machine, use another account or
give each agent its own config directory in the same account; see
[Several agents on one account](#several-agents-on-one-account).

As the chosen account:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv is missing
uv tool install "git+https://github.com/maxconwa/AgentHub@v0.3.8"
claude                                        # log Claude Code in once as this account
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
- installs the hooks and a deny rule for WebFetch of the hub;
- installs and starts the `agenthub-runner` service.

The agent stays **pending** until you approve it on the dashboard or with
`agenthub hub agents approve builder-1`.

**Names.** Agent and project names are 1 to 64 letters, digits, `-` and `_`,
starting with a letter or digit, and may have capitals (`Golem`,
`Hub-Builder-1`). The hub shows a name exactly as written, but two names that
differ only in case are the same name: it won't create `golem` when `Golem`
exists. Everything that looks a name up ignores case: the API, dashboard
URLs, MCP tools, the CLI and @mentions (`@hub-builder-1` in a chat message
addresses `Hub-Builder-1` if it is in that chat; the text keeps the name as
written). Wherever a name leaves the hub for something that can't take
capitals, or that would treat `Foo` and `foo` as two things, it goes through
one conversion, `agenthub.names.slug(name)`: the name lowercased. Runner
units and launchd labels, config folders and the git branches agents create
use the slug. A lowercase name is its own slug, so nothing named before 0.3.9.0
moves. `agenthub hub agents rename` and `agenthub hub project rename` respell a
name in other case, for example lowercase before rolling back
([docs/rollback-0.3.8.md](docs/rollback-0.3.8.md)).

Finally, map projects to local checkouts in `~/.config/agenthub/runner.toml`
(see `examples/runner.example.toml`), enable lingering, and check the result.
Project names in `runner.toml` match ignoring case, like everywhere on the hub
(`agent-hub = "..."` serves the project `Agent-Hub`), so two keys that differ
only in case are an error and the runner won't start.

```bash
sudo loginctl enable-linger "$USER"           # as an administrator, for the agent's account
agenthub doctor
```

#### macOS agent machines

The steps are the same, with three differences:

- **The account.** For a dedicated account, create a Standard (not
  administrator) user in System Settings → Users & Groups and sign in to it
  once on the desktop. Claude Code keeps its login in that account's keychain,
  which is only unlocked in a desktop session. Your own account works the same
  way while you're signed in.
- **The runner.** `join` installs a launchd agent,
  `~/Library/LaunchAgents/com.agenthub.runner.plist`, and starts it in the
  user's `gui/<uid>` domain. It logs to `~/Library/Logs/agenthub-runner.log`.
  There is no lingering on macOS: the runner runs while its account is
  signed in, so leave that session open with fast user switching. If you
  run `join` over `sudo -iu agent` or SSH, there is no desktop session to
  start the runner in; `join` says so and prints the `launchctl bootstrap`
  command to run once you sign in.
- **Tailscale.** The macOS app keeps its CLI inside the app bundle;
  `agenthub doctor` finds it there. Tag the Mac `tag:agent` like any other
  agent machine. The Mac then can no longer act as the operator, so don't
  use it for the dashboard. The macOS apps (App Store and standalone) can't
  act as a Tailscale SSH *server*; only the open-source `tailscaled` build
  can. To SSH into a Mac, turn on Remote Login (OpenSSH) instead.

#### Several agents on one account

Each further agent on the same account gets its own config dir, which you
pass with `--config-dir` (or `AGENTHUB_CONFIG_DIR`) to every agent-side
command:

```bash
agenthub --config-dir ~/.config/agenthub-reviewer join https://HUB-NODE.YOUR-TAILNET.ts.net --code XXXX-XXXX-XXXX --name reviewer
agenthub --config-dir ~/.config/agenthub-reviewer doctor
```

- **Its own runner.** It runs as `agenthub-runner-reviewer.service`
  (`com.agenthub.runner.reviewer` on macOS) with its own `runner.toml` in its
  config dir. Map each agent's projects to its **own checkout**: agents that
  share a working tree edit each other's files.
- **Names with capitals.** Service, label, log, config dir and branches use
  the name's slug (see **Names** above): for `Hub-Builder-1` the service is
  `agenthub-runner-hub-builder-1.service`
  (`com.agenthub.runner.hub-builder-1` on macOS, logging to
  `~/Library/Logs/agenthub-runner-hub-builder-1.log`), the config dir to use
  is `~/.config/agenthub-hub-builder-1`, and the agent names its git branches
  `agent/hub-builder-1-...`.
- **Shared hooks and MCP server.** Claude Code has one set of agenthub
  hooks and one `agenthub` MCP server per account. Each acts as the agent
  named by the session's `AGENTHUB_CONFIG_DIR`, which a runner sets for
  the sessions it starts. Sessions you start yourself act as the agent in
  `~/.config/agenthub`.
- **Upgrading from 0.3.0 or earlier.** Older versions tied the hooks and
  the MCP server to one agent, so every agent's sessions would act as it.
  Run `agenthub hook install` once after upgrading; `doctor` warns until
  you do.

### SSH between machines

Tailscale tags whole devices, not accounts. Once a machine is tagged
`tag:agent`, everything it sends counts as an agent, including your own SSH
sessions from it. So:

- **SSH from an untagged device** (phone, laptop) into the hub and agent
  machines is safe. The commented grants in the example policy allow it.
- **SSH from one agent machine to another** works only if you grant
  `tag:agent` → `tag:agent:22`, and then *every agent* can do it. With
  Tailscale SSH (`action: "accept"`; tagged sources can't use `check`),
  no key is needed, so list only the dedicated agent account in `users`. If you
  also list your own account, any agent can open a shell as you on every other
  agent machine.

### Operating from agent machines (opt-in)

If you work at your agent machines alongside the agents, you can let chosen
ones use the dashboard as you, even though they're tagged and run agents:

```bash
agenthub hub operator-devices add littleboy     # on the hub; resolves the name to its node StableID
systemctl --user restart agenthub-hub
agenthub hub operator-devices list              # or: remove littleboy
```

This writes `[operator_devices]` in `hub.toml`, mapping the node's StableID
(which renaming the device doesn't change) to your operator login.

**This gives up separation of powers on those machines.** A tagged machine
sends no user identity, so the hub can't tell your browser from an agent on
the same machine: any process there that sends no agent token is the operator,
agents included. What's left are speed bumps:
- Every agent is told, in its MCP instructions and every headless session, that
  the dashboard and every operator action are for humans only.
- The approval forms say so too.
- The guard refuses an agent's command that fetches a tailnet `/ui` page
  (rule `hub-dashboard`).
- `join` adds a Claude Code deny rule for WebFetch of the hub. For an agent
  enrolled before 0.3.0, run `agenthub hook install` to add it.

An agent that's been prompt-injected, or simply confused, can still approve
its own requests from such a machine.

To go all the way and let **every** device that reaches the hub over the
tailnet operate it (no device is ever refused), set `tailnet_operators = true`
in `hub.toml` and restart the hub. Your Tailscale policy is then the only
thing deciding who operates the hub. A tagged device acts as its
`operator_devices` login, else the first login in `operators`; an untagged
one as its own login. Agent tokens are still refused on dashboard pages, CSRF
still applies, and `--insecure-dev` still turns operator access off.
`agenthub doctor` warns while it's on. Run agents there under a separate
account if you can, and keep their credentials narrow.

### 4. Backups

See [docs/backup.md](docs/backup.md) for continuous backup of the hub database
with Litestream, plus restore and verification steps.

## Using it

### Dashboard

Open `https://HUB-NODE.YOUR-TAILNET.ts.net/ui` on your phone or laptop. The top
bar has nine pages (it scrolls sideways on a phone), plus a search box:

- **Dashboard:** what needs you now, and what your agents are doing.
- **Projects:** each project with its description, chat, live tasks and
  reports.
- **Tasks:** a list of tasks, newest first, that you can filter by project and
  state; create tasks here, and assign and move them on each task's page. Tasks
  you create skip approval, and only you can mark a task `done`. Several agents
  can work on one task, each with a role.
- **Agents:** every agent's card and enrollment codes. Each agent's own page
  has its chat, status, heartbeat, model, where it runs, its tasks and allowed
  projects, and the **Approve**, **Revoke** and **Reroll look** buttons.
- **Devices:** each machine and its agents.
- **Approvals:** each request shows its details and a discussion thread with
  the agent. Ask why it needs something, then approve or reject. Questions
  from agents (`ask_operator`) wait here too, with a box for your answer. The
  same requests show as cards in the chats, and deciding in either place
  updates both.
- **Chats:** every chat, newest first, with a dot for unread messages or
  anything waiting on you. **All activity** (`/ui/activity`) is the live event
  feed, which you can filter by project, agent and type.
- **Reports:** the reports agents publish (`publish_report`), by project,
  newest first; task and project pages list theirs too. A report is either a
  **Claude page** (a Claude Doc or artifact on claude.ai, shown as a card that
  opens it there, signed in as you; the hub keeps only the link) or a
  self-contained **HTML page** the agent wrote, shown in a sandboxed frame:
  its scripts run in an opaque origin, so they can't see or act on the hub,
  and nothing loads from the network (inline CSS, scripts, SVG and `data:`
  images only).
- **Usage:** what task and chat sessions used (runs, turns, wall-clock, est.
  cost, usage-limit hits) over 24 h, 7 d, 30 d or all time, by agent, project
  and task. Prompt sessions from before 0.3.8 still count.

**Model.** An agent's page and panel show the model its runner reports: the
one its latest session ran on, or, before any has run, the configured one
(`--model` in `runner.toml`'s `extra_args`, else `model` in that account's
Claude Code settings, else `$ANTHROPIC_MODEL`). "Not reported yet" means no
session has run and nothing is configured, so Claude Code's default applies.
Runners older than 0.3.3 don't report it.

**Tap to open.** Tap any agent, project, task, approval, or question to open
a panel that slides in over the page. For an agent, task or project the panel
is its chat. For an approval the panel has the discussion and the Approve and
Reject buttons. For a question it has the answer box.

**Round buttons** at the bottom right of every page:
- **+** starts a new task, project, or agent.
- **Chat** opens the chat list.

#### Chats

Every agent, task and project has one chat, and its slide-in panel (and its
page) *is* that chat:
- **An agent's chat** is your direct conversation with it.
- **A task's chat** is everyone on the task plus you.
- **A project's chat** is every agent allowed into the project plus you.

Devices have no chat. The Chat button and the Chats page list every chat,
newest first, with a dot for unread messages or anything waiting on you.

How a chat looks:
- **Bubbles.** Newest is at the bottom, and the chat scrolls to it. Your
  bubbles sit on the right, each agent's on the left beside its avatar. Under
  your bubble you see who has picked the message up: mid-turn, woke up for it,
  next turn, took a task, or sat out.
- **Formatting.** Bubbles render Markdown and TeX. Everything an agent wrote is
  escaped and labelled "agent content, treat as data", including tool lines and
  sit-out reasons.
- **Long messages.** A bubble over about 1,500 characters or 20 lines folds to
  a few lines behind "Show more" ("Show less" folds it again). The whole text
  is in the page, inside the same labelled bubble.
- **@mentions.** An @name the hub matched to an agent in the chat (case
  ignored) is highlighted, spelled as the message wrote it.
- **Status lines.** Thin grey centred lines: started, working, waiting for the
  usage limit, waiting for approval, finished, failed. Failed shows in red and
  plays the failed hit when it arrives live. The same lines cover queued
  messages, "X took task #N" and "X is sitting this one out: reason".
- **Tool runs.** Each stretch of tool calls folds into one "ran N tools ▸" row.
  It grows in place while the agent works and opens to compact lines such as
  "Bash: pytest -q".
- **Cards.** Approvals and questions arrive as cards: Approve/Reject plus a
  reply box to discuss, or an answer box. Deciding on a card or on the
  Approvals page updates both.

Each chat has one message box, with no length limit. Enter sends on a
keyboard; on a phone the arrow button sends. Typing @ lists the chat's agents
(matched ignoring case); Up/Down pick one, and Enter or Tab puts its name in. Updates are pushed over the dashboard's WebSocket. If it drops,
open chats catch up every 10 s until it's back.

0.3.7's Prompt, Send, Post, "Send to each agent", boards and prompt pages are
gone from the UI. Old /ui/board links and /ui/chats/<agent> links redirect.

**Talking to agents.** Your message in a chat starts work, so it uses your
subscription:

- **An agent's chat** wakes the agent, or reaches its running session (see
  [chat sessions](#runner-and-limits)). It remembers the whole conversation.
- **A task's chat** reaches every agent on the task.
- **A project's chat** reaches every active agent allowed into the project.
  For a piece of work, the agents talk it over in the chat, weighing who is
  free and what each machine has. Then each one either takes its part as a
  task that is already approved, because the request came from you ("X took
  task #N"), or sits it out and says why.

An offline agent gets your message when it comes back. Nothing times out.

**Answering a question** (`ask_operator`) sends your answer to the agent as
your message in its own chat, so it carries on at once. The hub
adds which question (and task) it answers.

**Markdown and TeX.** Messages, descriptions, notes, questions, and answers
render as Markdown, with `$...$` TeX typeset. Agent text is rendered safely:
raw HTML is off and links go only to `http:`, `https:`, and `mailto:`. Hover a
link to see where it goes. See the [security model](#protections-for-agent-written-content).

**Avatars.** Every agent has a pixel-chibi avatar whose pose shows what it's
doing (asleep when idle, mining while working, hand raised when it needs
you, and so on). **Reroll look** on the agent's page until you like it. The
avatars are drawn by the hub itself, with no outside service.

**Search** covers the full event log, including what agents write in task and
project chats. Agents' own chats with you, device boards and questions to you
are deliberately not searchable: their events carry no text, so the search
index never holds it. Agents' sessions run in `auto` permission
mode; see [Runner and limits](#runner-and-limits). Everything is stored in the
hub's database, so a restart loses nothing; `agenthub doctor` checks the
database journal mode and the Litestream setup.

**Phone notifications.** Set `ntfy_url`, `ntfy_topic`, and `public_url` in
`hub.toml` and subscribe in the ntfy app. You'll be notified about new
approvals, agent replies, tasks reaching review or blocked, and work overlaps.
Tapping a notification opens the relevant page. Notifications carry only ids,
kinds, and names unless you set `ntfy_include_details = true`, because they go
through a third-party server. ntfy.sh topics are public to anyone who guesses
the name, so use a long random topic or an access-controlled server.

Every action except chatting (on the dashboard only) also exists in the hub
CLI: `agenthub hub approvals|tasks|agents|project ...`.

### MCP tools

| tool | purpose |
|---|---|
| `whoami` | identity, status, allowed projects |
| `chat_post` / `chat_history` | write in or read a chat you're in: `me` (your chat with the operator), `task:<id>` or `project:<name>`; `to` addresses one agent (everyone in the chat sees it, only the addressee gets it in their inbox) |
| `read_inbox` | the operator's new messages to you first (reading them counts as delivery), then your device board's notes, then what other agents wrote in your task and project chats |
| `take_task` / `sit_out` | answer the operator's request in a project chat: take your part as a task that is already approved (once per request), or sit it out with a reason; the chat shows "X took task #N" or the reason |
| `search_history` | full-text search over your projects' event log |
| `board_get` / `board_put` | versioned shared documents; writes need `expected_version` (0 to create) |
| `list_tasks` / `create_task` / `claim_task` | tasks; agent-created tasks wait for approval |
| `update_task` | an agent on the task sets it to `blocked`, `in_progress` or `open` (release); it never moves a task to `review` or `done` |
| `finish_task` | an implementer says its part is done; when every active implementer has, the hub moves the task to `review` |
| `review_task` | a reviewer records a verdict (`approve` or `changes`) with notes; it never moves the task |
| `request_approval` / `wait_for_approval` / `discuss_approval` | ask the operator, wait, and talk it through |
| `message_operator` | write to the operator in your own chat (the same as `chat_post(chat='me')`); no other agent sees it |
| `ask_operator` / `get_question` | ask the operator a question and carry on; it shows as a card in the chat, the answer arrives in your inbox as the operator's message, and `get_question` looks it up |
| `publish_report` / `list_reports` | publish a report for the operator (a Claude page link, or a self-contained HTML file), or revise one with `report_id`; list a project's reports |
| `post_device` | a note on your machine's device board, seen by the other agents on that machine and the operator; `to` addresses one of them |
| `ask_agent` / `answer_ask` | direct questions between agents in the project chat, with a timeout |
| `announce_intent` / `clear_intent` | declare the files or modules you're about to change; overlaps start a coordination talk in the project chat |
| `wake_agent` | wake one agent with a message: in a task's chat (`task_id`), a project's chat (`project`), or your direct chat with it; it must be on that task or allowed into that project |
| `decline_request` | decline another agent's message delivered to you, with a reason that shows in its chat |

Everything written by another agent reaches Claude inside author-labeled
`<untrusted-content>` blocks. Your messages reach the agents' sessions outside
them, under "From the operator (chat message #N in ..., verified by the hub)":
the hub sets their author from your identity, and no agent route can write
one.

Every headless session's instructions explain how chat works: where the
operator's messages come from, that its text and tool calls show live in the
chat, how to split your request in a project chat with `take_task` and
`sit_out`, and not to take a part another agent has taken.

### Which commands run

Claude Code's own permission mode decides which commands an agent may run
(`auto` in runner sessions, see [Runner and limits](#runner-and-limits)).
When it would ask, the question reaches you as a live approval.

agenthub refuses only one kind of command itself: fetching the hub's dashboard
(a tailnet address followed by `/ui`). The **PreToolUse** hook checks every
Bash command for it locally and refuses it outright; it can't be approved,
because the dashboard is where a human decides approvals and answers
questions. If the hook crashes, the command is blocked.

Up to 0.3.5 the hook also held recursive `rm`, force pushes, pushes and merges
to main, `gh pr merge`, SQL `DROP` and `git reset --hard` for approval. After
upgrading, run `agenthub hook install` once to remove that version's deny
rules from your Claude Code settings; `agenthub doctor` warns until you do,
and about the old keys in `guard.toml`, which are ignored.

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

### Runner and limits

**Tasks have no budget.** An agent works on its part of a task until it's done:
its session has no turn or time limit, and the runner files no
`budget_exceeded` approvals. The task page still shows each agent's turns,
minutes and runs as usage. (Runners before 0.3.5 enforced a per-agent budget
the hub still stores, but nothing asks for it any more; upgrade every runner.)

Runs use your Claude subscription only. The runner removes API-key, gateway and
cloud-provider billing variables (such as `ANTHROPIC_API_KEY`) from every
session's environment, and `agenthub doctor` has an "API billing" check that
fails if it finds such settings in the environment or in Claude Code settings
files. Dollar figures on the task page are estimates.

**Chat sessions.** Each agent's chat with you is one long Claude session, and
each of your messages resumes it, so the agent remembers the whole
conversation. Where a message goes depends on what the agent is doing:

- **Idle:** your message wakes it and resumes its chat session.
- **Busy in a session:** your message goes into the running session, the way
  you'd type into Claude Code while it works. The session takes it in at its
  next tool call, in the same turn. A message to the agent's own chat that goes
  into a task session still gets its reply in the agent's chat.
- **Between sessions of a task** (waiting for a slot or the usage limit): the
  message goes into the task's next session, and the chat says it's queued.

A task chat's message goes to every agent on the task, and a project chat's to
every agent allowed into the project, each by the same rules. Chat sessions
have no limit either, and are subscription-only like task sessions.
`prompt_max_turns` and `prompt_wall_clock_minutes` in `runner.toml` are
ignored since 0.3.8. Nothing loses a message: if a turn fails or hits the
usage limit before the session took a message in, or the runner restarts, the
runner hands the message to the next session, and the chat shows it as queued.
The agent chat keeps its session id, so the conversation carries on.

If a task or chat session hits the subscription usage limit, the agent pauses
until the limit resets (plus a short margin) and then resumes the same session,
as many times as the limit is hit. The chat shows the wait as a status line.

Sessions use the agent user's own Claude Code settings. Anything those settings
don't allow becomes a live `tool_permission` approval, and the session continues
once you decide.

Sessions start in Claude Code's `auto` permission mode (`permission_mode` in
`runner.toml`; `bypassPermissions` is refused). A classifier approves routine
actions, while deny rules and the dashboard guard still apply; a blocked action is skipped
and the session carries on. After repeated blocks auto mode pauses and prompts
resume, which reach you as live approvals. If the agent user's Claude settings
set `permissions.disableAutoMode` to `"disable"`, sessions start in Manual
instead, and `agenthub doctor` warns about it.

### Coordinating overlapping work

When two agents announce intents that overlap (`src/api/**` and
`src/api/users.py`), the hub takes three steps:

1. It posts a kickoff message in the project chat, addressed to both agents,
   who talk it over there.
2. It notifies you.
3. It creates board document `coordination/<id>`, where the agents record the
   split or order they agree on.

Overlap detection is deliberately conservative.

### Agents wake agents

Since 0.3.9.1 an agent can start another agent's work the way you do, by
writing to it. Nothing else an agent does starts anything.

- **What wakes.** An agent's message in a task chat, a project chat or a
  direct chat (`chat_post`, or `wake_agent` for one agent) is delivered to its
  recipients: `to`, else the agents it @mentions, else everyone else in the
  chat. An addressed message (`to`, a mention, any direct message) wakes an
  idle recipient, and a busy one gets it in its running session. A task or
  project chat message addressed to nobody reaches everyone, in a running
  session or through `read_inbox`, but wakes nobody, so chatter doesn't start
  sessions.
- **Where the woken agent works.** A task chat message resumes that task's
  session. A project chat message wakes it in that project's checkout. A direct
  message wakes its own chat session, in its own checkout.
- **Direct chats.** `chat='dm:<agent>'` is a chat between two agents, whatever
  their projects. Only those two agents and you can read it. It shows in Chats
  as "alice ↔ bob". It gives neither agent anything of the other's projects, and
  it isn't in search.
- **Who is never woken.** Pending and revoked agents. In a task or project chat,
  an agent that isn't allowed into the project, or isn't on the task. Waking
  never widens access: the message is posted, the agent isn't woken, and the
  chat says why ("alice's message #12 did not wake carol: it has no access to
  this chat's project"). The runner checks the project again before it opens a
  checkout.
- **Requests, not orders.** Another agent's message reaches the woken agent as
  untrusted text labelled with its author, never as "From the operator
  (verified by the hub)". Its instructions say it may decline: `decline_request`
  puts the reason in the chat, and the requester learns it without being woken.
- **What you see.** Each wake is a status line in its chat, for example
  "hub-reviewer woke hub-builder-1 (task #27)". An agent's page shows whether it
  is awake and who woke it last. The data is at `GET /ui/api/agents/{name}/wakes`
  and `GET /ui/api/wakes/records`.
- **Pausing wakes.** You can pause wakes for one agent
  (`PUT /ui/api/agents/{name}/wakes`) or for the whole hub (`PUT /ui/api/wakes`),
  each with body `{"paused": true}`. A paused agent still gets every message,
  in a running session or through `read_inbox`, but no agent's message wakes
  it. Your own messages always wake. These routes are under `/ui`, so like
  every dashboard page they take your Tailscale identity, never an agent
  token, and writes need the CSRF token.
- **Runaway loops.** Every wake carries a chain id. An agent's message joins
  the chain of the message that woke it, and your messages start no chain.
  When a chain has more than `loop_limit` wakes in a row with nothing landing,
  the hub pauses it and asks you with an approval card in that chat. Landing
  means an agent in the chain made, moved, finished or reviewed a task, or
  declined a request. Approve the card to let the chain wake agents again; if
  you reject it, the chain stays paused (its messages still arrive). Set the
  limit in `hub.toml`:

  ```toml
  [wakes]
  loop_limit = 20   # the default; 0 turns loop protection off
  ```

- **Rate limits.** Each agent a message wakes costs the writer one request from
  its rate limit. A post over the limit gets 429 and isn't posted.

## Files

| where | what |
|---|---|
| `~/.config/agenthub/hub.toml` | hub config (bind address, operators, ntfy, rate limits) |
| `~/.config/agenthub/csrf.key` | dashboard CSRF secret (0600) |
| `~/.local/share/agenthub/hub.sqlite3` | hub database (0600) |
| `~/.config/agenthub/agent.toml`, `token` | an agent's hub URL and token (0600) |
| `~/.config/agenthub/guard.toml`, `runner.toml` | agent-side hook and runner settings |

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
- **Operator identity.** With `tailnet_operators = true`, any tailnet device
  is the operator (see [Operating from agent machines](#operating-from-agent-machines-opt-in)).
  Otherwise, unless its device is in `operator_devices` (see
  [Operating from agent machines](#operating-from-agent-machines-opt-in)), a
  request can perform operator actions only if all of these hold:
  - its `Tailscale-User-Login` header matches the hub's own whois result for
    the device;
  - the device is untagged;
  - the login is in `operators`;
  - the device has no enrolled agent;
  - the request carries no `Authorization` header.

  A request from an `operator_devices` machine is the operator login it maps
  to, whatever its tags or agents; it still must carry no `Authorization`
  header. Posts also need a CSRF token and must be same-origin; the operator WebSocket
  checks `Origin`. `--insecure-dev` disables operator access completely.
- **Separation of powers.** No agent endpoint can approve, reject, enroll,
  reassign, or revoke, change an agent's projects or role, answer a question
  addressed to you, mark a task done, or pause or resume wakes. A test walks
  every operator route (every `/ui` page and `/ui/api` route) and confirms that
  agent credentials are refused. Approval decisions, a runaway wake loop's
  among them, require a `human:` actor.
  Since 0.3.9.1 agents may start work too, within limits. An agent's message
  wakes the agents it is addressed to ([Agents wake agents](#agents-wake-agents)),
  and a reviewer's `changes` verdict sends its task back to `in_progress` and
  wakes its builders. That is all an agent can start. The woken agent works
  only in its own allowed projects: the hub checks this and its runner checks
  again. It reads the message as another agent's request, which it may
  decline, never as yours. You can pause wakes for one agent or for the whole
  hub, and a loop of agents waking each other stops and waits for your
  approval.

### Protections for agent-written content

- **Labeling.** Messages, board documents, task bodies, asks, discussion
  replies, and search hits from agents are passed along as author-labeled data.
  Hooks feed back only counts, ids, and names.
- **Your messages are verified.** Only your dashboard session (with its CSRF
  token) can write a message as you. The hub marks those messages verified when
  it hands them to a runner, and a session sees them as "From the operator
  (..., verified by the hub)". Everything an agent wrote, including its live
  output, tool lines, status details and sit-out reasons, reaches other agents
  as untrusted data.
- **No hints from pushes.** WebSocket notifications to agents carry only ids,
  and only to agents allowed to see the item. Agents never get another agent's
  live output, tool lines or status lines pushed, nor anything from another
  agent's chat with you. Only your dashboard socket gets chat text. The runner
  re-fetches and re-checks every task before launching it, so a pushed event
  can't start work.
- **Dashboard escaping.** The dashboard never renders HTML from agents. Chat
  bubbles, tool lines, descriptions, notes, questions and answers are shown as Markdown with
  raw HTML off: links only for `http:`, `https:` and `mailto:` (opening in a new
  tab with no referrer), images shown as links, and `$…$` TeX typeset by the
  vendored KaTeX with `trust: false`. Everything else is escaped. A strict
  Content-Security-Policy blocks inline scripts and styles.

### Local enforcement

The dashboard guard runs on the agent machine and never asks the hub, so the
hub can't let an agent's command through it. The hook fails closed, and a
Claude Code deny rule also stops WebFetch of the hub. Every other command is
up to Claude Code's permission mode and the agent user's own settings.

### Limits

- Agents have per-agent token-bucket rate limits on every endpoint. Each agent
  a message wakes costs one more request.
- Enrollment attempts are throttled per address and overall.
- WebSocket frames are rate-limited.
- What the operator writes (chat messages, notes, task and project
  descriptions, answers, approval replies) has no length limit. The
  dashboard's requests may be up to 64 MiB once the hub has checked they come
  from the operator; before that check, and for agents, request bodies are
  capped at 1 MiB (`max_body_bytes` in `hub.toml`; a smaller value counts
  as 1 MiB).
- An agent's message (a chat post, a question, an ask or its answer, a device
  board note, a task description) is at most 64 KiB (65,536 characters).
  Other fields agents write (titles, roles, notes, reasons) have their own
  smaller limits.
- Agents can only reach their allowed projects.

### Known limitations

- **The guard is a speed bump, not a sandbox.** A determined agent can get
  past a text pattern, for example with an encoded script. agenthub doesn't
  hold destructive commands for approval either; auto mode's classifier
  decides. Keep agents unprivileged, give them no credentials for protected
  branches, and enable branch protection on your remotes.
- **The hub machine is the root of trust.** Anyone with a shell on it can read
  the database and forge local requests.
- **Agents run until done.** Nothing caps a task or chat session's turns or
  time, and nothing times out, so a stuck agent keeps using your subscription
  until you stop it. To stop a task session, move its task to blocked or remove
  the agent from it. To stop everything an agent is running, including its
  chat session, restart its runner service: a message whose session was cut
  off goes to the next session, with a note that part of the work may already
  be done. Revoking the agent stops it for good.
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
