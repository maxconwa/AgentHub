"""The real whois client: concurrent lookups for one address share a single tailscale process."""

import asyncio
import json
import stat

from agenthub.hub.tailscale import TailscaleWhois


def test_concurrent_lookups_run_one_process(tmp_path):
    calls = tmp_path / "calls"
    fake = tmp_path / "tailscale"
    body = json.dumps({"Node": {"StableID": "nX", "Name": "phone.example.ts.net."},
                       "UserProfile": {"LoginName": "op@example.com"}})
    fake.write_text(f"#!/bin/sh\necho x >> {calls}\nsleep 0.2\necho '{body}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    whois = TailscaleWhois(tailscale_bin=str(fake))

    async def burst():
        return await asyncio.gather(*(whois.lookup("100.64.0.9") for _ in range(20)))

    results = asyncio.run(burst())
    assert {r.login for r in results} == {"op@example.com"}
    assert calls.read_text().count("x") == 1
