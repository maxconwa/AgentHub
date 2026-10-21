# Multi-Agent Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a task have several agents, each with an operator-written role and a stage ("works" or "reviews"). Implementers work first and reviewers review after, each agent has its own budget, and reviewers leave verdicts. Also make the runner safe for subscription-only use: no API billing, and a pause at the usage limit.

**Architecture:**
- A new `task_agents` table replaces the single `tasks.assignee`. Everything that read `assignee` now reads task membership instead: permissions, the runner's launch check, bus routing, budgets, and approvals.
- Agents no longer move a task to `review` themselves. They call `finish`, and the hub moves the task once every active implementer has finished.
- Reviewers call `record_verdict`. The operator still decides `done`.
- The runner gains stage-aware launching, per-agent budgets, an environment scrub, and a usage-limit pause.

**Tech Stack:** Python 3.12, FastAPI, SQLite migrations in `src/agenthub/hub/db.py`, pytest, and the existing fake `claude` in `tests/fake_claude.py`.

**Spec:** `docs/superpowers/specs/2026-10-21-dashboard-redesign-design.md`. This plan covers these spec sections:
- "Several agents per task, with roles you write";
- "Reviews";
- "Subscription only, never API credits";
- calls 1 (turns by role), 2 (usage-limit pause, hub side only), and 8 (per-agent budgets).

The avatars, home views, messaging, usage panel, pages, and janitor have their own plans.

## Global Constraints

- Roles are written by the operator only; agents never set roles (spec: "Agents never write roles").
- Stage values are exactly `works` and `reviews`. Verdict values are exactly `approve` and `changes`.
- Only the operator moves a task to `done` (spec, "Reviews": "This keeps 'only the operator marks a task done' as it is today").
- No automatic implement/review loop: reviewers record verdicts only, and never move task state.
- Each agent on a task has its own budget, copied from the task's budget when the agent is added (spec call 8).
- The runner must remove from every session's environment any variable that would switch Claude Code to API billing. `agenthub doctor` flags them (spec, "Subscription only").
- On hitting the subscription usage limit, the task pauses and resumes after the reset. It must not fail or block (spec).
- Schema changes go in a **new** migration; never edit released ones (spec, Constraints).
- Every new agent endpoint keeps token auth, device binding, project ACL, rate limit and size limits. Every new operator action keeps operator auth and CSRF. Every security check gets a test (spec, Constraints).
- The runner still re-fetches and verifies a task before every launch (spec, Constraints).

## Review Focus

1. **A revoked or removed implementer must not block the move to review.** "All implementers finished" counts only agents that are still on the task and active. Test lives in Task 2.
2. **An agent removed from a task mid-session must not get another run, and must not resume.** The runner's per-loop verification must drop it. Test lives in Task 6.
3. **A usage-limit message whose reset time can't be parsed** must pause the task for the fallback interval, without blocking it or marking it failed. Test lives in Task 7.
4. **A verdict on a task that isn't in `review`, or from an agent whose stage isn't `reviews`,** is refused with 409 or 403, and no verdict is stored. Test lives in Task 2.
5. **Runs stopped by the usage limit must not use up the run budget.** A task paused five times by the limit must not hit `max_subtasks`. Test lives in Task 3.

---

## File Structure

| file | responsibility |
|---|---|
| `src/agenthub/hub/db.py` | migration 5: `task_agents`, backfill from `tasks.assignee`, `task_runs.outcome` index |
| `src/agenthub/hub/task_agents.py` (new) | membership: add/remove/update/list agents on a task, finish, verdict |
| `src/agenthub/hub/tasks.py` | state machine reads membership; per-agent usage and budgets; `list_tasks` membership filter |
| `src/agenthub/hub/approvals.py` | `budget_exceeded` per agent; `task_reassign` mapped to membership |
| `src/agenthub/hub/bus.py` | task events route to every agent on the task |
| `src/agenthub/hub/app.py` | task payloads carry `agents`; new `/finish`, `/verdict`, `/pause` endpoints |
| `src/agenthub/client.py`, `src/agenthub/mcp_server.py` | client methods and MCP tools `finish_task`, `review_task` |
| `src/agenthub/cli.py`, `src/agenthub/hub/dashboard.py`, `templates/task.html` | operator: add/remove an agent with role and stage (interim UI) |
| `src/agenthub/runner/daemon.py` | membership-based verification, stage-aware launch, per-agent budget, finish, review prompt, usage-limit pause |
| `src/agenthub/runner/session.py` | environment scrub; `usage_limit` outcome with parsed reset time |
| `src/agenthub/doctor.py` | flags API-billing variables and `apiKeyHelper` |

`tasks.assignee` stays in the schema (SQLite can't drop it cheaply) but is **no longer read or written** after Task 1. A test enforces this (Task 1).

---

### Task 1: `task_agents` table and membership store

**Files:**
- Modify: `src/agenthub/hub/db.py` (append migration 5)
- Create: `src/agenthub/hub/task_agents.py`
- Modify: `src/agenthub/hub/tasks.py` (`_SELECT`/`_row`, `get_task`, `list_tasks`, `create_task`, `assign`)
- Test: `tests/test_task_agents.py`

**Interfaces:**
- Produces, in `task_agents.py`:
  - `STAGES = ("works", "reviews")`; `VERDICTS = ("approve", "changes")`; `MAX_ROLE_CHARS = 200`
  - `add_agent(conn, task_id: int, agent: str, *, role: str, stage: str, actor: str, budget: dict | None = None) -> dict`. Returns the task. `agent` is an id or name.
  - `remove_agent(conn, task_id: int, agent: str, *, actor: str) -> dict`
  - `update_agent(conn, task_id: int, agent: str, *, actor: str, role: str | None = None, stage: str | None = None) -> dict`
  - `list_agents(conn, task_id: int) -> list[dict]`, ordered by `added_at`. Each row: `{"agent_id","name","status","role","stage","budget": dict,"finished_at","verdict","verdict_notes","paused_until","added_by","added_at"}`
  - `member(task: dict, agent_id: str) -> dict | None` (pure)
- Produces, in `tasks.py`: `get_task()` returns `"agents": list_agents(...)` and **no** `assignee`/`assignee_name` keys. Callers that relied on them will fail loudly; Tasks 2–6 fix every one.
- Produces, in `tasks.py`: `list_tasks(conn, *, projects, state=None, agent: str | None = None, limit=100)`. `agent` filters by membership. This replaces the `assignee` parameter.
- Produces, in `tasks.py`: `create_task(..., assignee: str | None = None, role: str = "", ...)`. When `assignee` is given, it calls `add_agent(stage="works", role=role)`.
- Produces, in `tasks.py`: `assign(conn, task_id, agent, *, actor, role="")` makes `agent` the **only** `works` agent: it removes the other `works` agents, keeps reviewers, and resets state `claimed`/`in_progress`/`blocked` → `open`. It's used by `task_reassign` and the CLI.

- [ ] **Step 1: Write the failing tests** in `tests/test_task_agents.py`:
  - `test_add_two_agents_with_roles_and_stages`: add `a` as (`"Implementer"`, `works`) and `b` as (`"Reviewer"`, `reviews`). Assert `[ (m["name"], m["role"], m["stage"]) for m in get_task()["agents"] ] == [("a","Implementer","works"), ("b","Reviewer","reviews")]`.
  - `test_agent_budget_copied_from_task`: a task with `budget={"max_turns": 7}`; the added agent's `budget["max_turns"] == 7`. Then changing the task's budget doesn't change the existing agent's budget.
  - `test_add_rejects_duplicates_bad_stage_long_role_and_outsiders`: duplicate → `store.Conflict`; stage `"lead"` → `store.Invalid`; a 201-character role → `store.Invalid`; an agent not in the project, or inactive → `store.Invalid`.
  - `test_membership_changes_emit_events`: the event types `task.agent_added`, `task.agent_updated`, `task.agent_removed` appear, with payload keys `task_id`, `agent_id`, `agents` (the list of member ids after the change).
  - `test_migration_backfills_assignee`: build a schema-4 database with a task whose `assignee='ag1'` (see `test_migration_from_schema_2_preserves_data` in `tests/test_permission_hook.py` for the pattern). After `init_db`, `task_agents` has one row `(task, ag1, role "", stage "works")` whose budget equals the task budget.
  - `test_assignee_column_is_dead`: `grep`-style check. Assert that the string `"assignee"` doesn't appear in `src/agenthub/hub/tasks.py`, `task_agents.py`, `bus.py`, `approvals.py`, `app.py`, `runner/daemon.py` outside comments. Read the files and strip `#` comments before asserting.
  - `test_list_tasks_agent_filter`: two tasks, agent `a` on one; `list_tasks(agent=a.id)` returns only that one.

- [ ] **Step 2: Run, expect failures**

Run: `uv run pytest tests/test_task_agents.py -q`
Expected: FAIL (`ModuleNotFoundError: agenthub.hub.task_agents`)

- [ ] **Step 3: Add migration 5** to `MIGRATIONS` in `db.py`:

```sql
CREATE TABLE task_agents (
    task_id       INTEGER NOT NULL REFERENCES tasks(id),
    agent_id      TEXT NOT NULL REFERENCES agents(id),
    role          TEXT NOT NULL DEFAULT '',
    stage         TEXT NOT NULL CHECK (stage IN ('works', 'reviews')),
    budget        TEXT NOT NULL,
    finished_at   TEXT,
    verdict       TEXT CHECK (verdict IN ('approve', 'changes')),
    verdict_notes TEXT NOT NULL DEFAULT '',
    paused_until  TEXT,
    added_by      TEXT NOT NULL,
    added_at      TEXT NOT NULL,
    PRIMARY KEY (task_id, agent_id)
);
CREATE INDEX task_agents_agent ON task_agents(agent_id);
INSERT INTO task_agents (task_id, agent_id, role, stage, budget, added_by, added_at)
    SELECT id, assignee, '', 'works', budget, 'migration', updated_at FROM tasks WHERE assignee IS NOT NULL;
UPDATE tasks SET assignee = NULL;
```

- [ ] **Step 4: Implement `task_agents.py`** with the Interfaces above.
  - Validation reuses `_resolve_assignee`'s rule: the agent must be active and in the task's project.
  - Budgets go through `tasks.normalize_budget(task["budget"])`.
  - Every mutation emits its event with `project=task["project"]`.

  **Then update `tasks.py`:**
  - Drop `assignee` from `_SELECT` and `_row`.
  - Add `agents` to the task.
  - Rewrite `list_tasks` to use `EXISTS (SELECT 1 FROM task_agents ta WHERE ta.task_id = t.id AND ta.agent_id = ?)`.
  - Make `create_task` and `assign` delegate to `task_agents`.
- [ ] **Step 5: Run the new tests**

Run: `uv run pytest tests/test_task_agents.py -q`
Expected: PASS. Other test files will now fail on the removed `assignee` key; Tasks 2–6 fix those.

- [ ] **Step 6: Commit**

```bash
git add src/agenthub/hub/db.py src/agenthub/hub/task_agents.py src/agenthub/hub/tasks.py tests/test_task_agents.py
git commit -m "feat(tasks): task_agents membership with operator roles and stages"
```

---

### Task 2: State machine on membership, finish, and verdicts

**Files:**
- Modify: `src/agenthub/hub/tasks.py` (`AGENT_MOVES`, `check_transition`, `transition`, `add_note`, `_require_assignee` → `_require_member`)
- Modify: `src/agenthub/hub/task_agents.py` (add `finish`, `record_verdict`)
- Modify: `src/agenthub/hub/bus.py` (`recipients` for `task.*`)
- Test: `tests/test_tasks.py` (update), `tests/test_task_agents.py` (extend), `tests/test_websocket.py::test_routing_is_scoped` (update)

**Interfaces:**
- Consumes: `task_agents.member`, `list_agents` (Task 1)
- Produces:
  - `AGENT_MOVES` loses `("in_progress", "review")`. A new role `"system"` may make only `("in_progress","review")`.
  - `transition(..., role="agent")` requires the acting agent to be a member with stage `works`. Exception: claiming a task with **no** members adds the claimant (`works`, role `""`).
  - An agent moving `claimed|in_progress|blocked → open` (release) **removes that agent** from the task. If members remain, the state stays as it was and only the membership changes.
  - When an operator moves `review → in_progress`, `finished_at` is cleared for all `works` members and `verdict`/`verdict_notes` for all `reviews` members.
  - `task_agents.finish(conn, task_id, agent: Agent, *, note="", device=None) -> dict`. It requires the agent to be a `works` member and the task to be `in_progress`. It sets `finished_at` and emits `task.agent_finished`. When every `works` member whose agent status is `active` has `finished_at`, it calls `transition(..., "review", role="system", actor="system:hub", note="all implementers finished")`.
  - `task_agents.record_verdict(conn, task_id, agent: Agent, *, verdict: str, notes: str = "", device=None) -> dict`. It requires a `reviews` member and task state `review`, sets `verdict`/`verdict_notes`, emits `task.verdict` (payload `task_id`, `agent_id`, `verdict`, `notes`), and changes no state.
  - `bus.recipients` for `task.*` events: the member ids listed in `payload["agents"]`. Every task event payload now carries `agents`, the current member ids. Add this in `transition`, `finish`, `record_verdict`, `add_note`, `record_progress`, `start_run`/`end_run`, and the Task 1 events.

- [ ] **Step 1: Write and update the failing tests**
  - `test_claim_unassigned_task_adds_claimant_as_implementer`
  - `test_non_member_cannot_claim_or_move` → 403
  - `test_reviewer_cannot_move_state`: a reviewer member's `update` to `blocked` → 403.
  - `test_agents_cannot_move_to_review_directly`: `update` with `state=review` → 403.
  - `test_task_moves_to_review_when_all_implementers_finish`: two implementers. After the first `finish`, state is still `in_progress`. After the second, it's `review`, and the last `task.transitioned` event has actor `system:hub`.
  - `test_revoked_implementer_does_not_block_review` (Review Focus 1): implementers `a` and `b`. Revoke `b`; `a` finishes → `review`. Also, removing `b` instead of revoking also lets the move happen.
  - `test_verdict_rules` (Review Focus 4):
    - a verdict while `in_progress` → `store.Conflict`;
    - from a `works` member → `store.Forbidden`;
    - from a non-member → `store.Forbidden`;
    - a bad value `"lgtm"` → `store.Invalid`.

    No row's `verdict` changes in any of these cases.
  - `test_verdict_recorded_and_state_unchanged`: the reviewer's `approve` is stored, and the state stays `review`.
  - `test_operator_send_back_resets_finish_and_verdicts`
  - `test_release_removes_member_and_reopens_when_last`
  - `test_routing_is_scoped` (update): a `task.agent_added` event with `agents=[a,b]` routes to `{a,b}`.
  - Update every existing test in `tests/test_tasks.py` that asserted on `assignee`.
- [ ] **Step 2: Run, expect failures**

Run: `uv run pytest tests/test_tasks.py tests/test_task_agents.py tests/test_websocket.py -q`
Expected: FAIL on the new tests

- [ ] **Step 3: Implement** the Interfaces above. `_require_member(task, agent, stage=None)` replaces `_require_assignee` and raises `Forbidden("only agents on this task may do this")`.
- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_tasks.py tests/test_task_agents.py tests/test_websocket.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A src/agenthub/hub tests
git commit -m "feat(tasks): finish/verdict flow; review only when all implementers finish"
```

---

### Task 3: Per-agent runs, usage, budgets, and approvals

**Files:**
- Modify: `src/agenthub/hub/tasks.py` (`usage`, `start_run`, `end_run`, `budget_extension`, `extend_budget`)
- Modify: `src/agenthub/hub/approvals.py` (`request` for `budget_exceeded` and `task_reassign`, `_apply`)
- Test: `tests/test_tasks.py`, `tests/test_approvals.py`

**Interfaces:**
- Consumes: `task_agents.member` (Task 1)
- Produces:
  - `usage(conn, task_id: int, agent_id: str | None = None) -> dict`, same keys as today. With `agent_id`, it counts only that agent's runs.
  - `runs` counts only runs whose `outcome` is not `"usage_limit"`. Turns and minutes still include them.
  - `start_run` requires membership (any stage). For a `works` member the task must be `in_progress`; for a `reviews` member, `review`.
  - `budget_extension(member: dict, limit: str) -> int`, the same doubling with the same caps, applied to the member's budget.
  - `extend_budget(conn, task_id, agent_id: str, limit: str, value: int, *, actor) -> dict` updates `task_agents.budget`. Event `task.budget_extended` payload gains `agent_id`.
  - `budget_exceeded` approval details: `{"task_id","agent_id","limit","current","used","extend_to"}`, computed from the requesting member. `_apply` extends that agent's budget.
  - `task_reassign` approve → `tasks.assign(conn, task_id, details["assignee"], actor=actor, role=<requester's role>)`.

- [ ] **Step 1: Write the failing tests**
  - `test_usage_is_per_agent`: implementer runs 4 turns and the reviewer 2. `usage(..., agent_id=impl)["turns"] == 4` and `usage(..., agent_id=rev)["turns"] == 2`.
  - `test_usage_limit_runs_do_not_count_toward_runs` (Review Focus 5): five runs ended with `outcome="usage_limit"` plus one `success` give `usage()["runs"] == 1`.
  - `test_budget_exceeded_extends_only_that_agent`: the implementer's approval doubles the implementer's `max_turns`, and the reviewer's is unchanged.
  - `test_reviewer_run_requires_review_state`: `start_run` by the reviewer while `in_progress` → 409.
  - Update `test_budget_extension_is_decided_by_hub_not_agent` and `test_only_assignee_can_record_runs_and_progress`, renamed to `..._only_members_...`.
- [ ] **Step 2: Run, expect failures**

Run: `uv run pytest tests/test_tasks.py tests/test_approvals.py -q`
Expected: FAIL on the new tests

- [ ] **Step 3: Implement** the Interfaces above.
- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_tasks.py tests/test_approvals.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A src/agenthub/hub tests
git commit -m "feat(tasks): per-agent budgets and usage; limit pauses don't consume runs"
```

---

### Task 4: Agent API, client, and MCP tools

**Files:**
- Modify: `src/agenthub/hub/app.py` (task endpoints, new routes)
- Modify: `src/agenthub/client.py`
- Modify: `src/agenthub/mcp_server.py` (`_format_task`, tools)
- Test: `tests/test_tasks.py`, `tests/test_mcp.py`

**Interfaces:**
- Consumes: `task_agents.finish`, `record_verdict` (Task 2); `tasks.list_tasks(agent=...)` (Task 1)
- Produces, agent HTTP:
  - `POST /api/v1/tasks/{id}/finish` with body `{"note": str ≤ 4000}` → the task
  - `POST /api/v1/tasks/{id}/verdict` with body `{"verdict": "approve"|"changes", "notes": str ≤ 4000}` → the task
  - `POST /api/v1/tasks/{id}/pause` with body `{"until": RFC 3339 str}`. It sets the caller's `paused_until` and emits `task.agent_paused`. `{"until": null}` clears it.
  - `GET /api/v1/projects/{p}/tasks?mine=true` uses the membership filter.
- Produces, client: `finish_task(task_id, note="")`, `review_task(task_id, verdict, notes="")`, `pause_task(task_id, until: str | None)`.
- Produces, MCP tools:
  - `finish_task(task_id: int, note: str = "")`. Its docstring: "Mark your part of the task finished; the task moves to review when every implementer has finished."
  - `review_task(task_id: int, verdict: str, notes: str = "")`. Its docstring: "As this task's reviewer, record 'approve' or 'changes' with notes. The operator decides what happens next."
  - `update_task`'s docstring now lists `blocked`/`in_progress`/`open` only.
  - `_format_task` lists each member as `name (role, stage)`, with the role wrapped as **operator-written** text (not untrusted).

- [ ] **Step 1: Write the failing tests**
  - `test_finish_and_verdict_endpoints`: covers the flow, plus 403 for a non-member and 404 for a task in another project.
  - `test_pause_endpoint_sets_paused_until`
  - `test_mcp_finish_and_review_tools`: a two-agent flow over `mcp.Client`. `list_tasks` output contains `"(Implementer, works)"`.
  - `test_role_text_is_labeled_operator`: the task text in the MCP output shows the role outside any `<untrusted-content>` block, and the task body inside one.
- [ ] **Step 2: Run, expect failures**

Run: `uv run pytest tests/test_tasks.py tests/test_mcp.py -q`
Expected: FAIL on the new tests

- [ ] **Step 3: Implement** the routes. Each goes through `Caller`/`Conn` like its neighbours, with pydantic request models carrying `max_length`. Then the client methods and the tools.
- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_tasks.py tests/test_mcp.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A src/agenthub tests
git commit -m "feat(api): finish/verdict/pause endpoints and MCP tools"
```

---

### Task 5: Operator controls for members (CLI and interim dashboard)

**Files:**
- Modify: `src/agenthub/cli.py` (`hub tasks` group)
- Modify: `src/agenthub/hub/dashboard.py` (routes), `src/agenthub/hub/templates/task.html`, `tasks.html`, `_macros.html`
- Test: `tests/test_cli.py`, `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `task_agents.add_agent`, `remove_agent`, `update_agent` (Task 1)
- Produces, CLI:
  - `agenthub hub tasks add-agent TASK AGENT --role TEXT --stage works|reviews`
  - `agenthub hub tasks remove-agent TASK AGENT`
  - `agenthub hub tasks create ... --assignee A --role TEXT`, where `--role` is new
  - `tasks list` shows the members as `name(role)`
- Produces, dashboard (operator auth + CSRF, as with their neighbours):
  - `POST /ui/tasks/{task_id}/agents` with form `agent`, `role` (≤200), `stage`
  - `POST /ui/tasks/{task_id}/agents/{agent_id}/remove`
  - `POST /ui/tasks/{task_id}/agents/{agent_id}/update` with form `role`, `stage`

  `task.html` shows members with role, stage, finished, verdict and notes (notes as agent text), and an add form. These pages get redesigned in the home-views plan; this is the minimum to operate the feature.

- [ ] **Step 1: Write the failing tests**
  - `test_cli_add_and_remove_agent`
  - `test_dashboard_member_routes_require_csrf_and_operator`. The existing `test_agent_token_cannot_reach_any_operator_endpoint` must still pass, and must now include the three new routes. Assert `("POST", "/ui/tasks/{task_id}/agents") in ui_routes(app)`.
  - `test_dashboard_adds_reviewer_with_role`: after the post, `get_task()["agents"]` contains the reviewer with `role == "Reviewer: check the rate limiter"`.
  - `test_verdict_notes_are_escaped`: a verdict whose notes contain `<script>` renders as `&lt;script&gt;`.
- [ ] **Step 2: Run, expect failures**

Run: `uv run pytest tests/test_cli.py tests/test_dashboard.py -q`
Expected: FAIL on the new tests

- [ ] **Step 3: Implement** the Interfaces above.
- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli.py tests/test_dashboard.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A src/agenthub tests
git commit -m "feat(operator): add/remove task agents with roles and stages"
```

---

### Task 6: Runner — membership, stages, per-agent budget, review runs

**Files:**
- Modify: `src/agenthub/runner/daemon.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: the client methods `finish_task`, `review_task`, `pause_task` (Task 4); `task["agents"]` (Task 1); per-agent `usage` (Task 3).
- Produces, alongside: the existing `GET /api/v1/tasks/{id}/runs` gains an optional `agent` query parameter; with it, `"usage"` is `usage(conn, task_id, agent_id)`. The client becomes `task_runs(task_id: int, agent: str | None = None) -> dict`.
- Produces:
  - `Runner._verified_task(task_id) -> tuple[dict, dict] | None`, returning `(task, me)`. It returns `None` unless the caller is a member and the approval check passes, and one of these holds:
    - stage `works`, not finished, and state in `open|claimed|in_progress`;
    - stage `reviews`, `verdict is None`, and state `review`.
  - `sync()` also lists `state="review"`.
  - A `works` run on success calls `client.finish_task(id, note=summary)`. It no longer calls `update_task(review)`.
  - A `reviews` run uses `review_prompt(task: dict, me: dict) -> str`. That prompt includes the task description (untrusted-wrapped), the implementers' latest notes, and the instruction "call review_task(task_id=N, verdict='approve' or 'changes', notes=...)". If the session ends without a verdict, the runner posts a progress item `{"kind":"review","text":"reviewer finished without recording a verdict"}` and sets no verdict.
  - `system_prompt(agent_name, task, me)` includes `Your role on this task (written by the operator): <role>` and the stage.
  - Budgets use `me["budget"]` and per-agent usage. `_pause_for_budget` sends `{"task_id","agent_id","limit"}`.

- [ ] **Step 1: Write the failing tests** (live hub plus fake claude, as the existing tests do):
  - `test_implementer_then_reviewer`:
    1. Operator task with the runner's agent as `works`, and a second runner agent `rev` as `reviews`.
    2. The implementer runs and calls finish, and the task reaches `review`.
    3. The reviewer runner launches. Its fake `claude` stdin contains `"Reviewer"` and `review_task`.
    4. Make the fake record a verdict by calling the hub through the client: add a `FAKE_CLAUDE_MODE=review_approve` that issues `POST /tasks/{id}/verdict` with the token from `AGENTHUB_CONFIG_DIR`.
    5. The verdict is `approve`, and the state stays `review`.
  - `test_removed_member_gets_no_more_runs` (Review Focus 2): `max_turns_then_success` mode, then remove the agent while it's paused on its budget approval. Approve the extension: no second fake call is made, and the runner's work entry ends.
  - `test_runner_ignores_task_where_it_is_not_member`, adapted from `test_runner_ignores_tasks_it_should_not_run`.
  - Update `test_assigned_task_runs_headless_and_reports_progress`. The transition list is now `[("open","claimed"),("claimed","in_progress"),("in_progress","review")]`, and the last transition's actor is `system:hub`.
- [ ] **Step 2: Run, expect failures**

Run: `uv run pytest tests/test_runner.py -q`
Expected: FAIL on the new tests

- [ ] **Step 3: Implement** the Interfaces above. Re-verify through `_verified_task` at the top of every loop iteration, as today.
- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_runner.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A src/agenthub tests
git commit -m "feat(runner): stage-aware launches, per-agent budgets, reviewer runs"
```

---

### Task 7: Subscription safeguards — environment scrub, usage-limit pause, doctor

**Files:**
- Modify: `src/agenthub/runner/session.py`
- Modify: `src/agenthub/runner/daemon.py` (`_task_loop` handling of `usage_limit`)
- Modify: `src/agenthub/doctor.py` (`agent_checks`)
- Modify: `tests/fake_claude.py` (new mode)
- Test: `tests/test_runner.py`, `tests/test_doctor.py`

**Interfaces:**
- Produces, in `session.py`:
  - `BILLING_ENV_VARS: tuple[str, ...]`. Start from `("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")`. **First** confirm the list against https://code.claude.com/docs/en/env-vars and note the fetch date in a comment: add any variable the docs say selects API-key or cloud-provider billing, and drop any that doesn't exist.
  - `session_env(extra: dict[str, str]) -> dict[str, str]`: `os.environ` minus `BILLING_ENV_VARS`, plus `extra`. `Session.run` uses it in place of `{**os.environ, **self.env}`.
  - `parse_limit(text: str, now: datetime) -> tuple[bool, datetime | None]`. It recognises the documented messages "You've hit your session limit · resets 3:45pm" and "You've hit your weekly limit · resets Mon 12:00am" (code.claude.com/docs/en/errors, fetched 2026-10-21). It returns `(True, next local occurrence of that time)`, or `(True, None)` when it recognises the message but can't parse the time.
  - `result_from` returns `outcome="usage_limit"` and `resets_at: datetime | None` (a new `SessionResult` field) when `parse_limit` matches the result text or errors.
- Produces, in the runner: on `usage_limit`, it ends the run with outcome `usage_limit` and calls `pause_task(id, until=resets_at + 60s)`. The fallback is `LIMIT_FALLBACK_SECONDS = 1800`. It posts progress `{"kind":"limit","text":"paused for the usage limit until HH:MM"}`, sleeps until then (cancellable), calls `pause_task(id, None)`, and loops (resuming with `--resume`). The task state is not changed.
- Produces, in doctor: check `"API billing"`. It's a FAIL if any `BILLING_ENV_VARS` is set in the environment, or if `apiKeyHelper` appears in any Claude Code settings file that `_settings_files` reads. Detail: `"<name> would make Claude Code bill API credits instead of your subscription"`.

- [ ] **Step 1: Write the failing tests**
  - `test_session_env_strips_billing_vars`: `monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")`. Then the fake `claude` logs env, and assert `"ANTHROPIC_API_KEY" not in logged_env`. Extend `fake_claude.py` to log the full set of env **names** (not values) under the key `"env_names"`.
  - `test_parse_limit`:
    - `("You've hit your session limit · resets 3:45pm", now=14:00 local)` → `(True, today 15:45)`;
    - a time earlier than now rolls to tomorrow;
    - `"…weekly limit · resets Mon 12:00am"` → the next Monday at 00:00;
    - `"You've hit your session limit · resets soon"` → `(True, None)`;
    - unrelated text → `(False, None)`.
  - `test_usage_limit_pauses_and_resumes`. Add a new fake mode `limit_then_success`: the first call emits a result with `is_error: true` and `result: "You've hit your session limit · resets <now+2min as h:mma>"`. With `LIMIT_FALLBACK_SECONDS` and the sleep monkeypatched to near-zero, assert:
    - the task never enters `blocked`;
    - `paused_until` was set, then cleared;
    - the second call has `--resume`;
    - the task reaches `review`.
  - `test_unparseable_limit_uses_fallback` (Review Focus 3): limit text without a time. The pause uses `LIMIT_FALLBACK_SECONDS`, and the task isn't blocked.
  - `test_doctor_flags_api_billing`: with `ANTHROPIC_API_KEY` set → a FAIL row named `"API billing"`; with `apiKeyHelper` in `settings.json` → FAIL; with neither → OK.
- [ ] **Step 2: Run, expect failures**

Run: `uv run pytest tests/test_runner.py tests/test_doctor.py -q`
Expected: FAIL on the new tests

- [ ] **Step 3: Implement** the Interfaces above.
- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q -o addopts=""`
Expected: all pass

- [ ] **Step 5: Live check with real Claude Code** (no API key in the environment): repeat the Phase 3 scratch run with an implementer and a reviewer on one task (Haiku via `extra_args`). Expected:
  - both runs happen;
  - the task ends in `review` with the reviewer's verdict recorded;
  - `grep -c ANTHROPIC_API_KEY` over the fake-free session env dump is 0. Get the dump by temporarily setting `extra_args=["--debug"]`, or log the env names from the runner at debug level.
- [ ] **Step 6: Commit**

```bash
git add -A src/agenthub tests
git commit -m "feat(runner): subscription-only sessions and usage-limit pause/resume"
```
