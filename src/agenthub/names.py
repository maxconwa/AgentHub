"""Project and agent names: the one rule for what a name may be, and the one conversion for places that can't take
capitals.

A name is 1 to 64 ASCII letters, digits, ``-`` and ``_``, starting with a letter or digit (``Golem``,
``Hub-Builder-1``). The hub keeps it exactly as written and shows it that way, but two names that differ only in
case are the same name: the hub won't create the second, and every lookup (API, dashboard URLs, MCP tools, the CLI,
@mentions) ignores case.

Wherever a name leaves the hub for a system that can't take capitals, or that would treat ``Foo`` and ``foo`` as two
things (systemd units, launchd labels, config folders, git branches on a case-insensitive filesystem), it goes
through :func:`slug`, which lowercases it. Since names are unique ignoring case, no two names share a slug, and a
lowercase name is its own slug, so what existed before capitals were allowed keeps its unit, folder and branch.
"""

from __future__ import annotations

import re

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# An @mention: '@' then a name, not preceded by a word character (so an email address isn't one).
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?![\w-])")


def is_name(value: str) -> bool:
    return isinstance(value, str) and NAME_RE.fullmatch(value) is not None


def slug(name: str) -> str:
    """The name for systems that can't take capitals: the name lowercased. ``slug("Hub-Builder-1")`` is
    ``"hub-builder-1"``; a lowercase name is unchanged."""
    return name.lower()


def key(name: str) -> str:
    """What the hub compares names by (``name_key`` in the database): the name, case ignored."""
    return name.lower()


def same(a: str, b: str) -> bool:
    return key(a) == key(b)


def mentions(text: str) -> list[str]:
    """The names @mentioned in ``text``, as written, each once (first spelling wins, case ignored), in order."""
    seen: dict[str, str] = {}
    for m in MENTION_RE.finditer(text):
        seen.setdefault(key(m.group(1)), m.group(1))
    return list(seen.values())
