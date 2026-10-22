"""The look of "failed" in the stylesheet: a lasting red edge, and a one-shot hit when something turns failed."""

import re


def _rule(css: str, selector: str) -> str:
    m = re.search(r"^" + re.escape(selector) + r" \{([^}]*)\}", css, re.MULTILINE)
    assert m, selector
    return m.group(1)


def _keyframes(css: str, name: str) -> str:
    m = re.search(r"@keyframes " + name + r" \{(.*?\}) \}", css) or re.search(  # one line, or a block
        r"@keyframes " + name + r" \{(.*?)\n\}", css, re.DOTALL)
    assert m, name
    return m.group(1)


def test_failed_keeps_a_red_left_edge(hub):
    css = hub.client.get("/ui/static/app.css").text
    assert _rule(css, ".is-failed").strip() == "border-left: 3px solid var(--bad);"
    assert re.search(r"\.avatar-status\.status-error, \.avatar-status\.status-failed \{ color: var\(--bad\);", css)


def test_failed_hit_shakes_once_and_rings_out(hub):
    css = hub.client.get("/ui/static/app.css").text
    body = _rule(css, ".failed-new")
    assert "failed-hit 1.2s" in body and "infinite" not in body  # one shot
    frames = _keyframes(css, "failed-hit")
    shake = [int(x) for x in re.findall(r"translateX\((-?\d+)(?:px)?\)", frames)]
    assert shake[:6] == [0, -4, 4, -3, 3, 0] and set(shake[6:]) == {0}
    assert "37.5% { transform: translateX(0); }" in frames  # the shake is over by 0.45s of the 1.2s
    assert "box-shadow: 0 0 0 0 var(--bad)" in frames and "box-shadow: 0 0 0 10px transparent" in frames


def test_failed_hit_under_reduced_motion_is_a_still_outline(hub):
    css = hub.client.get("/ui/static/app.css").text
    reduced = re.search(r"@media \(prefers-reduced-motion: reduce\) \{ \.failed-new \{ animation: (\S+) 1\.2s", css)
    assert reduced
    still = _keyframes(css, reduced.group(1))
    assert "outline: 2px solid var(--bad)" in still and "transform" not in still and "box-shadow" not in still
