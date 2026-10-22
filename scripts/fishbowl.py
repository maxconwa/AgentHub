"""Draw the agenthub logo: a little pixel fishbowl in the avatars' style.

Writes ``src/agenthub/hub/static/fishbowl.svg`` (crisp-edge pixel paths) and PNG
copies at 32, 180 and 512 px for browsers and home screens that want a bitmap.
Standard library only. Run from the repo root: ``python3 scripts/fishbowl.py``.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

W = 32
STATIC = Path(__file__).resolve().parent.parent / "src" / "agenthub" / "hub" / "static"

# Same outline and flat-shading approach as hub/avatar_art.py.
PALETTE = {
    "outline": "#2b2230",
    "glass": "#d8f1fb",
    "glassL": "#ffffff",
    "rim": "#9fd9ef",
    "rimD": "#6fb8d6",
    "water": "#5fb3e8",
    "waterD": "#3f8fc9",
    "waterL": "#8fd0f5",
    "fish": "#ff8a3d",
    "fishD": "#e0662a",
    "fishL": "#ffc08a",
    "eye": "#24202c",
    "white": "#ffffff",
    "sand": "#e6c27a",
    "sandD": "#c49a52",
    "pebble": "#9b7bea",
    "pebble2": "#ff6f91",
    "weed": "#3fb36b",
    "weedD": "#2c8a52",
    "bubble": "#ffffff",
}
SLOTS = list(PALETTE)


def canvas() -> list[str | None]:
    return [None] * (W * W)


def put(c, x, y, k):
    if 0 <= x < W and 0 <= y < W:
        c[y * W + x] = k


def get(c, x, y):
    return c[y * W + x] if 0 <= x < W and 0 <= y < W else None


def ell(c, cx, cy, rx, ry, k, test=None):
    for y in range(W):
        for x in range(W):
            dx, dy = (x + 0.5 - cx) / rx, (y + 0.5 - cy) / ry
            if dx * dx + dy * dy <= 1 and (test is None or test(x, y)):
                put(c, x, y, k)


def recolor(c, frm, to, test):
    for y in range(W):
        for x in range(W):
            if c[y * W + x] == frm and test(x, y):
                c[y * W + x] = to


def outline(c):
    add = [i for i in range(W * W) if c[i] is None and any(
        get(c, i % W + dx, i // W + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))]
    for i in add:
        c[i] = "outline"


def draw() -> list[str | None]:
    c = canvas()
    cx, cy, rx, ry = 16, 18.5, 12.5, 11.5          # the bowl's round body
    inside = lambda x, y: ((x + 0.5 - cx) / (rx - 1)) ** 2 + ((y + 0.5 - cy) / (ry - 1)) ** 2 <= 1  # noqa: E731
    ell(c, cx, cy, rx, ry, "glass", lambda x, y: y >= 8)
    for y in (6, 7, 8):                               # the neck
        for x in range(9, 23):
            put(c, x, y, "glass")
    for x in range(7, 25):                            # the lip, a little wider than the neck
        put(c, x, 5, "rim")
    for x in (7, 24):
        put(c, x, 5, "rimD")
    ell(c, cx, cy, rx - 1, ry - 1, "water", lambda x, y: y >= 11)
    # shade the water along the right curve and the bottom
    # a crescent: water outside the same ellipse shifted up and left is in shade
    shade = lambda x, y: ((x + 0.5 - (cx - 3)) / (rx - 1)) ** 2 + ((y + 0.5 - (cy - 2.5)) / (ry - 1)) ** 2 > 1  # noqa: E731
    recolor(c, "water", "waterD", shade)
    for x in range(W):                                # bright surface line
        if get(c, x, 11) in ("water", "waterD"):
            put(c, x, 11, "waterL")
    # sand, kept inside the bowl
    for y in (26, 27, 28):
        for x in range(W):
            if inside(x, y) and get(c, x, y):
                put(c, x, y, "sandD" if y == 28 or x >= 23 else "sand")
    put(c, 12, 27, "pebble")
    put(c, 20, 27, "pebble2")
    # weed growing from the sand on the left
    for (x, y), k in (((9, 25), "weed"), ((8, 24), "weed"), ((8, 23), "weedD"), ((9, 22), "weed"),
                      ((9, 21), "weedD"), ((8, 20), "weed"), ((8, 19), "weed"), ((10, 24), "weedD"),
                      ((10, 23), "weed")):
        put(c, x, y, k)
    # goldfish swimming right
    ell(c, 17.5, 18, 4.5, 2.6, "fish")
    recolor(c, "fish", "fishD", lambda x, y: y >= 19)
    for x in (15, 16, 17, 18):
        put(c, x, 17, "fishL")
    for (x, y) in ((12, 17), (12, 18), (12, 19), (11, 16), (11, 17), (11, 19), (11, 20), (10, 15), (10, 21)):
        put(c, x, y, "fish")                          # a forked tail fan
    put(c, 11, 18, "fishD")
    for (x, y) in ((16, 15), (17, 15), (18, 15)):     # dorsal fin
        put(c, x, y, "fishD")
    put(c, 17, 21, "fishD")                           # belly fin
    put(c, 20, 17, "eye")
    put(c, 21, 17, "white")
    # bubbles rising from the fish
    for x, y in ((24, 15), (25, 13)):
        put(c, x, y, "bubble")
    # glass highlights on the upper left
    for x, y in ((6, 13), (6, 14), (6, 15), (7, 12), (8, 11), (11, 8), (11, 7)):
        put(c, x, y, "glassL")
    outline(c)
    return c


def svg(c) -> str:
    runs: dict[str, str] = {s: "" for s in SLOTS}
    for y in range(W):
        x = 0
        while x < W:
            k = c[y * W + x]
            if k is None:
                x += 1
                continue
            x0 = x
            while x < W and c[y * W + x] == k:
                x += 1
            runs[k] += f"M{x0} {y}h{x - x0}v1h-{x - x0}z"
    paths = "".join(f'<path d="{d}" fill="{PALETTE[k]}"/>' for k, d in runs.items() if d)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {W}" width="{W}" height="{W}" '
            f'shape-rendering="crispEdges">{paths}</svg>\n')


def png(c, scale: int) -> bytes:
    def rgba(k):
        if k is None:
            return b"\0\0\0\0"
        h = PALETTE[k].lstrip("#")
        return bytes(int(h[i:i + 2], 16) for i in (0, 2, 4)) + b"\xff"
    size = W * scale
    rows = []
    for y in range(size):
        row = b"".join(rgba(c[(y // scale) * W + x // scale]) for x in range(size))
        rows.append(b"\0" + row)
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


def main() -> None:
    c = draw()
    (STATIC / "fishbowl.svg").write_text(svg(c))
    for name, scale in (("fishbowl-32.png", 1), ("fishbowl-192.png", 6), ("fishbowl-512.png", 16)):
        (STATIC / name).write_bytes(png(c, scale))


if __name__ == "__main__":
    main()
