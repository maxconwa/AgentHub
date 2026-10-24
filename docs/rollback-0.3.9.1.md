# Rolling back from 0.3.10 to 0.3.9.1

0.3.10 adds device usage stats (each runner sends its machine's CPU %, RAM and
per-GPU VRAM with its heartbeat, and the Devices pages show them) and lets you
write one message to every agent on a machine. Its migrations keep every
column and row, so a 0.3.9.1 hub runs on a database that 0.3.10 has used. This
page covers how to roll back and what 0.3.9.1 makes of what 0.3.10 left behind.

## Roll back the hub and the runners together

**Runners and the hub upgrade together, and roll back together.** A 0.3.9.1
runner doesn't know device messages, and a 0.3.9.1 hub would deliver the
waiting ones as ordinary operator messages that ignore the pause switches.

1. **Let each runner's queue drain**, as described in "Runners" below. Do
   this while 0.3.10 is still running.
2. Stop the hub and every runner.
3. Take a copy of the database (`~/.local/share/agenthub/hub.sqlite3`, plus its
   `-wal` file if there is one), or check that Litestream is current
   ([backup.md](backup.md)).
4. **Hand over the device messages that are still waiting.** Mark them
   delivered, so a 0.3.9.1 hub doesn't wake anyone for them. 0.3.9.1 would wake
   for them whatever the pause switches say, with the wrong wording:

   ```bash
   sqlite3 ~/.local/share/agenthub/hub.sqlite3 \
     "UPDATE chat_deliveries SET status = 'delivered', mode = 'inbox',
             delivered_at = strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      WHERE status = 'pending' AND message_id IN (
        SELECT m.id FROM chat_messages m JOIN chats c ON c.id = m.chat_id
        WHERE c.kind = 'device')"
   ```

   Your other messages that are still waiting stay as they are, and 0.3.9.1
   delivers them as before. If a device message mattered, write it again in
   each agent's own chat after the rollback.
5. On the hub machine and on every agent machine:

   ```bash
   uv tool install --force "git+https://github.com/maxconwa/AgentHub@v0.3.9.1"
   ```

6. If you set `device_stats` in a `runner.toml`, remove it: a 0.3.9.1 runner
   refuses to start with a key it doesn't know.
7. Start the hub, then each runner, and run `agenthub doctor`.

## What the database keeps

0.3.10 adds schema migrations 26 and 27.

- **26** adds one table, `device_stats`: the latest sample per machine. The
  recent history behind the sparklines lives only in the hub's memory.
- **27** rebuilds `chats`, only to widen its CHECK: `chats.kind` may be
  `device` (a machine's chat, which holds your messages to every agent on it).
  It has the same columns, every row is copied with its id, and the
  table has no indexes or triggers to lose. This is the same kind of rebuild
  migration 24 did.

A 0.3.9.1 hub stops at migration 25. When the database is already at 27 it
starts without migrating, ignores `device_stats`, and reads and writes `chats`
as before. The widened check lets it do nothing it couldn't do already.

## What 0.3.9.1 makes of 0.3.10's rows

- **Device chats** stay in the database. 0.3.9.1 doesn't know the `device`
  kind, so where it lists them it titles each one by the machine's Tailscale
  node id instead of its name. Agents can't name them (`chat='device'` is an
  invalid chat to 0.3.9.1). They come back when you upgrade again.
- **Usage stats** are kept but not shown: the Devices pages go back to
  machines and their agents, with no meters.

## Upgrading to 0.3.10 again

Upgrade the hub and the runners together, as before. Migrations 26 and 27 are
already applied, so the hub starts at once and the device chats reappear with
your earlier messages. The meters fill again within a heartbeat or two of the
runners reconnecting.

## Runners

0.3.10's runner adds one optional `runner.toml` key, `device_stats` (default
true), and no systemd changes.

- **Let each runner's queue drain before you stop it.** A message the runner
  has claimed but no session has taken in yet waits in its chat state file's
  `queued` list (`~/.local/share/agenthub/runner/chat-<agent id>.json`). A
  0.3.9.1 runner can't load a queued entry saved with the new `device` field
  and drops it without a word. On the hub it is already claimed, so nothing
  delivers it again. Wait until no agent is mid-session (the dashboard shows
  none awake) and each state file's `queued` list is empty:

  ```bash
  python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("queued", [])))' \
    ~/.local/share/agenthub/runner/chat-<agent id>.json
  ```

  If one won't drain, the message is still in the device's chat: after the
  rollback, write it again in that agent's own chat.
- **Stats**: a 0.3.9.1 runner sends none. A 0.3.10 runner on a 0.3.9.1 hub
  would send them and the hub would ignore them, but don't run that mix.
