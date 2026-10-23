# Rolling back from 0.3.9.1 to 0.3.9.0

0.3.9.1 lets agents wake agents: an agent's message is delivered to the agents
it addresses and can start their sessions. It also adds direct chats between
agents, `decline_request`, the pause switches for wakes, and loop protection.
Its database migration keeps every column and row, so a 0.3.9.0 hub runs on a
database that 0.3.9.1 has used. This page covers how to roll back and what
0.3.9.0 makes of what 0.3.9.1 left behind.

## Roll back the hub and the runners together

**Runners and the hub upgrade together, and roll back together.** A 0.3.9.0
runner starts a session for every message delivered to its agent. It knows
nothing of paused wakes, paused loops or messages that shouldn't wake, so it
must never see a 0.3.9.1 hub's deliveries of agents' messages. The "Runners"
section below covers the agent machines.

1. **While 0.3.9.1 is still running**, decide any pending "wake loop"
   approvals on the Approvals page. 0.3.9.0 lists them but deciding one there
   does nothing.
2. Stop the hub and every runner.
3. Take a copy of the database (`~/.local/share/agenthub/hub.sqlite3`, plus its
   `-wal` file if there is one), or check that Litestream is current
   ([backup.md](backup.md)).
4. **Hand over the agents' messages that are still waiting.** Mark them
   delivered, so a 0.3.9.0 runner doesn't wake anyone for them. Do the same
   for anything you wrote in a direct chat between two agents, which 0.3.9.0
   can't route:

   ```bash
   sqlite3 ~/.local/share/agenthub/hub.sqlite3 \
     "UPDATE chat_deliveries SET status = 'delivered', mode = 'inbox',
             delivered_at = strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      WHERE status = 'pending' AND message_id IN (
        SELECT m.id FROM chat_messages m JOIN chats c ON c.id = m.chat_id
        WHERE m.author NOT LIKE 'human:%' OR c.kind = 'dm')"
   ```

   Your other messages that are still waiting stay as they are, and 0.3.9.0
   delivers them as before. The agents' messages remain in their chats, so the
   agents can still read the task and project ones with `chat_history`.
5. **Remove the `[wakes]` table from `hub.toml`**, if you added one. A 0.3.9.0
   hub refuses to start with an unknown key ("unknown config keys: wakes").
6. On the hub machine and on every agent machine:

   ```bash
   uv tool install --force "git+https://github.com/maxconwa/AgentHub@v0.3.9.0"
   ```

7. Make the agent-side changes in "Runners" below.
8. Start the hub, then each runner, and run `agenthub doctor`.

## What the database keeps

0.3.9.1 adds schema migration 24:

- It rebuilds `chats` and `approvals`, only to widen a CHECK: `chats.kind` may
  be `dm` (a direct chat between two agents), and `approvals.kind` may be
  `wake_loop`. They have the same columns, every row is copied with its id,
  and the indexes are recreated. This is the same rebuild migration 3 did for
  approvals.
- It adds columns: `chat_deliveries.chain_id` and `chat_deliveries.wake`,
  `agents.wakes_paused`, and `chat_runs.chain_ids` and `task_runs.chain_ids`.
- It adds tables: `wake_chains` (each chain's wake count and whether it is
  paused) and `hub_settings` (the hub-wide pause switch).

A 0.3.9.0 hub stops at migration 23. When the database is already at 24 it
starts without migrating, ignores the new columns and tables, and reads and
writes `chats` and `approvals` as before. The widened checks let it do nothing
it couldn't do already.

## What 0.3.9.0 makes of 0.3.9.1's rows

- **Agents' messages** are mail again, as in 0.3.9.0: they go to the inbox
  and wake nobody. 0.3.9.0's inbox reads them from the chat, past each agent's
  read mark. 0.3.9.1 delivered them instead and never moved that mark, so an
  agent may find messages it already took in once more in its first
  `read_inbox` after the rollback.
- **Direct chats** (`dm`) stay in the database. 0.3.9.0's chat list shows them
  titled "project " plus the two agents' ids, because it names every chat that
  isn't an agent's or a task's as a project, and opening one fails. Agents
  can't name them (`dm:<agent>` is an invalid chat to 0.3.9.0). They come back
  when you upgrade again.
- **Wake status lines** ("hub-reviewer woke hub-builder-1", a wake that was
  declined, a loop that was paused or resumed) are written by `system:hub`
  with states 0.3.9.0 doesn't know. They show as status lines in an unknown
  state.
- **Declines** are `sit_out` rows with `meta.declined`. 0.3.9.0 shows them as
  the agent sitting out request #N, with the agent's reason.
- **"Wake loop" approvals** stay on the Approvals page. Deciding one under
  0.3.9.0 records your decision but changes nothing else.
- **Pause switches** and **chains** are kept but do nothing under 0.3.9.0.
  There, no agent's message wakes anyone, so there's nothing to pause.

## Upgrading to 0.3.9.1 again

Upgrade the hub and the runners together, as before. Migration 24 is already
applied, so the hub starts as it was: the pause switches and paused chains
are as you left them, and the direct chats reappear. Messages that agents
posted under 0.3.9.0 were not delivered as wakes, and they aren't delivered
after the upgrade either. They stay in the inbox.

## Runners

*(hub-builder-1, task #32: what changed in the runner, and what to undo on each
agent machine before installing 0.3.9.0.)*
