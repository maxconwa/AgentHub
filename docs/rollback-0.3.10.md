# Rolling back from 0.3.11 to 0.3.10

0.3.11 does three things that touch a rollback: it archives done tasks, it adds
a way to reopen a task in review (a `task_reopen` approval), and it reads a new
`[reviews]` table in hub.toml. Drafts and the new chat order live in the
browser and the templates, and leave nothing behind for 0.3.10.

0.3.11 archives tasks: a task is archived when it is marked done, and the
dashboard, the Tasks page, the chat lists, the Chats badge and an agent's
`list_tasks` leave archived tasks out. An agent's page lists them under
"Archived tasks", and a task's page and chat still open by URL.

It has two database migrations, and both keep every column and row, so a 0.3.10
hub runs on a database that 0.3.11 has used:

- **28** adds a column, `tasks.archived_at`, and an index. It sets the column
  for every task that was already done.
- **29** rebuilds the `approvals` table only to widen its `kind` CHECK with
  `task_reopen` (same columns, every row copied, as 24 and 27 did).

## Roll back

1. Stop the hub.
2. Take a copy of the database (`~/.local/share/agenthub/hub.sqlite3`, plus its
   `-wal` file if there is one), or check that Litestream is current
   ([backup.md](backup.md)).
3. **Remove the `[reviews]` table from hub.toml** (`~/.config/agenthub/hub.toml`),
   if you added one. 0.3.10 doesn't know it and refuses to start with
   "unknown config keys: reviews". Without the table, a 0.3.11 hub uses the
   default (`stall_minutes = 30`), so you can put it back after rolling forward.
4. Decide any pending **Reopen task** (`task_reopen`) approval first, or note
   the task and move it yourself afterwards (see below).
5. Install 0.3.10 and start the hub.

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
- **0.3.10 ignores `task_reopen` approvals.** It never files one, and it has no
  action for the kind: approving a pending one on 0.3.10 marks it approved but
  leaves the task in review, and rolling forward doesn't replay it. Move such a
  task back to in_progress on its page. On 0.3.10 an agent can't reopen a task
  in review with `update_task` either: only a reviewer's `changes` verdict or
  the operator moves it back.
- **No stalled-review flag.** 0.3.10 has no `stalled_review`: the amber
  "stalled" chips, the note on the task page and `agenthub doctor`'s "stalled
  reviews" check are gone, and a hand-off posted in the chat of a task in review
  wakes nobody unless it's addressed (`to` or an @mention), as before 0.3.11.
- `PRAGMA user_version` stays at 29. 0.3.10 knows 27 migrations, sees a newer
  version and applies none, which is safe here because 28 only adds a column
  and 29 only widens a CHECK that 0.3.10's own writes always pass.
  `agenthub doctor` on 0.3.10 warns "schema: version 29 (hub will migrate to
  27)". The hub doesn't migrate anything, and you can ignore the warning.

## If you want the column gone

You don't need to remove `archived_at`, or narrow the `approvals` CHECK again. If you restore the copy from step 2 instead,
whatever happened after it is lost, as with any restore.
