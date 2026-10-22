"""Markdown and TeX: agent and operator text is rendered by markdown.render (raw HTML off, safe links only)."""

import re
import time

import pytest
from markupsafe import Markup

from agenthub.hub import db, markdown, store, task_agents, tasks
from agenthub.hub.dashboard import SECURITY_HEADERS

from .conftest import OPERATOR

MD = "Use **bold** and $x^2$"
RENDERED_BOLD = "<strong>bold</strong>"
RENDERED_MATH = '<span class="math inline">x^2</span>'


def tags(html):
    return set(re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)", html))


# --- markdown.render ----------------------------------------------------------------

def test_returns_markup_and_handles_empty():
    assert isinstance(markdown.render("hi"), Markup)
    assert markdown.render("") == "" and markdown.render(None) == ""


@pytest.mark.parametrize("text", [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<div>\n<b>raw block</b>\n</div>",
    "text <iframe src=//evil></iframe> inline",
    "<a href=\"javascript:alert(1)\">x</a>",
    "<!-- comment --><svg onload=alert(1)>",
])
def test_raw_html_never_produces_tags(text):
    html = markdown.render(text)
    assert tags(html) <= {"p", "br"}, html
    assert "&lt;" in html


@pytest.mark.parametrize("text", [
    "[x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x](&#106;avascript:alert(1))",
    "[x](java%0ascript:alert(1))",
    "[x](  javascript:alert(1)  )",
    "[x](data:text/html;base64,PHNjcmlwdD4=)",
    "[x](vbscript:msgbox)",
    "[x](file:///etc/passwd)",
    "[x](/ui/approvals/1)",
    "[x](https:javascript:alert(1))",
    "[x](//evil.example/)",
    "<javascript:alert(1)>",
    "<data:text/html,hi>",
    "![x](javascript:alert(1))",
    "![x](data:image/png;base64,AAAA)",
    "[x][r]\n\n[r]: javascript:alert(1)",
])
def test_unsafe_links_are_plain_text(text):
    html = markdown.render(text)
    assert "<a" not in html and "href" not in html and "<img" not in html, html


def test_safe_links_get_rel_and_target():
    for text, href in [("[site](https://example.com/a?b=1&c=2)", "https://example.com/a?b=1&amp;c=2"),
                       ("<http://example.com>", "http://example.com"),
                       ("[me](mailto:op@example.com)", "mailto:op@example.com")]:
        html = markdown.render(text)
        assert f'<a href="{href}" title="{href}" target="_blank" rel="noopener noreferrer nofollow">' in html, html


def test_link_title_shows_the_real_destination():
    """Link text can claim anything; hovering shows where it really goes (the author's own title is dropped)."""
    html = markdown.render('[https://bank.example](https://evil.example/?a="b"&c "a\\" onclick=\\"b")')
    assert ('<a href="https://evil.example/?a=%22b%22&amp;c" title="https://evil.example/?a=%22b%22&amp;c" '
            'target="_blank" rel="noopener noreferrer nofollow">https://bank.example</a>') in html, html
    assert "onclick" not in html


def test_images_render_as_links():
    html = markdown.render('![a <b> chart](https://example.com/c.png "t")')
    assert "<img" not in html
    assert ('<a href="https://example.com/c.png" title="https://example.com/c.png" target="_blank" '
            'rel="noopener noreferrer nofollow">'
            "a &lt;b&gt; chart</a>") in html


def test_basic_markdown_renders():
    html = markdown.render("**bold** _it_ ~~gone~~\n\n- one\n- two\n\n1. first\n\n`code <x>`\n\n"
                           "```py\nprint('<hi>')\n```\n\n> quoted\n\n# Head")
    for piece in ("<strong>bold</strong>", "<em>it</em>", "<s>gone</s>", "<ul>\n<li>one</li>", "<ol>",
                  "<code>code &lt;x&gt;</code>", '<pre><code class="language-py">print(\'&lt;hi&gt;\')',
                  "<blockquote>", "<h1>Head</h1>"):
        assert piece in html, piece


def test_single_newlines_stay_line_breaks():
    assert markdown.render("one\ntwo") == "<p>one<br />\ntwo</p>\n"


def test_tables_render_without_inline_style():
    html = markdown.render("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |")
    assert "<table>" in html and "<th" in html and "<td" in html
    assert "style=" not in html
    assert 'class="md-align-left"' in html and 'class="md-align-center"' in html and 'class="md-align-right"' in html


def test_math_spans_with_escaped_tex():
    assert '<span class="math inline">x^2</span>' in markdown.render("so $x^2$ holds")
    assert '<span class="math inline">a&lt;b&amp;c</span>' in markdown.render("$a<b&c$")
    assert '<div class="math display">\\int_0^1 f(x)\\,dx</div>' in markdown.render("$$\\int_0^1 f(x)\\,dx$$")
    assert 'a <span class="math display">\\sum_i i</span> b' in markdown.render("a $$\\sum_i i$$ b")
    multi = markdown.render("$$\n\\frac{a}{b} <script>\n$$")
    assert '<div class="math display">' in multi and "&lt;script&gt;" in multi and "<script" not in multi


def test_math_not_in_code_and_prices_stay_text():
    html = markdown.render("`$x$` and\n\n```\n$$y$$\n```")
    assert "math" not in html and "$x$" in html and "$$y$$" in html
    assert "math" not in markdown.render("It costs $5 and $10 total.")


def test_math_class_cannot_be_forged():
    html = markdown.render("```math\nx\n```\n\n<span class=\"math inline\">\\href{javascript:1}{x}</span>")
    assert 'class="math' not in html


def test_long_text_is_capped_and_rest_shown_as_plain_text():
    start = time.monotonic()
    body = ("**b** " * 10_000) + "TAIL<script>"  # 60 000+ characters
    html = markdown.render(body)
    assert time.monotonic() - start < 5
    head, _, rest = html.partition('<div class="md-rest">')
    assert rest and head.count("<strong>") == markdown.MAX_RENDERED // len("**b** ")
    assert "<strong>" not in rest and "**b**" in rest and "TAIL&lt;script&gt;" in rest


@pytest.mark.parametrize("body", ["[" * 50_000, "*a" * 25_000, "> " * 25_000, "$" * 50_000, "`" * 50_000,
                                  "- " * 25_000, "|a" * 25_000 + "\n|-|\n"],
                         ids=["brackets", "emphasis", "quotes", "dollars", "backticks", "lists", "table"])
def test_pathological_input_does_not_blow_up(body):
    start = time.monotonic()
    markdown.render(body)
    assert time.monotonic() - start < 5


# --- pages -----------------------------------------------------------------------------

def get(hub, path, **headers):
    return hub.client.get(path, headers=hub.op_headers(**headers))


def figures(html):
    return re.findall(r'<figure class="agent-text">.*?</figure>', html, re.S)


def assert_rendered_in_label(html, author="worker"):
    hits = [f for f in figures(html) if RENDERED_BOLD in f and RENDERED_MATH in f]
    assert hits, "no agent-text figure with rendered Markdown"
    assert all(f"Written by {author}" in f for f in hits)


@pytest.fixture
def rich(hub):
    a, c = hub.enroll("worker"), hub.enroll("helper")
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=a.id, project="proj", title="Plan", body=MD, assignee=None)
        task_agents.add_agent(conn, t["id"], a.id, role="tester", stage="works", actor=OPERATOR)
        conn.execute("UPDATE task_agents SET verdict = 'approve', verdict_notes = ? WHERE task_id = ?", (MD, t["id"]))
        store.post_message(conn, author=a.id, project="proj", channel="general", body=MD)
        store.post_message(conn, author=a.id, project="proj", channel=f"task-{t['id']}", body=MD)
        store.set_project_description(conn, "proj", "Operator **desc** $y$", actor=OPERATOR)
    for body in (MD, "**pair**"):
        hub.client.post("/api/v1/projects/proj/messages", headers=a.headers,
                        json={"channel": "general", "body": body, "to": "helper"})
    ap = hub.client.post("/api/v1/approvals/command", headers=a.headers,
                         json={"command": "ls", "cwd": "/w", "reason": MD}).json()
    hub.client.post(f"/api/v1/approvals/{ap['id']}/discussion", headers=a.headers, json={"body": MD})
    q = hub.client.post("/api/v1/projects/proj/questions", headers=a.headers, json={"question": MD}).json()
    q2 = hub.client.post("/api/v1/projects/proj/questions", headers=a.headers, json={"question": "port?"}).json()
    hub.client.post(f"/ui/questions/{q2['id']}/answer", headers=hub.op_headers(),
                    data={"csrf": hub.csrf(), "answer": "Use **8080**"})
    hub.client.post("/api/v1/private", headers=a.headers, json={"body": MD})
    hub.client.post("/api/v1/device/messages", headers=a.headers, json={"body": MD})
    node = conn.execute("SELECT device_node_id FROM agents WHERE id = ?", (a.id,)).fetchone()[0]
    x, y = sorted([a.id, c.id])
    return hub, a, t, ap, q, q2, node, f"pair-{x}-{y}"


def test_pages_render_agent_markdown_inside_label(rich):
    hub, a, t, ap, q, q2, node, pair = rich
    paths = ["/ui/projects/proj", "/ui/board/ch-proj~general", f"/ui/tasks/{t['id']}", f"/ui/approvals/{ap['id']}",
             f"/ui/approvals/{ap['id']}/discussion", f"/ui/approvals/{ap['id']}/thread", f"/ui/questions/{q['id']}",
             f"/ui/chats/{a.id}", f"/ui/agents/{a.id}", f"/ui/devices/{node}", f"/ui/chats/pair/{pair}",
             f"/ui/panel?item=agent:{a.id}", f"/ui/panel?item=task:{t['id']}", "/ui/panel?item=project:proj",
             f"/ui/panel?item=device:{node}", f"/ui/panel?item=approval:{ap['id']}",
             f"/ui/panel?item=question:{q['id']}", f"/ui/messenger/agent:{a.id}", "/ui/messenger/board:ch-proj~general"]
    for path in paths:
        r = get(hub, path, **{"hx-request": "true"})
        assert r.status_code in (200, 286), path
        assert_rendered_in_label(r.text), path
        assert "style=" not in r.text, path
    # the verdict notes on the task page
    assert sum(RENDERED_MATH in f for f in figures(get(hub, f"/ui/tasks/{t['id']}").text)) >= 2


def test_operator_text_is_rendered_too(rich):
    hub, a, t, ap, q, q2, node, pair = rich
    hub.client.post("/ui/projects/proj/notes", headers=hub.op_headers(), data={"csrf": hub.csrf(), "body": "**note**"})
    page = get(hub, "/ui/projects/proj").text
    assert "<strong>desc</strong>" in page and '<span class="math inline">y</span>' in page
    assert re.search(r'class="msg mine">.*?<strong>note</strong>', page, re.S)
    assert "<strong>desc</strong>" in get(hub, "/ui/projects").text
    assert "<strong>desc</strong>" in get(hub, "/ui/panel?item=project:proj").text
    assert "<strong>8080</strong>" in get(hub, f"/ui/questions/{q2['id']}").text
    hub.client.post(f"/ui/approvals/{ap['id']}/decide", headers=hub.op_headers(),
                    data={"csrf": hub.csrf(), "decision": "reject", "note": "Not **now**"})
    assert "<strong>now</strong>" in get(hub, f"/ui/approvals/{ap['id']}").text
    assert "<strong>now</strong>" in get(hub, f"/ui/panel?item=approval:{ap['id']}").text


def test_previews_stay_plain_text(rich):
    hub = rich[0]
    html = get(hub, "/ui/messenger").text
    assert "**bold**" in html and RENDERED_BOLD not in html


def test_pages_load_katex_and_csp_unchanged(rich):
    hub = rich[0]
    r = get(hub, "/ui/dashboard")
    assert r.headers["content-security-policy"] == SECURITY_HEADERS["content-security-policy"] == (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
    assert '<link rel="stylesheet" href="/ui/static/katex-0.19.0/katex.min.css">' in r.text
    assert '<script src="/ui/static/katex-0.19.0/katex.min.js" defer></script>' in r.text
    assert r.text.index("katex.min.js") < r.text.index("/ui/static/app.js")


def test_katex_files_served_with_content_types(hub):
    for path, ctype in [("katex-0.19.0/katex.min.js", "text/javascript"), ("katex-0.19.0/katex.min.css", "text/css"),
                        ("katex-0.19.0/fonts/KaTeX_Main-Regular.woff2", "font/woff2"),
                        ("katex-0.19.0/fonts/KaTeX_Math-Italic.woff2", "font/woff2")]:
        r = hub.client.get(f"/ui/static/{path}")
        assert r.status_code == 200, path
        assert r.headers["content-type"].split(";")[0] == ctype, path


def test_app_js_renders_math_with_safe_options(hub):
    js = hub.client.get("/ui/static/app.js").text
    for opt in ("throwOnError: true", "trust: false", 'strict: "ignore"', "maxExpand: 1000", "maxSize: 10"):
        assert opt in js
    assert "katex.render(" in js and "renderToString" not in js and "htmx.onLoad" in js
    # Invalid TeX: our own error display (no KaTeX renderError, whose inline style the CSP blocks).
    assert 'classList.add("katex-error")' in js and "el.textContent = tex" in js


def css_rule(css, selector):
    m = re.search(r"^" + re.escape(selector) + r"\s*\{([^}]*)\}", css, re.M)
    return m.group(1) if m else ""


def test_rendered_markdown_is_clipped_to_its_box(hub):
    """KaTeX can position output anywhere (\\kern, \\smash, \\rlap): nothing paints outside the text's box."""
    css = hub.client.get("/ui/static/app.css").text
    rule = css_rule(css, ".md, .agent-text")
    assert "overflow: hidden" in rule and "contain: paint" in rule
    assert "overflow-x: auto" in css_rule(css, ".math.display")  # wide display math scrolls inside its own box
