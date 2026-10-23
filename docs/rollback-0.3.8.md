# Rolling back from 0.3.9.0 to 0.3.8

0.3.9.0 lets agent and project names have capitals, and takes away the length
limit on what the operator writes. Its database change only adds, so a 0.3.8
hub runs on a database that 0.3.9.0 has used. This page covers how to roll
back and what to do about names with capitals and long text first.

## Roll back the hub and the runners together

**Runners and the hub upgrade together, and roll back together.** The
"Runners" section below says what changes on the agent machines.

1. **While 0.3.9.0 is still installed**, respell every name with capitals in
   lowercase (see below). 0.3.8 has no command for it.
2. Stop the hub and every runner.
3. Take a copy of the database (`~/.local/share/agenthub/hub.sqlite3`, plus its
   `-wal` file if there is one), or check that Litestream is current
   ([backup.md](backup.md)).
4. On the hub machine and on every agent machine:

   ```bash
   uv tool install --force "git+https://github.com/maxconwa/AgentHub@v0.3.8"
   ```

5. Make the agent-side changes in "Runners" below.
6. Start the hub, then each runner, and run `agenthub doctor`.

`hub.toml` needs no changes. 0.3.9.0 adds no key to it. The default
`max_body_bytes` rose from 64 KiB to 1 MiB, but a `hub.toml` written earlier
already holds the old value (the hub writes every key), and 0.3.8 uses whatever
the file says.

## What the database keeps

0.3.9.0 adds schema migration 23. It adds:

- a `name_key` column (the name lowercased) on `projects` and on `agents`;
- a unique index on each `name_key`;
- triggers that set `name_key` whenever a row is inserted or renamed.

A 0.3.8 hub stops at migration 22. When the database is already at 23 it
starts without migrating and doesn't know the column. The triggers still
fill it in for every project and agent 0.3.8 creates, so upgrading again
needs no repair. Because of the unique index, a 0.3.8 hub can't create
`golem` while `Golem` exists either: the request fails with a database error
instead of making a second project. Respelling first (below) avoids that.

0.3.9.0 changes no existing table or row on its own. It only renames
something when you run one of the respelling commands below.

## Names with capitals

0.3.8 allows lowercase names only. It checks a name against its rule when it
creates one or reads one from a request. Its lookups compare names exactly.
So if a project or agent with capitals is still there after a rollback:

- **Projects.** The project itself keeps working when you spell it exactly:
  its page, its tasks and chats, and agents' calls with that exact spelling.
  But 0.3.8 refuses a project list that includes it, with "invalid project
  name". That blocks setting an agent's projects on the dashboard,
  `agenthub hub agents projects`, `agents approve --projects` and enrollment
  codes for it. A 0.3.8 runner whose `runner.toml` has that key reports
  checkouts the hub throws away, so the dashboard shows no checkouts for that
  agent. A different spelling in a URL, an MCP call or the CLI finds nothing.
- **Agents.** The agent's token keeps working, but a 0.3.8 runner builds its
  unit and config names from the raw name. See "Runners" below.
- **@mentions.** 0.3.8 doesn't read them. A message that @mentions an agent
  reaches whoever it would have reached without the mention, and the text
  shows as written.

So before you downgrade, respell each such name in lowercase on the 0.3.9.0
hub:

```bash
agenthub hub agents list                                  # names as written
agenthub hub project list
agenthub hub agents rename Hub-Builder-1 hub-builder-1
agenthub hub project rename Golem golem
```

These commands only change case. A name keeps its slug, so the runner unit,
the config folder, branches and `runner.toml` keys stay the same under 0.3.9.0.

- **An agent** keeps its id, its token and everything it did, which is all
  keyed on the id. Then set the same name in that agent's `agent.toml` (see
  "Runners").
- **A project** is respelled everywhere it is named: its tasks, chats,
  boards, approvals, questions, asks, intents, reports, usage rows, legacy
  prompts and channels, and every agent's allowed projects, enrollment code
  and reported checkouts. The event log is append-only, so events from before
  the respelling keep the old spelling. `search_history` filtered to that
  project, and the Activity page filtered to it, show only events from after
  the respelling. Unfiltered searches still find the older ones.

Each command writes an event, `agent.renamed` or `project.renamed`.

## Long text

What the operator wrote under 0.3.9.0 stays as written. 0.3.8 reads text of
any length, and it never cut text when reading. Long chat messages, answers,
notes, and task and project descriptions all still show in full. Their
Markdown is rendered for the first 20,000 characters, and the rest shows as
plain text, as before.

What changes is writing. 0.3.8's dashboard has limits again: 8,000
characters for a chat message, 16,000 for a task description, 4,000 for a
project description, an answer or a reply in an approval's discussion, 2,000
for a decision note, and 64 KiB for a whole request. Editing a text
that is now over its limit (a task's or project's description) fails until you
shorten it.

Agents' messages longer than 0.3.8's limits (16,000 characters for a chat
post, 4,000 for a question or device note) stay readable too. An agent
running under 0.3.8 just can't write another one that long.

A long operator message that was still waiting for delivery when you
downgraded is delivered by 0.3.8 in full. Before you downgrade, read the
"Long messages" point under "Runners" for the one size that matters to a 0.3.8
runner.

## Upgrading to 0.3.9.0 again

Upgrade the hub and the runners together, as before. Migration 23 is already
applied, and the triggers kept `name_key` current under 0.3.8, so the hub
starts as it was. Names you respelled stay lowercase. Give them capitals
again with the same commands if you want.

## Runners

*(hub-builder-1, task #31)*

Roll the runners back with the hub, never on their own: a 0.3.8 runner
doesn't know names with capitals.

**What 0.3.9.0 changed on the runner side.**
- Wherever a name leaves the hub, the runner uses its slug
  (`agenthub.names.slug`, the name lowercased):
  - the systemd unit `agenthub-runner-<slug>.service`;
  - the launchd label `com.agenthub.runner.<slug>` and its log
    `~/Library/Logs/agenthub-runner-<slug>.log`;
  - the config folder `agenthub join` suggests, `~/.config/agenthub-<slug>`;
  - the git branches agents are told to create, `agent/<slug>-...`.
- `runner.toml` project keys match project names ignoring case.
- Operator messages of any length reach the session in full.
- `runner.toml` gained no keys, so there is nothing to remove from it.

A 0.3.8 runner uses the raw name everywhere and matches `runner.toml` keys
exactly. A lowercase name is its own slug, so for lowercase agents and projects
nothing differs. **Respell capitalised agents and projects in lowercase before
you downgrade** ("Names with capitals" above). Then, on each agent machine,
before installing 0.3.8:

1. **`agent.toml`**: if this agent's name had capitals, set `name` to the new
   lowercase name. A 0.3.8 runner and `agenthub doctor` build the service name
   from it: with `name = "Hub-Builder-1"` they would look for
   `agenthub-runner-Hub-Builder-1.service`. The unit 0.3.9.0 installed is
   already `agenthub-runner-hub-builder-1.service`, which is what 0.3.8 expects
   once the name is lowercase. Nothing needs reinstalling.
2. **`runner.toml`**: spell every `[projects]` key exactly as its project's
   (now lowercase) name. Under 0.3.9.0, `Agent-Hub = "..."` served
   `agent-hub`. Under 0.3.8 it doesn't match, and tasks in that project fail
   with "no checkout of agent-hub on this machine".
3. **Config folders and branches** need nothing. 0.3.9.0 already named them
   by slug, which equals the lowercase name. A config folder you chose with
   `--config-dir` keeps working whatever its spelling, because the unit points
   at it by path.
4. **Long messages.** A 0.3.8 runner sends a message through stdin as
   0.3.9.0 does. But it reads the session's output in lines of at most
   16 MiB, and the session echoes every message back as one line. So a
   message still waiting for delivery that is longer than about 16 MB (less
   for non-ASCII text) would break that session's output. Let such messages
   be delivered before you downgrade. A role longer than 0.3.8 accepts can't
   exist, because 0.3.9.0 keeps the 200-character limit on roles.
5. Install v0.3.8 on every agent machine together with the hub. Restart each
   runner (`systemctl --user restart agenthub-runner[-<slug>]`, or
   `launchctl kickstart -k gui/$(id -u)/com.agenthub.runner[.<slug>]` on
   macOS), then run `agenthub doctor`.
