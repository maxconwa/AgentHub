# Dashboard redesign: who is doing what, where

Status: approved by the operator · 2026-10-21

## Why

The operator runs several Claude Code agents on several machines and checks on
them from a phone. The first thing they want at a glance is **which agents are
working on which tasks, for which projects, on which devices, and in what
state**. The current dashboard (Phase 4) is organized around approvals and a
raw event feed, and it can't answer that question without digging.

The redesign answers that question on the Dashboard, the first page you land on. It gives every project, task
and agent its own page with a description and conversation. It lets the
operator talk to agents from wherever they are looking. And it gives agents a
face: a pixel chibi whose pose shows its state.

## What exists today (context for the builder)

agenthub is built through Phase 5. Relevant pieces:

- **Hub:** FastAPI + SQLite. Its tables include `projects` (name,
  description), `tasks` (single `assignee`), `messages` (project, channel,
  author, optional single `to`), `approvals` with discussion threads, `asks`,
  `intents` with coordination threads and `coordination/<id>` board docs, and
  `agents` (device name, IP, StableID, `last_heartbeat`, `connected`).
- **Dashboard:** Jinja2 + HTMX under `/ui`, behind Tailscale operator
  identity, CSRF, and a strict CSP. It has a live feed over the operator
  WebSocket (`/ui/ws`), and the event bus pushes rendered rows.
- **Runner:** keeps a WebSocket open to the hub, sends a heartbeat every 15 s,
  runs tasks in headless `claude -p` sessions, and resumes them with
  `--resume`.
- **Untrusted content:** anything agents wrote is wrapped in labeled
  `<untrusted-content>` blocks for agents, and escaped and labeled in the
  dashboard.
- **Avatar prototype:** a 48×48 pixel-chibi generator prototype (JavaScript)
  in the Design canvas the operator reviewed
  (`https://claude.ai/artifact/W7nwqX8RJ82HPbZnAn37iR`, file
  `project/PixelGrid.dc.html`). It has 8 hairstyles, 11 hair colours, 6 skin
  tones, 6 eye colours, 4 mouths, 5 tops, and 8 accessories plus none. The
  operator approved its look.

## Goals (all decided by the operator)

### Pages across the top

The dashboard has **nine separate pages**, linked from a bar across the top
(operator decision 2026-10-21: separate pages, not one page with tabs or
menus). The approvals red badge stays on its link.

0. **Dashboard** (the primary page, where `/ui` lands). At a glance: the
   **approvals** waiting on the operator (newest first, each with its agent's
   avatar, opening its approval page) and **every agent** with its avatar,
   status, current task and device (opening its agent page). Each section
   links to its full page.
1. **Projects.** Each project being worked on has a collapsible list of its
   tasks. Under each task: its agent(s), each with its **role on that task**,
   **device**, and **status**. The hierarchy is
   project → task → agent(s) → device and status. Projects open their
   project page.
2. **Tasks.** A flat list. Each row shows the task, its project, and its
   agents (with roles). Tasks open their task page.
3. **Agents.** Each agent, the device it runs on, the **directory it is
   working in**, its current task and project, its role there, and its
   status. Agents open their agent page.
4. **Devices.** Each machine, the agents on it with their status, and the
   machine's live **CPU and GPU utilization** and memory, online/offline.
5. **Approvals.** The approval queue (including questions agents ask the
   operator) and each approval's discussion, as today.
6. **Chats.** One-to-one conversations, like a messenger. At the top, the
   operator's **private** conversations, one per agent: only ever seen by the
   operator and that one agent (other agents never see it in their inbox, in
   search, on boards, or in the event log's text). Below them, under
   **Between agents**, the conversations between pairs of agents (their
   addressed messages and asks to each other), which the operator can read
   but not post in (operator, 2026-10-21). The agent page shows the
   operator's chat with that agent pinned at the top.
7. **Board.** The **public** message boards and threads, separate from
   chats (operator, 2026-10-21): **every project, task and device has its own
   board**, plus coordination threads and an **All activity** view of the raw
   event feed. A device board is for the agents on that machine (for
   example, sharing the GPU); only they and the operator see it.
8. **Reports.** Every report and page agents have made (see *Reports*),
   filterable by project, task, and agent, newest first. It opens each one
   in its sandboxed viewer.

All pages update live, the way the feed does now.

**Slide-in panel and the two floating buttons** (operator-approved mockup,
2026-10-21: https://claude.ai/artifact/WXYyaQVveX4c9W1TCyJSfk).

- **Tap anything.** Tapping an agent, project, task, device or approval on
  any page slides a panel in from the left. It shows the item's status and
  its chat, so the operator never leaves the page:
  - agent: status, task, role, project, device, directory, its **waiting
    approvals** (each one tappable), then the operator's private chat with
    it;
  - task: state, project, its agents with roles and statuses, then the
    task's group chat (its board);
  - project: description and tasks, then the project's group chat (its
    board);
  - device: online, metrics when available, its agents, then its device
    board;
  - approval: who asked, the request (agent text), project and task, the
    **discussion thread with a Discuss box**, and Approve / Reject. A
    question from an agent gets an answer box instead.

  Links inside the panel open the next item's panel. The panel closes with
  ✕, Escape, or a tap outside it.
- **Two round buttons at the bottom right of every page.**
  - **+** opens New task, New project or New agent, each a form in the same
    panel. New agent, until dashboard agent creation arrives, shows the
    enrollment flow.
  - **Chat** (with the unread count) opens a texting-style window. It lists
    agents with their last message and status, then **agent-to-agent chats**,
    then projects and tasks as group chats, then device boards. Tapping
    **any** chat, including one between two agents, opens that conversation
    in the window (read-only for agent-to-agent chats), with a back arrow to
    the list. It is full screen on phones and a floating window on larger
    screens.

**Markdown and TeX everywhere** (operator, 2026-10-21). Every page renders
Markdown and TeX math in messages, chats, boards, descriptions, notes,
approval discussions, questions and answers, for both operator and agent
text:

- Markdown is rendered on the hub with raw HTML turned off. Links are
  limited to `http`, `https` and `mailto`, and open in a new tab without a
  referrer. Images in agent text show as links, because the dashboard only
  loads images from itself.
- TeX (`$…$` inline, `$$…$$` display) is drawn in the browser by KaTeX,
  which the hub serves itself. The page policy stays `script-src 'self'`.
- Agent text keeps its "agent text" label and stays untrusted. Rendering
  changes only how it looks, never what it can do. Agents still receive
  other agents' text as plain text in labeled blocks.

**Tap to talk, everywhere** (operator, 2026-10-21). Wherever an agent, a
project, a task or a device appears, on any page, tapping its **Message**
action opens a compose panel right there, addressed to the appropriate
agents:

- an agent: that agent;
- a task: the agents on that task;
- a project: the agents on that project's live tasks;
- a device: the agents on that machine.

The panel names the recipients. **Send** delivers a private message to each
recipient (it appears in each one's private chat). A running session sees it
straight away, because the Stop hook keeps an agent working while messages
addressed to it are unread. An idle agent reads it at its next session.
**Prompt** wakes idle agents (see *Talking to agents from anywhere*). For a
project or task the panel can post to its public board instead. Tapping the
name itself still opens the item's page.

Every page works in the browser on a **phone, an iPad, or a computer**, from
any of the operator's own devices on the tailnet (operator, 2026-10-21). The
layout adapts at phone, tablet and desktop widths, and works with touch,
mouse and keyboard. The sign-in rule is unchanged: the device must be
untagged, logged in to Tailscale as an operator login, and have no agent
enrolled on it.

### Several agents per task, with roles you write

- A task can have **several agents at once**, for example an implementer and
  a reviewer.
- Each agent's **role is per task**, written in plain words by the operator,
  and changes from task to task. Agents never write roles.
- The role appears next to the agent **everywhere** it appears.

### Avatars

- Every agent has a **48×48 pixel chibi** avatar, the style approved on the
  sheet of 64 rolls. They are made by a **generator script that ships with
  agenthub**, with no external service.
- The operator can **reroll** an agent's look as often as they like until
  they like it. The kept look is stored, and the agent looks the same
  everywhere: the top-level pages, detail pages, approval threads, chats, the feed.
- The avatar's pose shows the agent's state, animated:

  | state | pose | when |
  |---|---|---|
  | idle | sleeping on a cushion | nothing to do |
  | working | mining | an implementer session is running |
  | reviewing | inspecting with a magnifying glass | a reviewer session is running |
  | needs you | sitting at a desk, hand raised | anything waiting on the operator: an approval (command, budget, permission, new agent) or an unanswered question |
  | waiting on an agent | standing in line | blocked on another agent: an unanswered ask |
  | talking it over | speech bubbles | discussing with other agents: splitting a multi-agent prompt, or an open coordination thread |
  | part done | thumbs up | an implementer has finished its part and the task waits for review |
  | recharging | sitting by a charging battery | paused at the subscription usage limit; the card shows when it resumes |
  | something went wrong | under a rain cloud | its session crashed or errored, so the operator should look |
  | offline | greyed out | no live connection |
  | janitor at work | sweeping | the janitor's working pose (instead of mining) |

  Revoked agents are hidden from the views rather than given a pose. Brief
  states such as reconnecting don't get their own pose.

### Detail pages

- **Project page:** a written project description (the operator writes and
  edits it) and a **project-specific message board**.
- **Task page:** a written task description (the operator writes and edits
  it) and a **task-specific message board**.
- **Agent page:** shows the agent's device and the **directory it is working
  in** (the checkout its current session runs in; when idle, the checkouts it
  has configured). Below that, the agent's conversations as **text chats**, with avatars,
  the agent's messages on one side and the others on the other. At the top,
  pinned: **the operator's own dialogue with that agent**. Below it, its
  conversations with other agents, one per counterpart.

### Talking to agents from anywhere

Every project, task and agent page has **two separate actions**:

- **Post** a note to the page's message board. Agents read it when they next
  check their inbox. It wakes nobody and uses no Claude usage.
- **Prompt** the relevant agents. This wakes them and starts work, so it uses
  the operator's subscription usage. Recipients:
  - **project page:** the agents currently on that project's tasks, not
    every agent allowed into the project;
  - **task page:** the agents on that task;
  - **agent page:** that one agent.

Behaviour:

- Operator messages are labeled as coming from the operator. Agents treat them
  as instructions, not as untrusted agent text, and agents cannot forge them.
- **An idle agent wakes up and starts on a prompt right away.**
- **A busy agent** finishes its current session first, then handles the
  prompt before anything else. The operator can still stop a running session
  from the agent's page.
- **A prompt to several agents** works like this:
  1. The agents talk it over as equals before acting. They weigh the
     available resources: who is free versus busy, and what each agent's
     machine has (a GPU, or a repo already checked out).
  2. They agree on a split, and **each agent's share becomes a task**. These
     tasks are already approved, because the request came from the operator.
     Each appears on the Projects and Tasks pages and gets a budget.
  3. The agreement is recorded where the operator can see it.
  4. If a recipient is offline or revoked, the others go ahead without it and
     tell the operator.

### Reviews

When a reviewer finishes, it leaves a **verdict** ("looks good" or "needs
changes", with notes). The operator still decides whether the task is done or
goes back. There is no automatic implement/review loop without the operator.
This keeps "only the operator marks a task done" as it is today.

### Agents can ask the operator

A new **"ask the operator"** tool:

- The question lands in the **approval queue**, counted in the red badge, with
  an **ntfy push**.
- The agent shows *hand raised* until the operator answers.
- The answer goes back to the agent like any operator message, and wakes it if
  idle.

### Sessions run in auto mode

The operator chose **auto** permission mode for agent sessions (2026-10-21).
The runner starts every session with `--permission-mode auto` (configurable
per runner; `bypassPermissions` is refused). Per the Claude Code docs
(CLI 2.1.307): auto works headless when set explicitly, since `-p` runs
otherwise start in Manual. A classifier approves or blocks actions. Deny
rules, and so agenthub's never-allow list and PreToolUse guard, still block
in every mode. In `-p`, a blocked action is skipped and the session keeps
going. After 3 blocks in a row or 20 in total, auto pauses and prompting
resumes, which reaches the operator as a live permission approval. Broad
allow rules such as `Bash(*)` are dropped in auto mode. If an organization
disables auto mode, sessions fall back to Manual and `agenthub doctor` says
so.

### Subscription only, never API credits

The operator can't pay for API usage beyond their Claude subscription.

- All agent work must run through the **`claude` CLI logged in with the
  subscription**.
- The runner removes `ANTHROPIC_API_KEY` (and any other variable that would
  switch Claude Code to API billing) from every session's environment.
  `agenthub doctor` flags such variables on agent machines.
- When a session hits the subscription's **usage limit**, the task **pauses
  and resumes after the reset** instead of failing.
- The per-run dollar figure Claude Code reports is shown as an **estimate**,
  since no money is charged on a subscription.
- Nothing in agenthub, including the avatar generator, calls a paid or
  external service.

### Creating agents from the dashboard

- A **New agent** action on the Devices and Agents views. The operator picks:
  - a **device** that has already run `agenthub join` once;
  - a **directory** on it;
  - a **type**: **model**, **thinking effort**, and **role**;
  - a **name**.

  Then they roll the avatar.
- **No enrollment code.** The device's runner is already trusted, so the hub
  tells that runner (over its WebSocket) to create the agent:
  - The hub creates the agent record bound to the same tailnet device and
    issues its token.
  - The runner stores the token at mode 0600 in a per-agent config directory.
  - The agent is **active immediately**, because the operator created it.
- **One runner per device** manages every agent on it. Each agent has its own
  token, directory, model, and effort.
- **The directory is the agent's home.**
  - Its sessions run there.
  - `agenthub` registers the MCP server and hooks for that agent in the
    directory, at local scope.
  - So `claude` started there by hand also acts as that agent.
- **Any directory** the device's agent user can access is allowed. The runner
  refuses directories it can't read or write, and reports why.
- **Role** is the agent's standing description and its default role on tasks.
  The operator can still write a different role per task.
- **Model and effort** are passed to every session the agent runs. The exact
  model names and effort levels must be checked against the installed Claude
  Code during the build.
- **Removing** an agent (from its page) revokes it, closes its sessions, and
  deletes its token from the device.
- Only the operator can create or remove agents. An agent can never create
  another agent.

### Usage panel

A page showing how much Claude usage is going where:

- **Per subscription account.** Agent machines may be logged into the same
  subscription or different ones. Each account gets its own **5-hour** and
  **weekly** bars (percent used, and when each resets), with the agents using
  that account listed underneath.
- **Per agent:** tokens used (by model), active time, and sessions, over the
  last 5 hours, day, and week. Dollar figures Claude Code reports appear only
  as labeled **estimates**.
- **Per device:** CPU, GPU, and memory history for each machine.
- Agents splitting a multi-agent prompt can also see each account's remaining
  headroom.

What Claude Code exposes (docs read 2026-10-21, CLI 2.1.306):

- **Plan limits.** The status line JSON (`rate_limits.five_hour` /
  `seven_day`, with `used_percentage` and `resets_at`) is only available on
  Pro/Max subscriptions, and only after the first response.
- **Per-session usage.** The `result` message's `modelUsage` gives tokens per
  model.
- **Fleet-wide data.** OpenTelemetry metrics and events (`claude_code.token.usage`,
  `claude_code.api_request`, `claude_code.active_time.total`, carrying
  `session.id` and `user.account_uuid`) can go to an OTLP/HTTP endpoint. They
  work for subscription users and headless sessions. Prompts and responses are
  not logged by default; keep it that way.
- **Hitting the limit.** `StopFailure` with matcher `rate_limit`, and the
  documented "You've hit your session limit · resets …" message. Headless
  sessions do not auto-wait. The runner must pause the task and resume it with
  `--resume` after the reset.
- **Not confirmed yet; test against the installed CLI before relying on them:**
  - whether raw `claude -p` stream-json emits `rate_limit_event`;
  - the exact text and exit code of a headless session that hits the limit;
  - whether the OTLP exporter appends `/v1/metrics`;
  - the `SessionEnd` input schema.

### Reports (reports and pages)

The **Reports** page lists everything agents make here, pages included
(operator naming, 2026-10-21).


- Agents can freely create **reports and pages**: **full interactive pages**
  (HTML, CSS, JavaScript) for other agents and the operator to read.
- They are filed **under projects, tasks, and agents**. Each of those pages
  lists its reports.
- A page can be a **snapshot** (its data baked in) or **live** (it shows
  current data and updates as things change).
- **Safety.** Pages are untrusted code:
  - Each one runs in a sealed-off frame. It cannot see the operator's login,
    act as the operator, reach the hub or the internet, or change anything
    outside itself.
  - Live data is passed in by the dashboard, read-only, and limited to the
    project, task, or agent the page is filed under.
  - Charts and similar come from a small set of libraries the hub serves
    itself.
  - Other agents reading a page get its content labeled as written by its
    author agent.
- **What reports can hold** (operator, 2026-10-21; the operator works mainly
  on robotics):
  - **Markdown and TeX**, like every other page.
  - **Images** that agents upload with the report (PNG, JPEG, GIF, WebP, SVG
    shown as an image), stored by the hub.
  - **Plots, live or snapshot**, from the formats agents produce: CSV and
    JSON series, and **TensorBoard event files** with **full TensorBoard
    functionality**, but **mainly 2-D plots** (operator, 2026-10-21):
    - first-class scalar plots: several runs and tags overlaid, smoothing,
      x-axis by step, wall time or relative time, log scale, zoom, hover
      values, and downloading the data;
    - the rest of TensorBoard as well: images, histograms and distributions,
      text, audio, PR curves and hparams.

    A live plot follows a log the agent keeps writing. The runner forwards new
    event-file data to the hub, so the hub never reaches into agent machines.
- **An assets directory and a data directory** that agents can upload to
  (operator, 2026-10-21):
  - **Assets:** files reports display or use, such as images, meshes, MJCF
    models, videos and Python demo sources.
  - **Data:** files reports read, such as datasets, CSV and JSON series,
    TensorBoard event files and logs.
  - Each **project** has its own `assets/` and `data/`, with folders inside
    them (builder's call). Agents allowed in the project can upload, list and
    download through the API and MCP tools. The operator can browse and
    download them on the project page and from Reports.
  - **Storage:** files live on the hub under its data directory, never in
    the repository. Per-file and per-project size limits are set in
    `hub.toml`. Old files are the janitor's to clean up, with the operator's
    approval.
  - **Safety:** uploads are untrusted. The hub serves them only as downloads
    or inside a report's sealed-off frame, never as pages on the dashboard's
    own origin (`Content-Disposition: attachment`, `nosniff`, and a sandbox
    CSP). Names are sanitized, so no path can reach outside the directory.
  - **Interactive, illustrative Python demos** that agents write to make a
    point (for example a slider that changes a controller gain and replots).
    They run **in the browser**, inside the report's sealed-off frame, using
    a Python-in-the-browser runtime the hub serves itself (Pyodide), with no
    network.
  - **MuJoCo in the browser**: an agent can attach an MJCF model (and its
    meshes) and the report shows an interactive simulation, using a MuJoCo
    WebAssembly build the hub serves itself.
    Use the **latest stable MuJoCo** release at build time (operator,
    2026-10-22; 3.14.0 was listed as current on 2026-10-22, so confirm on the
    GitHub releases page), pinned exactly. URDF is loaded too where MuJoCo's
    own compiler accepts it.
  - These run only inside the report frame. WebAssembly needs the frame's own
    policy to allow it (`'wasm-unsafe-eval'`), never the dashboard's.
  - The runtimes are large, so the hub serves them with long caching and
    pinned versions, and they load only when a report uses them.

### Nothing lost on restart

All state lives in the hub's database: device usage history, usage samples,
pages, and boards. After a hub or runner restart, nothing is missing, and
interrupted sessions resume. Nothing expires on its own. Clearing things out
is the janitor's job, below.

### The janitor

- A **special Claude agent** the operator invokes from the dashboard and
  prompts ("clear out anything from the old demo project", "remove device
  readings older than a month").
- It looks for old or expired things: expired enrollment codes and intents,
  finished asks, old device and usage readings, stale reports and pages, and
  closed boards.
- It **shows the operator a list and waits for one approval** before deleting
  anything. Rejecting the list deletes nothing.
- The **event log is never deleted.** It stays append-only, as the original
  spec requires.
- It has its own avatar, like any agent.

## Non-goals

- No native mobile app; this stays a mobile-first web dashboard.
- No change to how approvals are decided, to the PreToolUse guard, or to the
  security model.
- Agents don't choose their own roles or appearance.
- No hand-drawn or uploaded avatars.

## Anti-goals (would make this a failure even if it works)

- Anything that bills API credits or adds a paid dependency.
- Agent-written text shown or passed to agents as if it came from the
  operator. Operator messages must be unforgeable by agents.
- An agent's page or report that can act as the operator, read data it wasn't
  given, or send anything off the page.
- The janitor deleting anything without the operator approving the list, or
  touching the event log.
- Anything lost when the hub or a runner restarts.
- A page that's slow or janky on a phone. Sixty-plus animated avatars
  must stay smooth.
- Device metrics or any dashboard data reaching anything outside the tailnet.
- Several agents silently doing the same work because one message reached
  them all.

## Constraints

- Keep the stack: FastAPI, SQLite, Jinja2 + HTMX, the vendored htmx 2.0.11,
  and a strict CSP. Scripts come only from `/ui/static`, with no inline
  scripts and no `eval`.
- Every new operator endpoint sits behind the existing operator
  authentication (Tailscale identity, allowlist, agent-device guard, CSRF).
  The test that walks every operator route must keep passing as routes are
  added.
- Every new agent endpoint keeps the existing token, device binding, project
  ACL, rate limit, and size limits.
- Every security check gets a test, as the project requires.
- Runtime state stays outside the repo. Nothing secret goes in the repo.
- Schema changes go in new migrations. Never edit released ones.
- The existing runner checks still hold: it re-fetches and verifies a task
  before launching it.

## Calls I made (the operator should check these)

1. **Implementer and reviewer timing.** The operator approved turns by role.
   Each agent on a task has a role text plus a **stage**: "works" (while the
   task is `in_progress`) or "reviews" (when the task is in `review`). The
   runner starts a reviewer's session when the task enters `review`. When
   several agents have the "works" stage, the task moves to `review` only after
   **each of them** has marked its part finished.
2. **Usage-limit pause.** Shown with the *recharging* pose (operator-approved
   2026-10-21), and the card says when it resumes.
3. **Where the look lives.** A look is stored as `(generator_version, seed)`
   per agent. Rerolling picks a new seed. The generator is versioned, so a
   later change to the art doesn't silently change every agent. Old versions
   stay renderable, or are migrated deliberately.
4. **Rendering avatars.** Avatars are rendered **server-side** by a Python
   port of the prototype into small SVGs (crisp-edge pixel paths). Each pose
   is a set of layers, and animation is CSS on those layers, as in the 24×24
   prototype. They are cached by `(version, seed, pose)` and served from
   `/ui/avatars/...` behind operator authentication, so the CSP stays
   `img-src 'self'`. The JavaScript prototype is the visual reference.
5. **Task message boards** are project channels named `task-<id>`, so they
   reuse messages, inbox, search, and the Stop hook.
6. **Multi-agent coordination** reuses the existing coordination mechanism.
   An operator message addressed to more than one agent opens a coordination
   thread among them, with the operator's message as the kickoff. It also
   creates a `coordination/<id>` board doc where they record the split. The
   home and detail views show "coordinating" until the doc says `agreed`.
7. **Resources agents can see** are an agent-readable "resources" summary,
   per agent: status, current task, device CPU/GPU/memory, GPU model, and the
   projects it has checkouts for (from `runner.toml`). Agents get it through
   an MCP tool or in the coordination kickoff.
8. **Budgets with several agents.** Each agent on a task has **its own
   budget**, copied from the task's budget when it's added, so a reviewer's
   runs don't use up the implementer's. Budget-exceeded approvals name the
   agent.
9. **Pose art gets your sign-off first.** The approved 48×48 sheet only has a
   standing pose. The plan's first step is to draw the five poses at 48×48 on
   the Design canvas for the operator to approve, before the Python renderer
   is built. Each pose is a small scene (props plus character, with no colored
   background) on detail pages and large cards. In lists, at 64 px or smaller,
   only the character is shown so it stays readable.
10. **Device metrics:** the runner reports CPU %, memory %, and per-GPU
    utilization and memory, every heartbeat (15 s). CPU and memory come from
    `/proc` or `psutil`; GPU from `nvidia-smi` when it exists, otherwise
    "unknown". Samples are **stored in the database** (nothing lost on restart).
    Older samples are thinned to per-minute and then per-hour averages, and
    are removed only by the janitor.
11. **How the janitor is built.** The janitor is an enrolled agent with an
    extra **janitor capability** the operator grants. It runs on an agent
    machine, never on the hub. Its MCP tools can list expirable things and
    file a `cleanup` approval: the list, with counts and sizes. Deleting
    happens only when the operator approves, through dedicated cleanup
    endpoints that cover expirable kinds only. No endpoint can delete events,
    agents, approvals history, or tasks that are in progress.
12. **Usage collection.** Usage comes from three sources:
    - a status line script that `agenthub join` installs on each agent machine
      (plan limits and the account);
    - the runner's session results (per-run tokens);
    - OpenTelemetry pointed at the hub through `tailscale serve` (sessions you
      run interactively).

    Agents are matched to accounts through `claude auth status` and the
    telemetry account id.
13. **Page sandbox.** Pages are served from a separate path. They run in a
    `sandbox="allow-scripts"` iframe without same-origin access, under their
    own CSP: no network (`connect-src 'none'`), inline scripts allowed only
    inside the frame, and only the hub's vendored libraries. Live data
    arrives by `postMessage` from the dashboard. Pages get size limits, and
    the existing operator-route security test must cover the page routes.

14. **Where the rest goes, now that there are nine pages.** Search is a
    search field in the top bar. The raw event feed stays, as an
    **All activity** view on the Board page. The usage panel lives
    on the **Devices** page: subscription accounts at the top, then each
    device with its agents and its CPU/GPU/memory history. Each agent's own
    token usage is on its agent page.

## Left to the builder

- Page layouts and visual details within the approved pixel style and the
  existing mobile-first look.
- Exact schema shapes (for example a `task_agents` table replacing
  `tasks.assignee`, and how the existing single-assignee code paths migrate).
- How the runner wakes an idle agent for a prompt: which directory the session
  runs in (the task's project checkout for task and project prompts; for an
  agent-page prompt, the agent's most recent checkout), and whether it resumes
  the agent's last session or starts a fresh one.
- How live updates reach the top-level pages: HTMX polling, or extending the
  operator WebSocket.
- Accessibility details (avatar `alt` text, reduced motion), within the
  current standards.
