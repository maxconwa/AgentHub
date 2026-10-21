"""agenthub command-line interface."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from agenthub import paths

app = typer.Typer(help="Secure coordination hub for Claude Code agents.", no_args_is_help=True)
hub_app = typer.Typer(help="Run and administer the hub (run these on the hub machine).", no_args_is_help=True)
agents_app = typer.Typer(help="Manage enrolled agents.", no_args_is_help=True)
project_app = typer.Typer(help="Manage projects.", no_args_is_help=True)
app.add_typer(hub_app, name="hub")
hub_app.add_typer(agents_app, name="agents")
hub_app.add_typer(project_app, name="project")


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg="red", err=True)
    raise typer.Exit(1)


def _local_operator() -> str:
    # Local CLI on the hub machine: whoever can run it already controls the DB file.
    return f"human:local:{getpass.getuser()}"


def _hub_conn():
    from agenthub.config import HubConfig
    from agenthub.hub import db

    try:
        cfg = HubConfig.load()
    except FileNotFoundError:
        _fail(f"no hub config at {paths.hub_config_path()}; run `agenthub hub init` first")
    return db.init_db(cfg.db_path)


def _csv(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


# --- hub ----------------------------------------------------------------------

SYSTEMD_UNIT = """\
[Unit]
Description=agenthub coordination hub
After=network-online.target tailscaled.service

[Service]
{environment}ExecStart={exe} hub serve
Restart=on-failure
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths={data_dir} {config_dir}

[Install]
WantedBy=default.target
"""


def write_systemd_unit(unit_dir: Path, cfg) -> Path:
    exe = shutil.which("agenthub") or sys.argv[0]
    # Carry directory overrides into the service, or it would read a different config.
    env = "".join(
        f"Environment={var}={os.environ[var]}\n"
        for var in ("AGENTHUB_CONFIG_DIR", "AGENTHUB_DATA_DIR")
        if os.environ.get(var)
    )
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "agenthub-hub.service"
    unit_path.write_text(
        SYSTEMD_UNIT.format(
            environment=env,
            exe=Path(exe).resolve(),
            data_dir=Path(cfg.db_path).parent,
            config_dir=paths.config_dir(),
        )
    )
    return unit_path


@hub_app.command("init")
def hub_init(
    operator: Annotated[list[str], typer.Option(help="Tailscale login allowed to operate the hub (repeatable).")] = [],
    force: Annotated[bool, typer.Option(help="Overwrite an existing hub config.")] = False,
    systemd_dir: Annotated[Path, typer.Option(help="Where to write the systemd user unit.")] = Path(
        "~/.config/systemd/user"
    ),
    no_systemd: Annotated[bool, typer.Option("--no-systemd", help="Do not write a systemd unit.")] = False,
) -> None:
    """Create the hub config, database, and a systemd user unit."""
    from agenthub.config import HubConfig
    from agenthub.hub import db

    cfg_path = paths.hub_config_path()
    if cfg_path.exists() and not force:
        _fail(f"{cfg_path} already exists (use --force to overwrite)")
    cfg = HubConfig(operators=list(operator))
    cfg.save(cfg_path)
    db.init_db(cfg.db_path).close()

    unit_path = None if no_systemd else write_systemd_unit(systemd_dir.expanduser(), cfg)
    typer.echo(f"config:   {cfg_path}")
    typer.echo(f"database: {cfg.db_path}")
    if unit_path:
        typer.echo(f"unit:     {unit_path}")
    typer.echo("\nNext steps:")
    typer.echo("  systemctl --user daemon-reload && systemctl --user enable --now agenthub-hub")
    typer.echo(f"  tailscale serve --bg --https=443 http://127.0.0.1:{cfg.port}")
    if not operator:
        typer.echo("  add operator logins to `operators` in the config (needed for the dashboard)")


def uvicorn_options(host: str, port: int) -> dict[str, Any]:
    # proxy_headers MUST stay off: uvicorn would otherwise replace the TCP peer
    # address with X-Forwarded-For, and auth relies on seeing the real
    # (loopback) peer before it trusts XFF from tailscale serve.
    return {"host": host, "port": port, "proxy_headers": False, "server_header": False, "log_level": "info"}


@hub_app.command("serve")
def hub_serve(
    insecure_dev: Annotated[bool, typer.Option("--insecure-dev", help="Disable Tailscale identity checks. Local development only.")] = False,
    host: Annotated[str | None, typer.Option(help="Override bind host (loopback only unless --insecure-dev).")] = None,
    port: Annotated[int | None, typer.Option(help="Override port.")] = None,
) -> None:
    """Run the hub API server."""
    import uvicorn

    from agenthub.config import HubConfig
    from agenthub.hub.app import INSECURE_DEV_WARNING, BindError, HubState, check_bind, create_app

    try:
        cfg = HubConfig.load()
    except FileNotFoundError:
        _fail(f"no hub config at {paths.hub_config_path()}; run `agenthub hub init` first")
    host = host or cfg.bind_host
    port = port or cfg.port
    try:
        check_bind(host, insecure_dev=insecure_dev)
    except BindError as exc:
        _fail(str(exc))
    if insecure_dev:
        typer.secho(INSECURE_DEV_WARNING, fg="red", err=True, bold=True)
    uvicorn.run(create_app(HubState(cfg, insecure_dev=insecure_dev)), **uvicorn_options(host, port))


@hub_app.command("enroll")
def hub_enroll(
    projects: Annotated[str, typer.Option(help="Comma-separated projects the new agent may access.")] = "",
) -> None:
    """Create a one-time enrollment code (valid 15 minutes)."""
    from agenthub.hub import db, store

    conn = _hub_conn()
    with db.transaction(conn):
        code = store.create_enrollment(conn, created_by=_local_operator(), projects=_csv(projects))
    typer.echo(code)
    typer.echo("Valid for 15 minutes, single use. On the agent machine run:", err=True)
    typer.echo(f"  agenthub join <hub-url> --code {code} --name <agent-name>", err=True)


@agents_app.command("list")
def agents_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    from agenthub.hub import store

    agents = store.list_agents(_hub_conn())
    if as_json:
        typer.echo(json.dumps(agents, indent=2))
        return
    for a in agents:
        typer.echo(
            f"{a['id']}  {a['name']:<20} {a['status']:<8} {a['device']['name']:<30} "
            f"projects={','.join(a['allowed_projects']) or '-'}"
        )


@agents_app.command("approve")
def agents_approve(
    agent: str,
    projects: Annotated[str | None, typer.Option(help="Replace the agent's allowed projects (comma-separated).")] = None,
) -> None:
    """Activate a pending agent."""
    from agenthub.hub import db, store

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            if projects is not None:
                store.set_agent_projects(conn, agent, _csv(projects), actor=_local_operator())
            a = store.set_agent_status(conn, agent, "active", actor=_local_operator())
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"approved {a.name} ({a.id}) for projects: {', '.join(a.allowed_projects) or '(none)'}")


@agents_app.command("revoke")
def agents_revoke(agent: str) -> None:
    """Revoke an agent. Its token stops working on its next request."""
    from agenthub.hub import db, store

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            a = store.set_agent_status(conn, agent, "revoked", actor=_local_operator())
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"revoked {a.name} ({a.id})")


@agents_app.command("projects")
def agents_projects(agent: str, projects: str) -> None:
    """Set an agent's allowed projects (comma-separated)."""
    from agenthub.hub import db, store

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            a = store.set_agent_projects(conn, agent, _csv(projects), actor=_local_operator())
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"{a.name}: {', '.join(a.allowed_projects) or '(none)'}")


@project_app.command("add")
def project_add(name: str, description: str = "") -> None:
    from agenthub.hub import db, store

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            created = store.ensure_project(conn, name, actor=_local_operator(), description=description)
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"{'created' if created else 'exists'}: {name}")


@project_app.command("list")
def project_list() -> None:
    from agenthub.hub import store

    for p in store.list_projects(_hub_conn()):
        typer.echo(f"{p['name']}  {p['description']}")


# --- agent side -----------------------------------------------------------------


@app.command()
def join(
    hub_url: str,
    code: Annotated[str, typer.Option(help="Enrollment code from `agenthub hub enroll`.")],
    name: Annotated[str, typer.Option(help="Agent name (lowercase, digits, - and _).")],
    mcp_scope: Annotated[str, typer.Option(help="Claude Code MCP scope: user, local, or project.")] = "user",
    no_mcp: Annotated[bool, typer.Option("--no-mcp", help="Skip registering the MCP server with Claude Code.")] = False,
    insecure_dev: Annotated[bool, typer.Option("--insecure-dev", help="Allow an http:// hub URL (local dev).")] = False,
    force: Annotated[bool, typer.Option(help="Replace an existing agent config on this machine.")] = False,
) -> None:
    """Exchange an enrollment code for a token and register with Claude Code."""
    from agenthub import join as join_mod
    from agenthub.client import HubError

    if mcp_scope not in ("user", "local", "project"):
        _fail("--mcp-scope must be user, local, or project")
    try:
        cfg, cfg_path, status = asyncio.run(
            join_mod.enroll(hub_url, code, name, insecure_dev=insecure_dev, force=force)
        )
    except join_mod.JoinError as exc:
        _fail(str(exc))
    except HubError as exc:
        _fail(f"enrollment rejected ({exc.status}): {exc.detail}")
    typer.echo(f"enrolled as {cfg.name} ({cfg.agent_id}); config at {cfg_path}")

    if not no_mcp:
        try:
            spec = join_mod.mcp_server_spec(join_mod.agenthub_command(), paths.config_dir())
            join_mod.register_mcp(spec, scope=mcp_scope, cwd=Path.cwd())
        except join_mod.JoinError as exc:
            _fail(f"{exc} (the token is saved; rerun registration with `claude mcp add-json`)")
        typer.echo(f"registered MCP server '{join_mod.MCP_SERVER_NAME}' with Claude Code (scope: {mcp_scope})")

    if status == "pending":
        typer.secho("status: pending — ask the operator to approve this agent.", fg="yellow")


@app.command()
def mcp() -> None:
    """Run the stdio MCP server (launched by Claude Code)."""
    from agenthub.mcp_server import run_stdio

    try:
        run_stdio()
    except FileNotFoundError:
        _fail(f"no agent config at {paths.agent_config_path()}; run `agenthub join` first")
