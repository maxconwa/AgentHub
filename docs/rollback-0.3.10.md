# Rolling back from 0.3.11 to 0.3.10

0.3.11 archives tasks: a task is archived when it is marked done, and the
dashboard, the Tasks page, the chat lists, the Chats badge and an agent's
`list_tasks` leave archived tasks out. An agent's page lists them under
"Archived tasks", and a task's page and chat still open by URL.

Its database migration (28) only adds a column, `tasks.archived_at`, and an
index. It sets the column for every task that was already done. Every column
and row is kept, so a 0.3.10 hub runs on a database that 0.3.11 has used.

## Roll back

1. Stop the hub.
2. Take a copy of the database (`~/.local/share/agenthub/hub.sqlite3`, plus its
   `-wal` file if there is one), or check that Litestream is current
   ([backup.md](backup.md)).
3. Install 0.3.10 and start the hub. Nothing needs editing first.

Runners don't need to roll back with the hub for this change. A 0.3.11 runner
never asks for archived tasks, and an older hub ignores the `include_archived`
parameter of the task list, so the runner simply sees done tasks there again.

## What 0.3.10 makes of it

- **Archived tasks show up again.** 0.3.10 doesn't know `archived_at`: done tasks
  are listed wherever 0.3.10 lists them, as before 0.3.11, and done task
  chats count toward the Chats badge again.
- **0.3.10 never archives.** A task the operator marks done while 0.3.10 runs
  gets no `archived_at`. Migration 28 doesn't run again when you roll forward,
  because the database is already at version 28. Instead, a 0.3.11 hub archives
  at every start any done task that isn't yet archived, dated by the task's
  last move, and logs `archived N done tasks`. Nothing needs doing by hand.
- `PRAGMA user_version` stays at 28. 0.3.10 knows 27 migrations, sees a newer
  version and applies none, which is safe here because 28 is only additive.
  `agenthub doctor` on 0.3.10 warns "schema: version 28 (hub will migrate to
  27)". The hub doesn't migrate anything, and you can ignore the warning.

## If you want the column gone

You don't need to remove it. If you restore the copy from step 2 instead,
whatever happened after it is lost, as with any restore.
