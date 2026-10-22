"""agenthub command-line interface."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from agenthub import paths

app = typer.Typer(help="Secure coordination hub for Claude Code agents.", no_args_is_help=True)
hub_app = typer.Typer(help="Run and administer the hub (run these on the hub machine).", no_args_is_help=True)
agents_app = typer.Typer(help="Manage enrolled agents.", no_args_is_help=True)
project_app = typer.Typer(help="Manage projects.", no_args_is_help=True)
approvals_app = typer.Typer(help="Review and decide approvals.", no_args_is_help=True)
tasks_app = typer.Typer(help="Create, assign, and move tasks as the operator.", no_args_is_help=True)
operator_devices_app = typer.Typer(
    help="Agent machines that count as the operator (opt-in; weakens separation of powers).", no_args_is_help=True
)
hook_app = typer.Typer(help="Claude Code hooks (invoked by Claude Code) and their installation.", no_args_is_help=True)
app.add_typer(hub_app, name="hub")
hub_app.add_typer(agents_app, name="agents")
hub_app.add_typer(project_app, name="project")
hub_app.add_typer(approvals_app, name="approvals")
hub_app.add_typer(tasks_app, name="tasks")
hub_app.add_typer(operator_devices_app, name="operator-devices")
app.add_typer(hook_app, name="hook")


@app.callback()
def _main(
    config_dir: Annotated[Path | None, typer.Option(
        "--config-dir", envvar="AGENTHUB_CONFIG_DIR", show_envvar=True,
        help="Config dir of the agent (or hub) to act on; each agent on an account has its own.")] = None,
) -> None:
    if config_dir is not None:
        # Exported, so the hooks, MCP server and runner sessions this command starts see it too.
        os.environ["AGENTHUB_CONFIG_DIR"] = str(config_dir.expanduser().resolve())


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
    return {
        "host": host, "port": port, "proxy_headers": False, "server_header": False, "log_level": "info",
        "ws_max_size": 64 * 1024,  # agents only send small heartbeat frames
    }


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
    """Activate a pending agent (decides its enrollment approval)."""
    from agenthub.hub import approvals, db, store

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            if projects is not None:
                store.set_agent_projects(conn, agent, _csv(projects), actor=_local_operator())
            a = approvals.set_agent_status(conn, agent, "active", actor=_local_operator())
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"approved {a.name} ({a.id}) for projects: {', '.join(a.allowed_projects) or '(none)'}")


@agents_app.command("revoke")
def agents_revoke(agent: str) -> None:
    """Revoke an agent. Its token stops working on its next request."""
    from agenthub.hub import approvals, db, store

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            a = approvals.set_agent_status(conn, agent, "revoked", actor=_local_operator(), note="revoked")
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


def _hub_config():
    from agenthub.config import HubConfig

    try:
        return HubConfig.load()
    except FileNotFoundError:
        _fail(f"no hub config at {paths.hub_config_path()}; run `agenthub hub init` first")
    except ValueError as exc:
        _fail(str(exc))


def _tailnet_nodes(tailscale: str) -> dict[str, str]:
    """{StableID: short device name} for this node and the peers it can see."""
    proc = subprocess.run([tailscale, "status", "--json"], capture_output=True, text=True)
    if proc.returncode != 0:
        _fail(f"`tailscale status` failed: {proc.stderr.strip() or proc.stdout.strip()}")
    data = json.loads(proc.stdout)
    nodes = [data.get("Self") or {}, *(data.get("Peer") or {}).values()]
    return {n["ID"]: (n.get("DNSName") or n.get("HostName") or "").split(".")[0] for n in nodes if n.get("ID")}


_RESTART = "restart the hub to apply: systemctl --user restart agenthub-hub"


@operator_devices_app.command("list")
def operator_devices_list() -> None:
    cfg = _hub_config()
    names = _tailnet_nodes(cfg.tailscale_bin) if cfg.operator_devices else {}
    for node, login in cfg.operator_devices.items():
        typer.echo(f"{names.get(node, '?'):<24} {node:<20} {login}")


@operator_devices_app.command("add")
def operator_devices_add(
    device: Annotated[str, typer.Argument(help="Tailscale device name (e.g. littleboy) or node StableID.")],
    login: Annotated[str | None, typer.Option(help="Operator login it acts as (default: the only operator).")] = None,
) -> None:
    """Let requests from DEVICE act as the operator, even though it is tagged or runs an agent.

    Any process on that machine that sends no agent token, the agents included, can then approve,
    answer, and prompt. Agents are told the dashboard is for humans, and the guard asks you before
    one of them fetches it, but neither is a hard boundary.
    """
    cfg = _hub_config()
    if login is None:
        if len(cfg.operators) != 1:
            _fail(f"pass --login, one of: {', '.join(cfg.operators) or '(no operators configured)'}")
        login = cfg.operators[0]
    if login not in cfg.operators:
        _fail(f"{login} is not in operators")
    nodes = _tailnet_nodes(cfg.tailscale_bin)
    matches = [nid for nid, name in nodes.items() if device in (nid, name)]
    if len(matches) != 1:
        _fail(f"no single tailnet device named {device!r}" if not matches
              else f"{device!r} matches several devices; pass its StableID: {', '.join(matches)}")
    cfg.operator_devices[matches[0]] = login
    path = cfg.save()
    typer.secho(f"{nodes[matches[0]]} ({matches[0]}) now acts as {login}, agents on it included; "
                f"saved to {path}", fg="yellow")
    typer.echo(_RESTART)


@operator_devices_app.command("remove")
def operator_devices_remove(device: str) -> None:
    cfg = _hub_config()
    names = _tailnet_nodes(cfg.tailscale_bin)
    matches = [nid for nid in cfg.operator_devices if device in (nid, names.get(nid))]
    if not matches:
        _fail(f"{device!r} is not an operator device")
    for nid in matches:
        del cfg.operator_devices[nid]
    cfg.save()
    typer.echo(f"removed {device}; {_RESTART}")


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


@approvals_app.command("list")
def approvals_list(
    status: Annotated[str, typer.Option(help="pending, approved, rejected, or all")] = "pending",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from agenthub.hub import approvals

    rows = approvals.list_approvals(_hub_conn(), status=None if status == "all" else status)
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        who = r["requester_name"] or r["requester"]
        flag = " [reply waiting]" if r["discussion"] and not r["awaiting_reply"] and r["status"] == "pending" else ""
        typer.echo(f"#{r['id']:<5} {r['status']:<9} {r['kind']:<17} {who:<16} {_approval_summary(r)}{flag}")


def _approval_summary(r: dict) -> str:
    d = r["details"]
    if r["kind"] == "command":
        return f"`{d['command']}` in {d['cwd']}"
    if r["kind"] == "task_create":
        return f"task #{d['task_id']}: {d['title']}"
    if r["kind"] == "agent_enrollment":
        return f"{d['name']} on {d['device']['name']}"
    if r["kind"] == "tool_permission":
        return f"{d['tool_name']} {json.dumps(d.get('input'))[:100]}" + (f" (task #{d['task_id']})" if d.get("task_id") else "")
    return json.dumps(d)[:120]


@approvals_app.command("show")
def approvals_show(approval_id: int, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show an approval and its discussion thread."""
    from agenthub import untrusted
    from agenthub.hub import approvals, store

    try:
        r = approvals.get(_hub_conn(), approval_id)
    except store.StoreError as exc:
        _fail(str(exc))
    if as_json:
        typer.echo(json.dumps(r, indent=2))
        return
    who = r["requester_name"] or r["requester"]
    typer.echo(f"#{r['id']} {r['kind']} [{r['status']}] requested by {who} at {r['created_at']}")
    typer.echo(f"  {_approval_summary(r)}")
    if r["reason"]:
        typer.echo(f"  reason (from agent): {r['reason']}")
    for m in r["discussion"]:
        author = m["author"] if m["author"].startswith("human:") else untrusted.author_label(m["author"], m["author_name"])
        typer.echo(f"  [{m['ts']}] {author}: {m['body']}")
    if r["decided_by"]:
        typer.echo(f"  decided by {r['decided_by']} at {r['decided_at']}: {r['decision_note'] or ''}")


@approvals_app.command("discuss")
def approvals_discuss(approval_id: int, message: str) -> None:
    """Ask or tell the requesting agent something before you decide."""
    from agenthub.hub import approvals, db, store

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            approvals.post_discussion(conn, approval_id, author=_local_operator(), body=message)
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"sent to #{approval_id}; the agent sees it in wait_for_approval or on its next stop")


def _decide(approval_id: int, approve: bool, note: str) -> None:
    from agenthub.hub import approvals, db, store

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            r = approvals.decide(conn, approval_id, approve=approve, actor=_local_operator(), note=note)
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"#{r['id']} {r['status']}: {_approval_summary(r)}")


@approvals_app.command("approve")
def approvals_approve(approval_id: int, note: str = "") -> None:
    _decide(approval_id, True, note)


@approvals_app.command("reject")
def approvals_reject(approval_id: int, note: str = "") -> None:
    _decide(approval_id, False, note)


@tasks_app.command("list")
def tasks_list(
    project: Annotated[str | None, typer.Option()] = None,
    state: Annotated[str | None, typer.Option()] = None,
) -> None:
    from agenthub.hub import store, tasks

    try:
        rows = tasks.list_tasks(_hub_conn(), projects=[project] if project else None, state=state)
    except store.StoreError as exc:
        _fail(str(exc))
    for t in rows:
        members = ", ".join(f"{m['name']}({m['role']})" if m["role"] else m["name"] for m in t["agents"]) or "-"
        typer.echo(f"#{t['id']:<5} {t['project']:<16} {t['state']:<18} {members:<24} {t['title']}")


@tasks_app.command("create")
def tasks_create(
    project: str,
    title: str,
    body: Annotated[str, typer.Option()] = "",
    assignee: Annotated[str | None, typer.Option()] = None,
    role: Annotated[str, typer.Option(help="Role text for the assignee (operator-written).")] = "",
) -> None:
    """Operator-created tasks skip approval and start open. Agents work on them until done (no budget)."""
    from agenthub.hub import db, store, tasks

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            t = tasks.create_task(conn, actor=_local_operator(), project=project, title=title, body=body,
                                  assignee=assignee, role=role, state="open")
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"#{t['id']} open: {t['title']} (budget {t['budget']})")


@tasks_app.command("assign")
def tasks_assign(
    task_id: int,
    assignee: str,
    role: Annotated[str | None, typer.Option(help="Role text; default keeps the existing role.")] = None,
) -> None:
    """Make AGENT the only implementer (reviewers stay)."""
    from agenthub.hub import db, store, tasks

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            t = tasks.assign(conn, task_id, assignee, actor=_local_operator(), role=role)
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"#{t['id']} {t['state']} → {', '.join(m['name'] for m in t['agents']) or '-'}")


@tasks_app.command("add-agent")
def tasks_add_agent(
    task_id: int,
    agent: str,
    role: Annotated[str, typer.Option(help="What this agent is for (operator-written, max 200 chars).")] = "",
    stage: Annotated[str, typer.Option(help="works or reviews.")] = "works",
) -> None:
    """Add AGENT to a task as an implementer (works) or reviewer (reviews)."""
    from agenthub.hub import db, store, task_agents

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            t = task_agents.add_agent(conn, task_id, agent, role=role, stage=stage, actor=_local_operator())
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"#{t['id']} {t['state']}: {', '.join(f'{m['name']}({m['stage']})' for m in t['agents'])}")


@tasks_app.command("remove-agent")
def tasks_remove_agent(task_id: int, agent: str) -> None:
    """Remove AGENT from a task."""
    from agenthub.hub import db, store, task_agents

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            t = task_agents.remove_agent(conn, task_id, agent, actor=_local_operator())
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"#{t['id']} {t['state']}: {', '.join(m['name'] for m in t['agents']) or '-'}")


@tasks_app.command("move")
def tasks_move(task_id: int, state: str, note: str = "") -> None:
    """Move a task to a new state as the operator (e.g. review → done)."""
    from agenthub.hub import db, store, tasks

    conn = _hub_conn()
    try:
        with db.transaction(conn):
            t = tasks.transition(conn, task_id, state, actor=_local_operator(), role="operator", note=note)
    except store.StoreError as exc:
        _fail(str(exc))
    typer.echo(f"#{t['id']} {t['state']}")


# --- agent side -----------------------------------------------------------------


@app.command()
def join(
    hub_url: str,
    code: Annotated[str, typer.Option(help="Enrollment code from `agenthub hub enroll`.")],
    name: Annotated[str, typer.Option(help="Agent name (lowercase, digits, - and _).")],
    mcp_scope: Annotated[str, typer.Option(help="Claude Code MCP scope: user, local, or project.")] = "user",
    no_mcp: Annotated[bool, typer.Option("--no-mcp", help="Skip registering the MCP server with Claude Code.")] = False,
    no_hooks: Annotated[bool, typer.Option("--no-hooks", help="Skip installing hooks and deny rules.")] = False,
    no_runner: Annotated[bool, typer.Option("--no-runner", help="Skip installing the runner service (systemd or launchd).")] = False,
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
            spec = join_mod.mcp_server_spec(join_mod.agenthub_command(), join_mod.fallback_config_dir())
            join_mod.register_mcp(spec, scope=mcp_scope, cwd=Path.cwd())
        except join_mod.JoinError as exc:
            _fail(f"{exc} (the token is saved; rerun registration with `claude mcp add-json`)")
        typer.echo(f"registered MCP server '{join_mod.MCP_SERVER_NAME}' with Claude Code (scope: {mcp_scope})")

    if not no_hooks:
        from agenthub.hooks import install as hook_install

        try:
            path = hook_install.install(mcp_scope, exe=join_mod.agenthub_command(),
                                        fallback_dir=join_mod.fallback_config_dir())
        except (join_mod.JoinError, ValueError, OSError) as exc:
            _fail(f"could not install hooks: {exc} (rerun `agenthub hook install`)")
        typer.echo(f"installed PreToolUse/Stop/PermissionRequest hooks and deny rules in {path}")

    if not no_runner:
        from agenthub.runner import service

        unit, problem = service.install(join_mod.agenthub_command(), paths.config_dir(), cfg.name)
        if problem:
            typer.secho(f"runner service written to {unit} but not started: {problem}", fg="yellow")
        else:
            typer.echo(f"runner service installed and started ({unit})")
        typer.echo("  map projects to local directories in "
                   f"{paths.config_dir() / 'runner.toml'} (see examples/runner.example.toml)")
        if service.use_launchd():
            typer.echo("  the runner runs while this user is logged in; use fast user switching to keep it running")
        else:
            typer.echo("  to keep it running while logged out: sudo loginctl enable-linger $USER")

    if status == "pending":
        typer.secho("status: pending — ask the operator to approve this agent.", fg="yellow")


@app.command()
def runner(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    """Run the runner daemon: execute tasks assigned to this agent in headless Claude sessions."""
    import logging

    from agenthub.config import AgentConfig
    from agenthub.runner import daemon
    from agenthub.runner.config import RunnerConfigError, load_runner_config

    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        agent = AgentConfig.load()
        cfg = load_runner_config()
    except FileNotFoundError:
        _fail(f"no agent config at {paths.agent_config_path()}; run `agenthub join` first")
    except RunnerConfigError as exc:
        _fail(str(exc))
    if not cfg.projects:
        typer.secho("warning: no [projects] in runner.toml; tasks cannot be run until you map project "
                    "names to local directories", fg="yellow", err=True)
    try:
        asyncio.run(daemon.run(agent, cfg))
    except daemon.Revoked as exc:
        from agenthub.runner.service import revoked_exit_code

        typer.secho(f"stopped: {exc}", fg="red", err=True)
        raise typer.Exit(revoked_exit_code())
    except KeyboardInterrupt:
        pass


@app.command()
def doctor(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check Tailscale, hub reachability, token, hooks, runner, and file permissions."""
    from dataclasses import asdict

    from agenthub.doctor import FAIL, OK, run_doctor

    sections, ok = run_doctor()
    if not sections:
        _fail(f"nothing to check: no agent.toml or hub.toml in {paths.config_dir()}")
    if as_json:
        typer.echo(json.dumps({name: [asdict(c) for c in checks] for name, checks in sections}, indent=2))
    else:
        colors = {OK: "green", FAIL: "red"}
        for name, checks in sections:
            typer.secho(f"{name}:", bold=True)
            for c in checks:
                typer.echo("  " + typer.style(f"{c.status:<4}", fg=colors.get(c.status, "yellow")) + f" {c.name}: {c.detail}")
    if not ok:
        raise typer.Exit(1)


@app.command()
def mcp() -> None:
    """Run the stdio MCP server (launched by Claude Code)."""
    from agenthub.mcp_server import run_stdio

    try:
        run_stdio()
    except FileNotFoundError:
        _fail(f"no agent config at {paths.agent_config_path()}; run `agenthub join` first")


# --- hooks ----------------------------------------------------------------------


def _read_hook_input() -> dict[str, Any]:
    data = json.loads(sys.stdin.read() or "{}")
    if not isinstance(data, dict):
        raise ValueError("hook input is not a JSON object")
    return data


@hook_app.command("pretooluse")
def hook_pretooluse() -> None:
    """PreToolUse hook: blocks risky Bash commands until approved. Fails closed."""
    from agenthub.hooks import pretooluse

    try:
        result = pretooluse.handle(_read_hook_input())
    except Exception as exc:  # noqa: BLE001 - any failure denies
        result = pretooluse.deny(f"hook error ({exc.__class__.__name__}); command blocked")
    if result is not None:
        try:
            sys.stdout.write(json.dumps(result))
        except Exception:  # noqa: BLE001 - last resort: exit 2 blocks the call
            sys.stderr.write("[agenthub] hook error; command blocked")
            raise typer.Exit(2)


@hook_app.command("stop")
def hook_stop() -> None:
    """Stop hook: continue while messages addressed to this agent are unread. Fails open."""
    from agenthub.hooks import stop

    try:
        result = stop.handle(_read_hook_input())
    except Exception:  # noqa: BLE001 - never trap the agent
        result = None
    if result is not None:
        sys.stdout.write(json.dumps(result))


@hook_app.command("permission")
def hook_permission() -> None:
    """PermissionRequest hook: in runner sessions, ask the operator live. Denies on any error."""
    from agenthub.hooks import permission

    try:
        result = permission.handle(_read_hook_input())
    except Exception as exc:  # noqa: BLE001
        result = permission._decision("deny", f"hook error ({exc.__class__.__name__})")
    if result is not None:
        sys.stdout.write(json.dumps(result))


@hook_app.command("install")
def hook_install_cmd(
    scope: Annotated[str, typer.Option(help="user, project, or local")] = "user",
    no_mcp: Annotated[bool, typer.Option("--no-mcp", help="Leave the MCP server registration alone.")] = False,
) -> None:
    """Install the hooks, never-list deny rules and MCP server registration into Claude Code."""
    from agenthub import join as join_mod
    from agenthub.hooks import install as hook_install

    exe, fallback = join_mod.agenthub_command(), join_mod.fallback_config_dir()
    try:
        path = hook_install.install(scope, exe=exe, fallback_dir=fallback)
        if not no_mcp:
            join_mod.register_mcp(join_mod.mcp_server_spec(exe, fallback), scope=scope, cwd=Path.cwd())
    except (join_mod.JoinError, ValueError, OSError) as exc:
        _fail(str(exc))
    typer.echo(f"installed hooks and deny rules in {path}"
               + ("" if no_mcp else f"; registered MCP server '{join_mod.MCP_SERVER_NAME}' (scope: {scope})"))


@hook_app.command("uninstall")
def hook_uninstall_cmd(
    scope: Annotated[str, typer.Option(help="user, project, or local")] = "user",
) -> None:
    """Remove agenthub's hooks and deny rules from Claude Code settings."""
    from agenthub.hooks import install as hook_install

    try:
        path = hook_install.uninstall(scope)
    except (ValueError, OSError) as exc:
        _fail(str(exc))
    typer.echo(f"removed agenthub hooks and deny rules from {path}")
