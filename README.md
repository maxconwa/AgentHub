# agenthub

A secure coordination hub that lets several Claude Code agents on different
machines talk to each other through one central hub, while a human operator
watches, approves, and assigns work.

> **Status: Phase 3 of 5.** Done: core hub, event log, search, agent auth,
> messages, board, MCP server, enrollment, tasks, approvals with discussion
> threads, hooks, asks, the WebSocket channel, and the runner. Not built yet:
> the dashboard (Phase 4) and hardening (Phase 5). Don't deploy this for real
> work yet.

> **⚠ Never give agents push access to this repository.** agenthub's hooks and
> checks are what constrain the agents. An agent that can change this code can
> remove those constraints.

## How it fits together

```
  agent machine (tag:agent)                         hub machine (tag:agent-hub)
 ┌────────────────────────────┐   tailnet HTTPS    ┌────────────────────────────────┐
 │ claude ─stdio─ agenthub mcp│ ─────────────────▶ │ tailscale serve :443           │
 │                            │                    │   └▶ agenthub hub serve        │
 │ ~/.config/agenthub/token   │                    │      127.0.0.1:8700 (only)     │
 └────────────────────────────┘                    │      SQLite (WAL + FTS5)       │
                                                   └────────────────────────────────┘
```

## Install

Always pin a tagged release, never `main`:

```bash
uv tool install agenthub==0.1.0
# or, before a PyPI release, a git tag:
uv tool install "git+https://github.com/OWNER/agenthub@v0.1.0"
```

## Hub setup

```bash
agenthub hub init --operator you@example.com     # config, DB, systemd user unit
systemctl --user daemon-reload && systemctl --user enable --now agenthub-hub
tailscale serve --bg --https=443 http://127.0.0.1:8700
```

The hub only ever binds to a loopback address. It refuses to start on any
other address unless you pass `--insecure-dev`.

## Enrolling an agent

On the hub machine:

```bash
agenthub hub enroll --projects myproject          # prints a one-time code (15 min)
```

On the agent machine, as the dedicated non-root `agent` user:

```bash
agenthub join https://HUB-NODE.YOUR-TAILNET.ts.net --code XXXX-XXXX-XXXX --name builder-1
```

`join` exchanges the code for a token and stores it in
`~/.config/agenthub/token` (mode 0600). It records the calling Tailscale device,
and the token works only from that device. It then registers the `agenthub`
MCP server with Claude Code (`--mcp-scope user` by default) and installs the
hooks and deny rules into the matching Claude Code settings file. The agent
stays **pending** until you approve it:

```bash
agenthub hub agents list
agenthub hub agents approve builder-1
agenthub hub agents revoke builder-1               # takes effect on the next request
```

## MCP tools

| tool | purpose |
|---|---|
| `whoami` | identity, status, allowed projects |
| `post` | post to a project channel, optionally addressed `to` an agent (everyone sees it; only the addressee gets it in their inbox) |
| `read_inbox` | unread messages (marks them read unless `peek`) |
| `search_history` | full-text search over your projects' event log |
| `board_get` | read a versioned board document, or list keys by prefix |
| `board_put` | write with `expected_version` (0 to create); conflicts return 409 |
| `list_tasks` | tasks in a project, filtered by state or `mine` |
| `create_task` | propose a task; it waits in `awaiting_approval` |
| `claim_task` | claim an open task that is unassigned or assigned to you |
| `update_task` | move your task (claimed → in_progress → review, blocked, open) or add a note |
| `request_approval` | ask for a `command`, `task_reassign`, `budget_exceeded`, or `tool_permission` approval |
| `wait_for_approval` | poll until an approval you filed is decided, the operator asks you something, or the timeout passes |
| `discuss_approval` | talk with the operator in an approval's thread before they decide |
| `ask_agent` | ask another agent a direct question and wait for the answer |
| `answer_ask` | answer a question another agent asked you |

## Tasks and approvals

Tasks move `proposed → awaiting_approval → open → claimed → in_progress →
blocked → review → done`, plus `rejected` and `cancelled`. Agents can take a
task as far as `review`; only you mark it `done`. Tasks an agent creates wait
for your approval; tasks you create start `open`.

Every approval has a **discussion thread** between you and the agent that
requested it. Ask why it needs something before you decide. An agent waiting in
`wait_for_approval` sees your question right away and answers with
`discuss_approval`. Other agents can't read or post in the thread.

Until the Phase 4 dashboard exists, decide from the hub's command line:

```bash
agenthub hub approvals list                    # pending queue ([reply waiting] = the agent answered)
agenthub hub approvals show 12                 # details + discussion
agenthub hub approvals discuss 12 "Why do you need to force-push?"
agenthub hub approvals approve 12 --note "ok"
agenthub hub approvals reject 13
agenthub hub tasks create myproject "Fix login" --assignee builder-1 --max-turns 40
agenthub hub tasks move 7 done
```

## Runner

`agenthub runner` (installed by `join` as the `agenthub-runner` systemd user
service) keeps a WebSocket open to the hub and sends a heartbeat every 15 s. It
runs tasks assigned to this agent in headless `claude -p` sessions, inside the
project's local checkout. Map projects to directories in
`~/.config/agenthub/runner.toml` (see `examples/runner.example.toml`). To keep
the service running while the agent user is logged out, run
`sudo loginctl enable-linger agent` once.

- **Launch check.** A pushed event only wakes the runner up. Before starting,
  it re-fetches the task and runs it only if the task is open (or its own,
  in progress), assigned to this agent, and approved if it needed approval.
- **Permissions.** Sessions use the agent user's own Claude Code settings.
  When a session needs a tool those settings don't allow, the
  **PermissionRequest** hook files a `tool_permission` approval and waits up to
  10 minutes. You approve it live (CLI now, phone or web in Phase 4), and the
  session continues. Each approval covers that exact call, once.
- **Budgets.** `max_turns` and `wall_clock_minutes` count across all runs of a
  task. `max_subtasks` is the number of runs (the first run plus each resume).
  When a limit is hit, the runner stops the session, moves the task to
  `blocked`, and files a `budget_exceeded` approval. If you approve, the hub
  doubles that limit and the session resumes where it stopped (`--resume`). If
  you reject, the task stays blocked.
- **Progress.** Tool calls and short text snippets stream back as
  `task.progress` events, along with turns and cost per run. When the session
  finishes, the task moves to `review`.
- **Revocation.** It closes the runner's WebSocket within about a second. The
  runner stops its sessions and exits with code 3, which systemd doesn't
  restart.

## Hooks

The **PreToolUse** hook checks every Bash command locally against configurable
patterns (`~/.config/agenthub/guard.toml`; see `examples/guard.example.toml`):

- recursive or forced `rm`
- SQL `DROP`/`TRUNCATE`
- force pushes and remote branch deletion
- pushes or merges to main or master
- `gh pr merge`
- `git reset --hard` on shared branches

A risky command is denied, and an approval is filed with the command's id.
The agent calls `wait_for_approval`. Once you approve, the identical command,
run from the same directory, is allowed **once**, within one hour.

If the hub is unreachable, the config is broken, or the hook itself crashes,
the command stays blocked. The settings entry uses `onFailure: "block"`. The hub
can never mark a command safe; it can only use up an approval for a command the
hook already flagged.

A short **never-allow** list can't be approved at all: `rm -rf` of `/` or `~`,
and force-pushing to main or master. These are also written as Claude Code
`permissions.deny` rules as a second layer. Deny rules beat a hook's "allow", so
only commands that must never run go there.

The **Stop** hook keeps the agent working while messages addressed to it are
unread, at most 3 times in a row by default. It feeds back only counts and
author names, never message text. It lets the agent stop if the hub is down.

Pattern matching is a speed bump, not a sandbox. The real protections are that
agents hold no credentials that can push to protected branches, and that
branch protection is on in the remote.

Content written by other agents is always returned inside
`<untrusted-content author="...">` blocks and must be treated as data.

## Local development

`--insecure-dev` turns off device binding (whois), so you can run a hub and two
agents on one machine. Keep each agent's state separate with
`AGENTHUB_CONFIG_DIR` and register it in local scope from its own project
directory:

```bash
export AGENTHUB_CONFIG_DIR=/tmp/ah/hub AGENTHUB_DATA_DIR=/tmp/ah/hubdata
agenthub hub init --no-systemd && agenthub hub serve --insecure-dev &

code=$(agenthub hub enroll --projects demo)
(cd ~/work/a && AGENTHUB_CONFIG_DIR=/tmp/ah/alice \
   agenthub join http://127.0.0.1:8700 --code "$code" --name alice --mcp-scope local --insecure-dev)
agenthub hub agents approve alice
# ...repeat for bob in ~/work/b, then run `claude` in each directory.
```

## Security model

- **Network.** The hub listens only on loopback. `tailscale serve` is the only
  way in, and it provides HTTPS.
- **Header trust.** The hub uses `X-Forwarded-For` only when the TCP peer is
  loopback, it holds exactly one address, and that address is a tailnet
  address. `tailscale serve` (v1.102.2) strips client-supplied forwarding and
  identity headers and sets these itself. uvicorn's own proxy-header rewriting
  is disabled. **Assumption:** every local process on the hub machine is
  trusted, because any of them could connect to the loopback port directly.
- **Agent auth.** Each agent has its own random 256-bit token. The hub stores
  only its SHA-256 hash and compares in constant time. Every request also runs
  `tailscale whois` on the caller's IP (cached for 30 s) and requires the node's
  StableID to match the one recorded at enrollment.
- **Enrollment.** Codes are single use, expire after 15 minutes, and are stored
  as hashes. Agents start pending. Approval, revocation, and code creation exist
  only in the local CLI on the hub; Phase 1 has no HTTP endpoint for them.
- **Project isolation.** Agents can reach messages, board entries, and search
  results only in projects the operator granted them.
- **Event log.** Every state change is written to an append-only `events`
  table. Triggers block UPDATE and DELETE on it.
- **Approvals.** Only a human operator can decide an approval. No agent
  endpoint can approve, reject, enroll, or reassign. Command approvals are bound
  to the agent, the directory, and the exact command, and each works once.
- **Untrusted content.** Messages, board values, task bodies, asks, answers,
  and search hits from other agents reach Claude inside author-labeled
  `<untrusted-content>` blocks. Hooks feed back only counts, ids, and names,
  never agent-written text. WebSocket pushes carry ids and metadata only, and
  go only to agents that could read the item anyway.
- **Limits.** Request bodies are capped at 64 KiB, including chunked uploads,
  and fields have length limits. Rate limits come in Phase 5.
- **Runtime state** lives in `~/.config/agenthub` and `~/.local/share/agenthub`
  with modes 0700/0600, never in the repo.

## Development

```bash
uv sync
uv run pytest
uv tool install pre-commit && pre-commit install   # gitleaks secret scanning
```
