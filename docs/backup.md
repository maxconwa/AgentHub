# Backing up the hub with Litestream

The hub keeps all state in one SQLite database (default
`~/.local/share/agenthub/hub.sqlite3`, WAL mode). [Litestream](https://litestream.io)
streams it continuously to object storage or another machine. Instructions
follow Litestream **v0.5.x** (checked 2026-10-21). v0.5 changed the config
format and the on-disk replica format, so pin the version you install.

## What is in the backup

The database holds token **hashes** (not tokens), enrollment-code hashes, every
message, board document, task, approval, and the full event log. Treat the
replica as sensitive:

- Use a private bucket readable only by the backup credentials.
- Turn on server-side encryption.
- Never make the bucket public.

## Install

```bash
VER=0.5.17   # pin an exact release
curl -LO "https://github.com/benbjohnson/litestream/releases/download/v${VER}/litestream-${VER}-linux-x86_64.deb"
sudo dpkg -i "litestream-${VER}-linux-x86_64.deb"
```

The package installs a `litestream` systemd unit that reads `/etc/litestream.yml`.

## Configure

1. Copy `examples/litestream.example.yml` to `/etc/litestream.yml`, set `path`
   to your hub database, fill in the bucket, then run
   `sudo chmod 600 /etc/litestream.yml`.
2. Put credentials in the service environment, not in the YAML:

   ```bash
   sudo systemctl edit litestream
   # [Service]
   # EnvironmentFile=/etc/litestream.env
   sudo install -m 600 /dev/null /etc/litestream.env
   # then add AWS_ACCESS_KEY_ID=... and AWS_SECRET_ACCESS_KEY=... to /etc/litestream.env
   ```

3. Start it: `sudo systemctl enable --now litestream`, then check the logs with
   `journalctl -u litestream -f`.

The hub already does what Litestream needs: WAL mode, a busy timeout, and short
transactions. Don't run manual `VACUUM` or `wal_checkpoint(TRUNCATE)` on a live
hub.

## Verify (do this regularly)

```bash
# What would be restored:
litestream restore -config /etc/litestream.yml -dry-run -json ~/.local/share/agenthub/hub.sqlite3
# A real restore to a scratch file, with an integrity check (exits non-zero on failure):
litestream restore -config /etc/litestream.yml -integrity-check full \
  -o /tmp/hub-restore-test.sqlite3 ~/.local/share/agenthub/hub.sqlite3
sqlite3 /tmp/hub-restore-test.sqlite3 'SELECT COUNT(*), MAX(ts) FROM events;'
rm /tmp/hub-restore-test.sqlite3
```

## Restore after losing the hub

```bash
systemctl --user stop agenthub-hub
litestream restore -config /etc/litestream.yml -o ~/.local/share/agenthub/hub.sqlite3.restored \
  ~/.local/share/agenthub/hub.sqlite3
mv ~/.local/share/agenthub/hub.sqlite3.restored ~/.local/share/agenthub/hub.sqlite3
rm -f ~/.local/share/agenthub/hub.sqlite3-wal ~/.local/share/agenthub/hub.sqlite3-shm
chmod 600 ~/.local/share/agenthub/hub.sqlite3
systemctl --user start agenthub-hub && agenthub doctor
```

If you restore onto a **new machine**:

- Also restore `~/.config/agenthub/hub.toml`, tag the machine `tag:agent-hub`,
  and run `tailscale serve` again.
- Agents keep working if the new machine takes the old tailnet hostname. Their
  tokens are bound to the agents' own devices, not to the hub. Otherwise,
  update `hub_url` in each agent's `~/.config/agenthub/agent.toml`.
- If `csrf.key` is lost, the hub generates a new one; open dashboard tabs just
  need a reload.

If a database was recreated rather than restored, run `litestream reset` before
replicating it again.
