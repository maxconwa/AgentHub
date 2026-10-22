"""Markdown and TeX for the dashboard: agent- and operator-written text rendered safely.

CommonMark plus tables and strikethrough, with raw HTML off (every ``<`` in the source is shown as text).
Links are kept only for ``http:``, ``https:`` and ``mailto:``; any other link (``javascript:``, ``data:``,
relative) stays plain text, and every kept link opens in a new tab without a referrer, with its real
destination as its title. Images become links,
so agent text never makes the browser fetch anything. ``$…$`` and ``$$…$$`` become ``.math`` elements holding
the TeX as escaped text, which static/app.js typesets with the vendored KaTeX (``trust: false``).

Only :func:`render` turns text into Markup; templates use it through the ``md`` filter.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markupsafe import Markup
from mdit_py_plugins.dollarmath import dollarmath_plugin

# Render at most this much as Markdown; the rest is shown as plain text.
MAX_RENDERED = 20_000

# "https:" must start an absolute URL: "https:x" would be a link relative to the dashboard itself.
SAFE_LINK = re.compile(r"(?:https?://|mailto:)", re.IGNORECASE)
LINK_ATTRS = {"target": "_blank", "rel": "noopener noreferrer nofollow"}


class _SafeMarkdown(MarkdownIt):
    def validateLink(self, url: str) -> bool:  # noqa: N802 (markdown-it's name)
        """Links, autolinks, reference definitions and images: only http(s) and mailto survive."""
        return bool(SAFE_LINK.match(url))


def _link_open(self, tokens, idx, options, env) -> str:
    """The title is the real destination (link text can claim any URL); the author's own title is dropped."""
    token = tokens[idx]
    href = str(token.attrGet("href") or "")
    token.attrs = {"href": href, "title": href, **LINK_ATTRS}
    return self.renderToken(tokens, idx, options, env)


def _image(self, tokens, idx, options, env) -> str:
    """An image is shown as a link to it, named by its alt text."""
    token = tokens[idx]
    src = str(token.attrGet("src") or "")
    text = self.renderInlineAsText(token.children or [], options, env) or src
    attrs = "".join(f' {k}="{v}"' for k, v in LINK_ATTRS.items())
    return f'<a href="{escapeHtml(src)}" title="{escapeHtml(src)}"{attrs}>{escapeHtml(text)}</a>'


def _cell_open(self, tokens, idx, options, env) -> str:
    """Column alignment as a class: the CSP forbids the inline ``style`` markdown-it would write."""
    token = tokens[idx]
    style = str(token.attrs.pop("style", ""))
    if style.startswith("text-align:"):
        token.attrSet("class", "md-align-" + style.removeprefix("text-align:"))
    return self.renderToken(tokens, idx, options, env)


def _math_inline(self, tokens, idx, options, env) -> str:
    return f'<span class="math inline">{escapeHtml(tokens[idx].content)}</span>'


def _math_inline_double(self, tokens, idx, options, env) -> str:
    """``$$…$$`` inside a paragraph: display math, as a span so it stays inside the paragraph."""
    return f'<span class="math display">{escapeHtml(tokens[idx].content)}</span>'


def _math_block(self, tokens, idx, options, env) -> str:
    return f'<div class="math display">{escapeHtml(tokens[idx].content.strip())}</div>\n'


def _build() -> MarkdownIt:
    # breaks: a single newline stays a line break, as these bodies were shown before (chat-style text).
    md = _SafeMarkdown("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])
    # No space inside the dollars and no digit outside them, so "$5 and $10" stays text.
    dollarmath_plugin(md, allow_labels=False, allow_space=False, allow_digits=False, allow_blank_lines=False,
                      double_inline=True)
    for name, rule in (("link_open", _link_open), ("image", _image), ("th_open", _cell_open),
                       ("td_open", _cell_open), ("math_inline", _math_inline), ("math_inline_double", _math_inline_double),
                       ("math_block", _math_block)):
        md.add_render_rule(name, rule)
    return md


_md = _build()


def render(text: str | None) -> Markup:
    """``text`` as safe HTML: Markdown for the first MAX_RENDERED characters, plain text after."""
    if not text:
        return Markup("")
    head, rest = text[:MAX_RENDERED], text[MAX_RENDERED:]
    html = _md.render(head)
    if rest:
        html += f'<div class="md-rest">{escapeHtml(rest)}</div>\n'
    return Markup(html)  # noqa: S704 (markdown-it escapes everything: raw HTML is off)
