# Rolling back from 0.3.8 to 0.3.7

0.3.8 replaced prompts, private messages, channel posts and boards in the UI
with chats. Its database changes only add, so a 0.3.7 hub runs on a database
that 0.3.8 has used. This page covers how to roll back and what you lose.

## Roll back the hub and the runners together

**Runners and the hub must both be upgraded together, and rolled back
together.** A 0.3.8 runner talks to the hub through the chat API, which a
0.3.7 hub doesn't have. A 0.3.7 runner asks for prompts, which a 0.3.8 hub no
longer hands out. A mismatched pair doesn't corrupt anything, but the operator's
messages don't reach the agents.

1. Stop the hub and every runner.
2. Take a copy of the database (`~/.local/share/agenthub/hub.sqlite3`, plus its
   `-wal` file if there is one), or check that Litestream is current
   ([backup.md](backup.md)).
3. On the hub machine and on every agent machine:

   ```bash
   uv tool install --force "git+https://github.com/maxconwa/AgentHub@v0.3.7"
   ```

4. Start the hub, then each runner, and run `agenthub doctor`.

`hub.toml`, `agent.toml` and `runner.toml` need no changes.
<!-- ROLLBACK-CONFIG: confirm after integration that 0.3.8 adds no config keys a 0.3.7 hub would reject. -->

## What the database keeps

0.3.8 adds schema migration 22. A 0.3.7 hub stops at migration 21 and leaves
the newer tables alone:

- `chats`: one per agent, task and project.
- `chat_messages`: every chat bubble, tool fold and status line.
- `chat_deliveries`: one row per operator message and recipient.
- `chat_cursors`: the agents' read positions in chats.
- `agents.chat_support`: whether the agent's runner speaks chat.

The prompts, private messages, channel messages and boards are never changed.
0.3.8 copies them into chats when it starts, and leaves the originals in place.
After a rollback, 0.3.7 shows everything that existed before the upgrade
exactly as it did before.

## What rolling back loses

Everything written only to chats while the hub ran 0.3.8 stays in the
database, but 0.3.7 can't see it:

- **Messages you sent in a chat.** These include agent chats, task chats and
  project chats. 0.3.7 has no prompt or private-message row for them, so they
  don't appear in Prompts, Chats or boards.
- **Agents' replies and live output.** Text bubbles, "ran N tools" folds and
  status lines (started, working, usage limit, waiting for approval,
  finished, failed, queued) exist only in `chat_messages`.
- **Undelivered messages.** A chat message still pending for an agent is
  never delivered by 0.3.7. Send it again as a prompt.
- **"took task" and "sitting out" lines.** The tasks that agents took from a
  project-chat request stay: they are ordinary, pre-approved tasks and 0.3.7
  shows them. The line that links a task to your request, and the reasons
  agents gave for sitting a request out, are visible only in chat.
- **The agent chat's Claude session.** 0.3.7 runners start prompt sessions
  afresh, so an agent doesn't carry its 0.3.8 chat conversation into 0.3.7
  prompts.
- **Read positions.** The chats' unread counts and the agents' chat cursors.

Approvals and questions are unaffected. They live in their own tables, and
both versions show and decide them.

## Upgrading to 0.3.8 again

Upgrade the hub and the runners together, as before. On start the 0.3.8 hub
copies whatever 0.3.7 wrote in the meantime (prompts, private messages,
channel messages) into the chats. The copy is keyed on the source row, so
nothing is duplicated, and the chat history from before the rollback is still
there.
