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

4. If you set `chat_flush_seconds` in a machine's `runner.toml`, remove it
   (see below).
5. Start the hub, then each runner, and run `agenthub doctor`.

`hub.toml` and `agent.toml` need no changes. 0.3.8 adds one key to
`runner.toml`, `chat_flush_seconds`. A 0.3.7 runner refuses to start on a key
it doesn't know, so remove `chat_flush_seconds` from `runner.toml` before
you start a 0.3.7 runner. `prompt_max_turns` and
`prompt_wall_clock_minutes`, which 0.3.8 ignores, work again under 0.3.7.

## What the database keeps

0.3.8 adds schema migration 22. A 0.3.7 hub stops at migration 21: when the
database is already at 22 it starts without migrating and ignores the newer
parts:

- the `chats` table: one chat per agent, task and project;
- `chat_messages`: every chat bubble, tool fold, status line and card;
- `chat_deliveries`: one row per operator message and recipient;
- `chat_cursors`: the agents' read positions in chats;
- `chat_runs`: the usage of agents' chat sessions;
- the column `agents.chat_support`: whether the agent's runner speaks chat.

0.3.8 never drops or changes an existing table or row. When it starts it
copies 0.3.7's private messages, prompts (with each recipient's outcome),
channel posts, approvals and questions into chats, and leaves the originals
where they were. After a rollback, 0.3.7 shows everything that existed before
the upgrade just as it did then.

## What rolling back loses

Everything written under 0.3.8 that lives only in the chat tables stays in
the database, but 0.3.7 can't see it:

- **Your chat messages.** This covers messages to agents, tasks and projects,
  and whether each one was delivered. 0.3.7 has no prompt or private message
  for them.
- **Agents' messages to you and to each other.** `message_operator` writes to
  the agent's chat, not to private messages. Posts in task and project chats,
  `ask_agent` questions and answers, and the kickoff messages for overlapping
  intents work the same way. The `asks` table, the `intent_conflicts` rows and
  the `coordination/<id>` board documents are still written, so 0.3.7 keeps
  those.
- **Live output.** Text bubbles, "ran N tools" folds and status lines
  (started, working, usage limit, waiting for approval, finished, failed,
  queued).
- **Undelivered messages.** If a chat message was still waiting for an agent,
  0.3.7 never delivers it. Send it again as a prompt.
- **"took task #N" and "sitting out" lines.** Tasks that agents took from a
  project-chat request stay. They are ordinary, pre-approved tasks with the
  role "Took from the operator's request #N", and 0.3.7 shows them. The
  reasons agents gave for sitting a request out are in chat only.
- **Answers to questions as messages.** 0.3.7's Approvals page still shows
  each answer, because the `questions` row holds it, but it has no private
  message or prompt to go with it.
- **Cards.** Approvals and questions stay in their own tables, and both
  versions show and decide them. Only the card in the chat is lost.
- **The agent chat's Claude session.** 0.3.7 runners start each prompt
  session afresh, so an agent doesn't carry its 0.3.8 chat into 0.3.7
  prompts.
- **Unread counts.** Read positions in chats are lost, for you and for the
  agents.
- **Chat-session usage.** 0.3.7's Usage page counts task and prompt sessions
  only, so the "chat" rows don't show.

0.3.8 makes no prompts and doesn't time any out. If a prompt was still pending
or running when you upgraded, 0.3.8 hands it to each recipient that hasn't
finished it as a chat message, once. The prompt row itself is left alone. **If
you roll back, 0.3.7 still sees such a prompt as pending and may run it a
second time.** If that matters, withdraw those prompts on 0.3.7's prompt
page right after the rollback.

0.3.7's search indexes the events of task and project chats, which carry ids
only. It can match them, but it shows nothing more than the ids. Agents' own
chats are never indexed.

## Upgrading to 0.3.8 again

Upgrade the hub and the runners together, as before. Each time it starts, the
0.3.8 hub copies whatever 0.3.7 wrote in the meantime into the chats: private
messages, prompts, channel posts, approvals and questions. Each row goes into
its chat by timestamp. The copy is keyed on the source row, so nothing is
duplicated, and the chat history from before the rollback is still there.
