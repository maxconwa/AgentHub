"""0.3.11 drafts: a half-written message in a chat's box (agent, task, project, direct chat) or the device page's
broadcast box survives clicking away and back. The cause was that the text lived only in the DOM: closing the panel
empties it, opening another chat replaces it, a panel form (a wake switch) re-renders it through #panel, and leaving
the page drops it. Now each box has a stable id with hx-preserve (a #panel swap that re-renders the same chat keeps
the element) and a data-draft-key under which app.js keeps the text in sessionStorage until the hub has taken it."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agenthub.hub import chats, dashboard, db, task_agents, tasks

from .conftest import OPERATOR
from .test_pages import place

APP_JS = (Path(dashboard.__file__).parent / "static" / "app.js").read_text()


def get(hub, path, **headers):
    return hub.client.get(path, headers=hub.op_headers(**headers), follow_redirects=False)


def boxes(html):
    """Every textarea with a draft key: (id, draft key, has hx-preserve)."""
    out = []
    for tag in re.findall(r"<textarea\b[^>]*>", html):
        key = re.search(r'data-draft-key="([^"]*)"', tag)
        if key:
            ident = re.search(r'\sid="([^"]*)"', tag)
            out.append((ident and ident.group(1), key.group(1), bool(re.search(r"\shx-preserve[\s>=]", tag))))
    return out


@pytest.fixture
def team(hub):
    a = hub.enroll("worker")
    b = hub.enroll("helper")
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Fix login", assignee=None)
        task_agents.add_agent(conn, t["id"], a.id, role="tester", stage="works", actor=OPERATOR)
        task_agents.add_agent(conn, t["id"], b.id, role="", stage="reviews", actor=OPERATOR)
    r = hub.client.post(f"/api/v1/chats/dm/{b.name}/messages", headers=a.headers, json={"body": "psst"})
    assert r.status_code == 201, r.text
    return hub, a, b, t, chats.pair_key(a.id, b.id)


# --- the markup -----------------------------------------------------------------------------------------------------

def test_every_chat_box_has_a_draft_key_a_stable_id_and_hx_preserve(team):
    hub, a, _, t, dm = team
    cases = {"agent": a.id, "task": str(t["id"]), "project": "proj", "dm": dm}
    pages = {"agent": f"/ui/agents/{a.id}", "task": f"/ui/tasks/{t['id']}", "project": "/ui/projects/proj",
             "dm": f"/ui/dm/{dm}"}
    for kind, ref in cases.items():
        key = f"{kind}:{ref}"
        for where, html in (("panel", get(hub, f"/ui/panel?item={key}").text), ("page", get(hub, pages[kind]).text)):
            found = boxes(html)
            assert len(found) == 1, (kind, where, found)
            ident, draft, preserved = found[0]
            assert draft == key and preserved, (kind, where)
            assert ident == f"chatbox-{where}-{dashboard.dom_key(key)}", (kind, where)
            # The box is the chat's one message box, inside its data-chat-send form.
            form = html.split("data-chat-send")[1].split("</form>")[0]
            assert f'data-draft-key="{key}"' in form


def test_ids_are_selector_safe_distinct_per_chat_and_per_place(team):
    """htmx finds a preserved element again with a bare '#' + id selector, so the id may hold only letters, digits,
    '-' and '_'; and a page's box and the same chat's box in the panel must not share one."""
    hub, a, _, t, dm = team
    seen = set()
    for key in (f"agent:{a.id}", f"task:{t['id']}", "project:proj", f"dm:{dm}"):
        ident = boxes(get(hub, f"/ui/panel?item={key}").text)[0][0]
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", ident), ident
        seen.add(ident)
    assert len(seen) == 4
    page_id = boxes(get(hub, f"/ui/agents/{a.id}").text)[0][0]
    assert page_id not in seen and page_id.startswith("chatbox-page-")


def test_dom_key_is_one_to_one_and_safe():
    samples = ["task:1", "task_1", "task-1", "dm:a:b", "dm:a_3a_b", "project:my proj", "project:é", "", "_"]
    out = [dashboard.dom_key(s) for s in samples]
    assert len(set(out)) == len(samples)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]*", o) for o in out)
    assert dashboard.dom_key("task:12") == "task_3a_12"
    assert dashboard.dom_key("Ab-9") == "Ab-9"


def test_a_panel_swap_that_re_renders_the_same_chat_keeps_the_box(team):
    """The wake switch in an agent's panel posts with hx-target="#panel": the panel comes back re-rendered. Its box
    has the same id and hx-preserve, so htmx keeps the very element the operator was typing in."""
    hub, a, _, _, _ = team
    before = boxes(get(hub, f"/ui/panel?item=agent:{a.id}").text)
    r = hub.client.post(f"/ui/agents/{a.id}/wakes", headers=hub.op_headers(**{"HX-Request": "true"}),
                        data={"csrf": hub.csrf(), "paused": "1", "panel": f"agent:{a.id}", "next": "/ui/agents"})
    assert r.status_code == 200 and "data-panel-close" in r.text
    assert boxes(r.text) == before


def test_broadcast_box_has_a_per_device_draft_key_outside_the_live_region(hub):
    a, b = hub.enroll("alpha"), hub.enroll("beta")
    place(hub, a, "n1", "box", True, "2026-10-23T16:30:00.000Z")
    place(hub, b, "n2", "other", True, "2026-10-23T16:30:00.000Z")
    for node in ("n1", "n2"):
        html = get(hub, f"/ui/devices/{node}").text
        assert boxes(html) == [("broadcast-body", f"device:{node}", False)]
        live = html.split('<section id="live"')[1].split("</section>")[0]
        assert "<textarea" not in live and "<form" not in live  # a poll never touches the box
        box = html.split('<section class="panel broadcast"')[1].split("</section>")[0]
        assert f'action="/ui/devices/{node}/broadcast"' in box  # still a plain form without JavaScript


def test_a_project_name_with_an_underscore_gets_its_own_id(hub):
    """my_proj and my-proj are two projects; '_' is encoded, so their boxes' ids differ too."""
    hub.enroll("worker", projects=("my_proj", "my-proj"))
    one = boxes(get(hub, "/ui/panel?item=project:my_proj").text)
    two = boxes(get(hub, "/ui/panel?item=project:my-proj").text)
    assert one == [("chatbox-panel-project_3a_my_5f_proj", "project:my_proj", True)]
    assert two == [("chatbox-panel-project_3a_my-proj", "project:my-proj", True)]


def test_card_replies_and_other_forms_keep_no_draft(team):
    """Only the message boxes are drafts; the cards' reply boxes already keep their text across a row refresh."""
    hub, a, _, t, _ = team
    html = get(hub, f"/ui/tasks/{t['id']}").text
    assert html.count("data-draft-key=") == 1


# --- app.js ---------------------------------------------------------------------------------------------------------

def js_between(start, end):
    i = APP_JS.index(start)
    return APP_JS[i:APP_JS.index(end, i)]


def test_app_js_saves_drafts_on_input_in_session_storage_per_key():
    assert 'var DRAFT = "agenthub.draft.";' in APP_JS
    assert 'return DRAFT + ta.getAttribute("data-draft-key");' in APP_JS
    assert re.search(r'addEventListener\("input", function \(evt\) \{\s*if \(evt\.target\.matches && '
                     r'evt\.target\.matches\("textarea\[data-draft-key\]"\)\) saveDraft\(evt\.target\);', APP_JS)
    assert "localStorage" not in APP_JS  # per tab, not kept on disk for every later visitor of the browser
    save = js_between("function saveDraft(ta)", "function forgetDrafts")
    assert "sessionStorage.setItem(draftKey(ta), ta.value)" in save and "sessionStorage.removeItem" in save
    assert "try {" in save and "catch (e)" in save  # storage off or full: no drafts, no broken page


def test_app_js_restores_into_empty_boxes_on_load_and_after_every_swap():
    restore = js_between("function restoreDrafts(root)", "document.addEventListener")
    assert "if (ta.value) return;" in restore  # a preserved element keeps its own text
    assert "sessionStorage.getItem(draftKey(ta))" in restore and "sizeBox(ta)" in restore
    assert "if (window.htmx) htmx.onLoad(restoreDrafts); else restoreDrafts(document.body);" in APP_JS


def test_app_js_forgets_a_draft_only_after_the_hub_took_the_message():
    assert APP_JS.count("forgetDrafts(") == 4  # its definition, two 2xx paths, and the plain-form "Sent" page
    send = js_between('fetch(form.getAttribute("action"), {', "// The message box grows")
    ok, failed = send.split(".catch(function (e) {")
    assert "if (!r.ok) throw" in ok and ok.index("if (!r.ok) throw") < ok.index("forgetDrafts(form)")
    assert "forgetDrafts" not in failed and "form.reset()" not in failed  # a failed send keeps box and draft
    broadcast = js_between("// The device page's broadcast box empties", "// Failed:")
    assert "!evt.detail.successful) return;" in broadcast  # only a 2xx...
    assert re.search(r'querySelector\("#broadcast-result \.ok-line"\)\) \{ form\.reset\(\); forgetDrafts\(form\); \}',
                     broadcast)  # ...that says "Sent" (a refusal comes back 200 with "Not sent")
    plain = js_between("Without HTMX the broadcast box posts as a plain form", "htmx.onLoad(restoreDrafts)")
    assert "if (!window.htmx)" in plain and ".ok-line" in plain


def test_app_js_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    subprocess.run([node, "--check", str(Path(dashboard.__file__).parent / "static" / "app.js")], check=True)
