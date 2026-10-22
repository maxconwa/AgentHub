"""Pixel-chibi avatar art: ``(version, seed, pose)`` -> a standalone animated 48×48 SVG.

A line-for-line port of the operator-approved JavaScript generator (the ``Poses`` board on the
avatar Design canvas), so a seed gives the same character here as there. Pure and standard-library
only: nothing here does I/O or calls a service.

Each pose is a stack of layers, drawn on 48×48 grids of palette slot names, outlined, then encoded
as one row-run ``<path>`` per slot. Animation is CSS on the layer ``<g>`` elements, ``steps()``
only, and off under ``prefers-reduced-motion``. The output holds only palette colors and fixed
class names, never caller text.

``GENERATOR_VERSION`` is part of every stored look. Changing the art means adding a new version
and keeping the old one renderable, so existing agents don't silently change.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass

GENERATOR_VERSION = 1
VERSIONS: frozenset[int] = frozenset({1})

POSES: tuple[str, ...] = (
    "idle", "working", "reviewing", "needs_you", "waiting", "talking",
    "part_done", "recharging", "error", "offline", "janitor",
)
POSE_LABELS: dict[str, str] = {
    "idle": "idle",
    "working": "working",
    "reviewing": "reviewing",
    "needs_you": "needs you",
    "waiting": "waiting on an agent",
    "talking": "talking it over",
    "part_done": "part done",
    "recharging": "recharging",
    "error": "something went wrong",
    "offline": "offline",
    "janitor": "janitor at work",
}

W = 48
M32 = 0xFFFFFFFF

SLOTS = (
    "outline", "skin", "skinS", "hair", "hairS", "hairL", "eye", "eyeD", "eyeL", "white", "blush", "mouth",
    "outfit", "outfitS", "outfitL", "pants", "pantsS", "shoe", "acc", "accS", "accL",
    "zz", "moon", "star", "cushion", "cushionL", "rock", "rockD", "ore", "wood", "woodD", "metal", "metalD",
    "paper", "ink", "yellow", "yellowL", "red", "green", "bubble", "grey", "greyD", "greyL", "cloud", "cloudD",
    "drop", "floor", "floorD",
)
SCENE = {
    "zz": "#7f8fd6", "moon": "#f5d77a", "star": "#e8b93a", "cushion": "#8fa3d9", "cushionL": "#aebcf0",
    "rock": "#8b8fa3", "rockD": "#6e7286", "ore": "#5fd3f3", "wood": "#b0743f", "woodD": "#8a5530",
    "metal": "#aab3cc", "metalD": "#5b6178", "paper": "#fbfaf5", "ink": "#2b2230", "yellow": "#ffd84d",
    "yellowL": "#fff3b0", "red": "#e5484d", "green": "#3fb36b", "bubble": "#ffffff", "grey": "#b6bccb",
    "greyD": "#8d93a3", "greyL": "#d5d9e2", "cloud": "#c3c9d6", "cloudD": "#9aa2b3", "drop": "#6fb3f2",
    "floor": "#0000001a", "floorD": "#00000026",
}

SKIN = [("#ffe3cf", "#f2c3a6"), ("#f7cfae", "#e2a985"), ("#e8b48a", "#cc9067"), ("#c98d60", "#a96f47"),
        ("#97603d", "#7a4a2d"), ("#5f3a24", "#4a2b1a")]
HAIR = [("#2b2735", "#1c1924", "#4f4a66"), ("#5b3a26", "#432a1b", "#8a5a3b"), ("#c98a3e", "#a26b2b", "#efbd76"),
        ("#e8c766", "#c9a544", "#fff0a8"), ("#d9573f", "#b0402d", "#f58a6e"), ("#8e5cd6", "#6c41ad", "#bb96f2"),
        ("#3fb0a7", "#2c8c84", "#86e3d9"), ("#f08fbd", "#d06a9c", "#ffc6df"), ("#eceaf3", "#c4c0d3", "#ffffff"),
        ("#4b7be0", "#3459b5", "#93b3ff"), ("#6fbf5a", "#4f9940", "#ade692")]
OUTFIT = [("#4e79d6", "#3a5ba8", "#86a8f0"), ("#e0773a", "#b85a24", "#f6a676"), ("#3f9e6e", "#2c7a52", "#7dcca1"),
          ("#c2456c", "#9a3152", "#e8829f"), ("#7a5bc7", "#5c41a0", "#a991e8"), ("#2f8fb8", "#21708f", "#71c3e5"),
          ("#e0b12a", "#b88b17", "#f6d87e"), ("#5c6b8a", "#45516b", "#8f9bb8"), ("#ef6f6c", "#c9504e", "#ffa3a0"),
          ("#3a3d4f", "#2a2c3a", "#686c86")]
PANTS = [("#3b3f58", "#2c2f42"), ("#5a4636", "#45352a"), ("#2f4f7a", "#233a5b"), ("#4d5a3a", "#3a442b"),
         ("#6a6f85", "#535767")]
EYES = [("#5a3a2a", "#8a5f45"), ("#3a6fd0", "#7aa6f2"), ("#3d9a5f", "#79cf97"), ("#8a4fd0", "#b78cf0"),
        ("#c9861e", "#f0b85a"), ("#d04f7a", "#f28aab")]
ACC_COLORS = [("#ff6f91", "#d94f74", "#ffb3c6"), ("#ffd84d", "#d9b22c", "#fff0a6"), ("#5fd3f3", "#33a9c9", "#b2efff"),
              ("#9b7bea", "#7657c9", "#cdbaff"), ("#3a3f55", "#272a3a", "#8b93b5")]
STYLES = ["bob", "long", "spiky", "buns", "ponytail", "pixie", "curly", "sidepart"]
ACCS = ["none", "none", "glasses", "bow", "headband", "catears", "beanie", "headphones", "flower"]
TOPS = ["tee", "hoodie", "overalls", "stripe", "collar"]

Canvas = list  # W*W cells, each a slot name or None
Test = Callable[[int, int], bool]


def _round(x: float) -> int:
    """JavaScript ``Math.round`` (halves round up), not Python's banker's rounding."""
    return math.floor(x + 0.5)


def _rng(seed: int) -> Callable[[], float]:
    # The JS multiplies as doubles, so large seeds lose low bits before ToUint32; do the same.
    a = int(float(seed) * 2654435761.0) % 2**32

    def nxt() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & M32
        t = a
        t = ((t ^ (t >> 15)) * (t | 1)) & M32
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & M32)) & M32
        return ((t ^ (t >> 14)) & M32) / 4294967296

    return nxt


def _pick(r: Callable[[], float], xs: list) -> object:
    return xs[math.floor(r() * len(xs))]


@dataclass(frozen=True)
class Look:
    style: str
    acc: str
    top: str
    pal: dict[str, str]


def make_look(seed: int) -> Look:
    r = _rng(seed + 11)
    skin, hair, fit, pants, eye = _pick(r, SKIN), _pick(r, HAIR), _pick(r, OUTFIT), _pick(r, PANTS), _pick(r, EYES)
    acc = _pick(r, ACCS)
    if acc == "glasses":
        acc_c = ("#2e2a35", "#2e2a35", "#2e2a35")
    elif acc == "headphones":
        acc_c = ACC_COLORS[4]
    else:
        acc_c = _pick(r, ACC_COLORS[:4])
    style = _pick(r, STYLES)
    top = _pick(r, TOPS)
    pal = {
        "outline": "#2b2230", "skin": skin[0], "skinS": skin[1], "hair": hair[0], "hairS": hair[1],
        "hairL": hair[2], "eye": eye[0], "eyeD": "#24202c", "eyeL": eye[1], "white": "#ffffff", "blush": "#f59aa6",
        "mouth": "#a3404f", "outfit": fit[0], "outfitS": fit[1], "outfitL": fit[2], "pants": pants[0],
        "pantsS": pants[1], "shoe": "#2a2530", "acc": acc_c[0], "accS": acc_c[1], "accL": acc_c[2],
    }
    return Look(style=style, acc=acc, top=top, pal=pal)


def _grey_hex(hex_: str) -> str:
    if len(hex_) != 7:
        return hex_
    n = int(hex_[1:], 16)
    r, g, b = n >> 16, (n >> 8) & 255, n & 255
    lum = _round(0.299 * r + 0.587 * g + 0.114 * b)
    m = _round(lum * 0.75 + 0x9A * 0.25)

    def h(v: int) -> str:
        return format(max(0, min(255, v)), "02x")

    return "#" + h(m) + h(m) + h(m + 6)


# --- drawing primitives -------------------------------------------------------------


def _canvas() -> Canvas:
    return [None] * (W * W)


def _set(c: Canvas, x: int, y: int, k: str | None) -> None:
    if 0 <= x < W and 0 <= y < W:
        c[y * W + x] = k


def _get(c: Canvas, x: int, y: int) -> str | None:
    return c[y * W + x] if 0 <= x < W and 0 <= y < W else None


def _ell(c: Canvas, cx: float, cy: float, rx: float, ry: float, k: str | None, test: Test | None = None) -> None:
    for y in range(max(0, math.floor(cy - ry - 1)), min(W - 1, math.ceil(cy + ry + 1)) + 1):
        for x in range(max(0, math.floor(cx - rx - 1)), min(W - 1, math.ceil(cx + rx + 1)) + 1):
            dx, dy = (x + 0.5 - cx) / rx, (y + 0.5 - cy) / ry
            if dx * dx + dy * dy <= 1 and (test is None or test(x, y)):
                _set(c, x, y, k)


def _rect(c: Canvas, x: int, y: int, w: int, h: int, k: str) -> None:
    for j in range(h):
        for i in range(w):
            _set(c, x + i, y + j, k)


def _rrect(c: Canvas, x: int, y: int, w: int, h: int, r: int, k: str) -> None:
    for j in range(h):
        for i in range(w):
            cx = r - i - 0.5 if i < r else (i - (w - r) + 0.5 if i >= w - r else 0)
            cy = r - j - 0.5 if j < r else (j - (h - r) + 0.5 if j >= h - r else 0)
            if cx * cx + cy * cy <= r * r:
                _set(c, x + i, y + j, k)


def _pts(c: Canvas, points: list[tuple[int, int]], k: str) -> None:
    for x, y in points:
        _set(c, x, y, k)


def _recolor(c: Canvas, frm: str, to: str, test: Test) -> None:
    for y in range(W):
        for x in range(W):
            if c[y * W + x] == frm and test(x, y):
                c[y * W + x] = to


def _blit(dst: Canvas, src: Canvas, dx: int, dy: int = 0) -> None:
    for y in range(W):
        for x in range(W):
            k = src[y * W + x]
            if k:
                _set(dst, x + dx, y + dy, k)


def _outline_all(c: Canvas) -> None:
    add = [
        y * W + x
        for y in range(W)
        for x in range(W)
        if not c[y * W + x]
        and (_get(c, x + 1, y) or _get(c, x - 1, y) or _get(c, x, y + 1) or _get(c, x, y - 1))
    ]
    for i in add:
        c[i] = "outline"


def _in_head(x: int, y: int) -> bool:
    dx, dy = (x + 0.5 - 24) / 13, (y + 0.5 - 18) / 12
    return dx * dx + dy * dy <= 1


# --- character parts (the approved 48×48 sheet) ------------------------------------


def _hair_back(c: Canvas, st: str) -> None:
    if st == "long":
        _ell(c, 24, 20, 16, 15, "hairS")
        _rect(c, 9, 20, 30, 17, "hairS")
    if st in ("bob", "buns", "sidepart"):
        _ell(c, 24, 19, 15.5, 14, "hairS", lambda x, y: y <= 31)
    if st == "ponytail":
        _ell(c, 24, 19, 15, 13.5, "hairS", lambda x, y: y <= 29)
        _ell(c, 38.5, 27, 4, 9, "hairS")
        _ell(c, 38.5, 24, 3, 5, "hair")
    if st == "curly":
        for a in range(14):
            ang = a / 14 * math.pi * 2
            _ell(c, 24 + math.cos(ang) * 13.5, 18 + math.sin(ang) * 11.5, 5, 5, "hairS", lambda x, y: y <= 31)
    if st in ("spiky", "pixie"):
        _ell(c, 24, 17, 14, 12.5, "hairS", lambda x, y: y <= 24)
    if st == "buns":
        _ell(c, 12, 8, 5, 5, "hair")
        _ell(c, 36, 8, 5, 5, "hair")
        _set(c, 10, 6, "hairL")
        _set(c, 34, 6, "hairL")


def _body(c: Canvas, top: str, o: dict) -> None:
    sit, left, right = o.get("body") == "sit", o.get("l") or "down", o.get("r") or "down"
    if left == "down":
        _rrect(c, 13, 30, 4, 9, 2, "outfit")
        _ell(c, 15, 39.5, 2.2, 2, "skin")
    if left == "lap":
        _rrect(c, 14, 31, 4, 7, 2, "outfit")
        _ell(c, 17, 38.5, 2.2, 2, "skin")
    if left == "gesture":
        _rect(c, 9, 30, 9, 4, "outfit")
        _ell(c, 8, 31.5, 2.2, 2.2, "skin")
    if left == "paper":
        _rrect(c, 13, 30, 4, 7, 2, "outfit")
        _ell(c, 14.5, 37.5, 2.2, 2, "skin")
    if right == "down":
        _rrect(c, 31, 30, 4, 9, 2, "outfit")
        _ell(c, 33, 39.5, 2.2, 2, "skin")
    if right == "lap":
        _rrect(c, 30, 31, 4, 7, 2, "outfit")
        _ell(c, 31, 38.5, 2.2, 2, "skin")
    if right == "hold":
        _rrect(c, 31, 30, 4, 7, 2, "outfit")
        _ell(c, 33, 37.5, 2.2, 2, "skin")
    if right == "lift":
        _rect(c, 30, 29, 7, 3, "outfit")
        _rrect(c, 36, 22, 4, 10, 2, "outfit")
        _ell(c, 38, 21, 2.2, 2.2, "skin")
    ty = 29 if sit else 28
    if sit:
        _rect(c, 15, 40, 18, 3, "pants")
        _rect(c, 15, 42, 18, 1, "pantsS")
        _rrect(c, 11, 39, 5, 4, 1, "shoe")
        _rrect(c, 32, 39, 5, 4, 1, "shoe")
        _rrect(c, 17, ty, 14, 12, 4, "outfit")
    else:
        _rect(c, 19, 40, 4, 4, "pants")
        _rect(c, 25, 40, 4, 4, "pants")
        _rect(c, 22, 40, 1, 4, "pantsS")
        _rect(c, 28, 40, 1, 4, "pantsS")
        _rrect(c, 18, 43, 6, 3, 1, "shoe")
        if not o.get("noRightShoe"):
            _rrect(c, 24, 43, 6, 3, 1, "shoe")
        _rrect(c, 17, ty, 14, 13, 4, "outfit")
    _recolor(c, "outfit", "outfitS", lambda x, y: (28 <= x <= 30 and y >= ty + 2) or x == 34)
    if top == "tee":
        _rect(c, 22, ty, 4, 1, "skin")
        _set(c, 21, ty, "outfitL")
        _set(c, 26, ty, "outfitL")
    if top == "hoodie":
        _rect(c, 19, ty, 10, 2, "outfitL")
        _rect(c, 22, ty + 2, 1, 4, "white")
        _rect(c, 25, ty + 2, 1, 4, "white")
        _rect(c, 20, ty + 7, 8, 3, "outfitS")
    if top == "overalls":
        _rect(c, 20, ty + 4, 8, 9 - (1 if sit else 0), "pants")
        _rect(c, 20, ty, 2, 4, "pants")
        _rect(c, 26, ty, 2, 4, "pants")
        _set(c, 21, ty + 4, "white")
        _set(c, 26, ty + 4, "white")
    if top == "stripe":
        _recolor(c, "outfit", "outfitL", lambda x, y: y in (ty + 5, ty + 8))
        _recolor(c, "outfitS", "outfitL", lambda x, y: y in (ty + 5, ty + 8))
    if top == "collar":
        _rect(c, 21, ty, 3, 1, "white")
        _rect(c, 24, ty, 3, 1, "white")
        _set(c, 22, ty + 1, "white")
        _set(c, 25, ty + 1, "white")
        for by in range(ty + 3, ty + 11, 3):
            _set(c, 23, by, "outfitS")


def _head(c: Canvas) -> None:
    _ell(c, 10.5, 19.5, 2.2, 3, "skin")
    _ell(c, 37.5, 19.5, 2.2, 3, "skin")
    _ell(c, 24, 18, 13, 12, "skin")
    _recolor(c, "skin", "skinS", lambda x, y: 24 <= y <= 30 and 30 <= x <= 37 and _in_head(x, y))
    _set(c, 9, 20, "skinS")
    _set(c, 38, 20, "skinS")


def _face(c: Canvas, eyes: str, mouth: str) -> None:
    for ex in (17, 27):
        left = 1 if ex == 17 else 0
        if eyes == "closed":
            _pts(c, [(ex, 20), (ex + 1, 21), (ex + 2, 21), (ex + 3, 20)], "eyeD")
        elif eyes == "happy":
            _pts(c, [(ex, 21), (ex + 1, 20), (ex + 2, 20), (ex + 3, 21)], "eyeD")
        elif eyes == "x":
            _pts(c, [(ex, 19), (ex + 3, 19), (ex + 1, 20), (ex + 2, 20), (ex + 1, 21), (ex + 2, 21),
                     (ex, 22), (ex + 3, 22)], "eyeD")
        elif eyes == "tired":
            _rect(c, ex, 20, 4, 1, "eyeD")
            _rect(c, ex, 21, 4, 1, "eye")
            _rect(c, ex, 22, 4, 1, "eyeL")
        elif eyes == "focus":
            _rect(c, ex - left, 19, 5, 1, "eyeD")
            _rect(c, ex, 20, 4, 2, "eye")
            _rect(c, ex, 22, 4, 1, "eyeL")
            _set(c, ex, 20, "white")
        else:
            _rect(c, ex, 19, 4, 4, "eye")
            _rect(c, ex, 22, 4, 1, "eyeL")
            _rect(c, ex - left, 18, 5, 1, "eyeD")
            _rect(c, ex, 19, 2, 2, "white")
            _set(c, ex + 3, 21, "white")
    _ell(c, 15.5, 24.5, 2, 1, "blush")
    _ell(c, 31.5, 24.5, 2, 1, "blush")
    if mouth == "smile":
        _pts(c, [(22, 25), (23, 26), (24, 26), (25, 25)], "mouth")
    if mouth == "open":
        _rect(c, 22, 25, 4, 2, "mouth")
        _rect(c, 23, 26, 2, 1, "blush")
    if mouth == "tiny":
        _rect(c, 23, 25, 2, 1, "mouth")
    if mouth == "flat":
        _rect(c, 22, 25, 4, 1, "mouth")
    if mouth == "wavy":
        _pts(c, [(21, 25), (22, 26), (23, 25), (24, 26), (25, 25), (26, 26)], "mouth")


def _depth_for(st: str, x: int) -> int:
    if st == "spiky":
        return 2 + (3 if (x * 3) % 7 > 3 else 1)
    if st == "pixie":
        return 2 + (3 if x % 3 == 0 else 1)
    if st == "curly":
        return 3 + (x // 2 % 2) * 2
    if st == "sidepart":
        return 2 if x < 21 else min(5, 2 + (x - 20) // 2)
    return 3 + (1 if x % 5 == 0 else 0)


def _hair_front(c: Canvas, st: str) -> None:
    cap_y = 12
    for y in range(cap_y + 1):
        for x in range(W):
            if _in_head(x, y):
                _set(c, x, y, "hair")
    for bx in range(12, 36):
        d = _depth_for(st, bx)
        for by in range(cap_y + 1, cap_y + d + 1):
            if _in_head(bx, by):
                _set(c, bx, by, "hair")
        if _in_head(bx, cap_y + d):
            _set(c, bx, cap_y + d, "hairS")
    if st in ("bob", "long", "sidepart"):
        _rect(c, 11, 13, 3, 11, "hair")
        _rect(c, 34, 13, 3, 11, "hair")
        _rect(c, 11, 23, 3, 1, "hairS")
        _rect(c, 34, 23, 3, 1, "hairS")
    if st in ("buns", "ponytail", "pixie"):
        _rect(c, 11, 13, 2, 6, "hair")
        _rect(c, 35, 13, 2, 6, "hair")
    if st == "curly":
        _ell(c, 12, 16, 3, 5, "hair")
        _ell(c, 36, 16, 3, 5, "hair")
    if st == "spiky":
        for sx in (14, 19, 24, 29, 34):
            for r in range(5):
                _rect(c, sx - r // 2, 2 + r, 1 + r, 1, "hair")
    _recolor(c, "hair", "hairS", lambda x, y: x >= 32 and y >= 9)
    for x, y in ((16, 10), (17, 9), (18, 9), (19, 8), (20, 8), (21, 8)):
        if _get(c, x, y) == "hair":
            _set(c, x, y, "hairL")


def _accessory(c: Canvas, acc: str) -> None:
    if acc == "glasses":
        for ex in (17, 27):
            _rect(c, ex - 1, 17, 6, 1, "acc")
            _rect(c, ex - 1, 23, 6, 1, "acc")
            _rect(c, ex - 1, 17, 1, 7, "acc")
            _rect(c, ex + 4, 17, 1, 7, "acc")
        _rect(c, 22, 19, 4, 1, "acc")
    if acc == "bow":
        _rect(c, 28, 5, 3, 5, "acc")
        _rect(c, 33, 5, 3, 5, "acc")
        _rect(c, 31, 6, 2, 3, "accS")
        _set(c, 28, 5, "accL")
        _set(c, 33, 5, "accL")
        _set(c, 29, 6, "accL")
    if acc == "headband":
        for x in range(W):
            for y in (10, 11):
                if _in_head(x, y):
                    _set(c, x, y, "accL" if y == 10 else "acc")
    if acc == "catears":
        for r in range(6):
            _rect(c, 13, 1 + r, 1 + r, 1, "hair")
            _rect(c, 34 - r, 1 + r, 1 + r, 1, "hair")
        for q in range(2, 5):
            _rect(c, 14, 2 + q, q - 1, 1, "blush")
            _rect(c, 34 - q + 1, 2 + q, q - 1, 1, "blush")
    if acc == "beanie":
        _ell(c, 24, 12, 14, 8, "acc", lambda x, y: y <= 12)
        _rect(c, 11, 11, 26, 3, "accS")
        for sx in range(14, 34, 4):
            _rect(c, sx, 5, 1, 6, "accL")
        _ell(c, 24, 3.5, 2.5, 2.5, "accL")
    if acc == "headphones":
        for hy in range(15):
            for hx in range(W):
                ddx, ddy = (hx + 0.5 - 24) / 15, (hy + 0.5 - 18) / 14
                dd = ddx * ddx + ddy * ddy
                if 0.82 <= dd <= 1:
                    _set(c, hx, hy, "acc")
        _rrect(c, 8, 15, 4, 8, 2, "acc")
        _rrect(c, 36, 15, 4, 8, 2, "acc")
        _rect(c, 9, 17, 1, 4, "accL")
        _rect(c, 38, 17, 1, 4, "accL")
    if acc == "flower":
        _pts(c, [(13, 9), (15, 9), (13, 11), (15, 11), (14, 8), (14, 12), (12, 10), (16, 10)], "acc")
        _set(c, 14, 10, "accL")
        _set(c, 14, 9, "accS")


def _make_char(lk: Look, **o: object) -> Canvas:
    c = _canvas()
    _hair_back(c, lk.style)
    _body(c, lk.top, o)
    _head(c)
    _face(c, str(o.get("eyes") or "open"), str(o.get("mouth") or "smile"))
    _hair_front(c, lk.style)
    _accessory(c, lk.acc)
    return c


# --- props --------------------------------------------------------------------------


def _back_view(c: Canvas) -> None:
    _rrect(c, 13, 30, 4, 9, 2, "greyD")
    _rrect(c, 31, 30, 4, 9, 2, "greyD")
    _rect(c, 19, 40, 4, 4, "grey")
    _rect(c, 25, 40, 4, 4, "grey")
    _rrect(c, 18, 43, 6, 3, 1, "greyD")
    _rrect(c, 24, 43, 6, 3, 1, "greyD")
    _rrect(c, 17, 28, 14, 13, 4, "grey")
    _ell(c, 24, 18, 13, 12, "greyL")
    _ell(c, 24, 17, 13.5, 12, "greyD", lambda x, y: y <= 26)


def _raised_arm(c: Canvas) -> None:
    _rect(c, 11, 27, 7, 3, "outfit")
    _rrect(c, 8, 14, 4, 15, 2, "outfit")
    _ell(c, 10, 12.5, 2.6, 2.4, "skin")


def _thumb_arm(c: Canvas) -> None:
    _rect(c, 30, 29, 7, 3, "outfit")
    _rrect(c, 36, 21, 4, 11, 2, "outfit")
    _ell(c, 38, 19.5, 2.4, 2.2, "skin")
    _rect(c, 37, 15, 2, 3, "skin")


def _floor_band(c: Canvas) -> None:
    _rect(c, 0, 45, 48, 3, "floor")


def _cushion(c: Canvas) -> None:
    _ell(c, 24, 45.5, 20, 3.2, "cushion")
    _recolor(c, "cushion", "cushionL", lambda x, y: y <= 43)


@dataclass
class Layer:
    canvas: Canvas
    cls: str = ""
    kind: str = "char"  # 'scene' (backdrop props) or 'char' (part of the figure, kept at list size)
    outline: bool = True


def _filled(draw: Callable[[Canvas], None]) -> Canvas:
    c = _canvas()
    draw(c)
    return c


# --- the eleven poses ---------------------------------------------------------------


def _idle(lk: Look) -> list[Layer]:
    sky = _canvas()
    _ell(sky, 7, 6, 3.5, 3.5, "moon")
    _ell(sky, 9, 5, 3, 3, None)
    _pts(sky, [(16, 3), (40, 8), (3, 16), (44, 20)], "star")
    ch = _make_char(lk, body="sit", l="lap", r="lap", eyes="closed", mouth="tiny")
    z1 = _canvas()
    _rect(z1, 37, 14, 4, 1, "zz")
    _set(z1, 39, 15, "zz")
    _set(z1, 38, 16, "zz")
    _rect(z1, 37, 17, 4, 1, "zz")
    z2 = _canvas()
    _rect(z2, 42, 8, 3, 1, "zz")
    _set(z2, 43, 9, "zz")
    _rect(z2, 42, 10, 3, 1, "zz")
    return [Layer(sky, "", "scene", False), Layer(_filled(_cushion), "", "scene"), Layer(ch, "a-breathe"),
            Layer(z1, "a-z1", "char", False), Layer(z2, "a-z2", "char", False)]


def _working(lk: Look) -> list[Layer]:
    rock = _canvas()
    _ell(rock, 41, 41, 8, 6, "rock", lambda x, y: y <= 45)
    _recolor(rock, "rock", "rockD", lambda x, y: y >= 43)
    _pts(rock, [(39, 39), (43, 41), (41, 43), (45, 38)], "ore")
    ch = _canvas()
    _blit(ch, _make_char(lk, r="hold", eyes="focus", mouth="smile"), -7)
    pk = _canvas()
    _pts(pk, [(27, 36), (28, 35), (29, 34), (30, 33), (31, 32), (32, 31), (33, 30), (34, 29)], "wood")
    _pts(pk, [(31, 26), (32, 25), (33, 25), (34, 25), (35, 25), (36, 26), (37, 27), (38, 28), (39, 29), (40, 30)],
         "metal")
    _pts(pk, [(32, 26), (33, 26), (34, 26), (35, 26), (36, 27), (37, 28), (38, 29)], "metalD")
    _ell(pk, 26, 37.5, 2.2, 2, "skin")
    sp = _canvas()
    _pts(sp, [(33, 37), (34, 36)], "yellow")
    _set(sp, 35, 38, "yellowL")
    _set(sp, 32, 35, "yellowL")
    return [Layer(_filled(_floor_band), "", "scene", False), Layer(rock, "", "scene"), Layer(ch),
            Layer(pk, "a-swing"), Layer(sp, "a-spark", "scene", False)]


def _reviewing(lk: Look) -> list[Layer]:
    paper = _canvas()
    _rect(paper, 6, 31, 8, 10, "paper")
    _rect(paper, 7, 33, 6, 1, "ink")
    _rect(paper, 7, 35, 5, 1, "ink")
    _rect(paper, 7, 37, 6, 1, "ink")
    ch = _make_char(lk, l="paper", r="lift", eyes="focus", mouth="flat")
    mg = _canvas()
    for y in range(W):
        for x in range(W):
            ax, ay = x + 0.5 - 29, y + 0.5 - 20.5
            d = math.sqrt(ax * ax + ay * ay)
            if 3.4 <= d <= 4.7:
                _set(mg, x, y, "metal")
    _pts(mg, [(33, 24), (34, 24), (35, 23), (36, 22)], "metalD")
    return [Layer(_filled(_floor_band), "", "scene", False), Layer(paper), Layer(ch), Layer(mg, "a-scan")]


def _needs_you(lk: Look) -> list[Layer]:
    ch = _make_char(lk, l="none", r="down", eyes="open", mouth="open")
    desk = _canvas()
    _rect(desk, 1, 35, 46, 2, "wood")
    _rect(desk, 3, 37, 42, 11, "woodD")
    _rect(desk, 30, 27, 13, 8, "metal")
    _set(desk, 36, 30, "paper")
    _rect(desk, 28, 34, 17, 1, "metalD")
    _rect(desk, 5, 30, 4, 5, "red")
    _set(desk, 9, 31, "red")
    _set(desk, 9, 33, "red")
    bub = _canvas()
    _rrect(bub, 37, 1, 10, 10, 2, "bubble")
    _pts(bub, [(38, 11), (38, 12)], "bubble")
    _rect(bub, 41, 3, 2, 4, "red")
    _rect(bub, 41, 8, 2, 1, "red")
    return [Layer(ch), Layer(_filled(_raised_arm), "a-wave"), Layer(desk, "", "scene"), Layer(bub, "a-pulse")]


def _waiting(lk: Look) -> list[Layer]:
    ahead = _canvas()
    _blit(ahead, _filled(_back_view), -18)
    ch = _canvas()
    _blit(ch, _make_char(lk, noRightShoe=True, mouth="flat"), 8)
    foot = _canvas()
    _rrect(foot, 32, 43, 6, 3, 1, "shoe")
    dots = []
    for x, cls in ((27, "a-dot"), (31, "a-dot a-dot2"), (35, "a-dot a-dot3")):
        d = _canvas()
        _rect(d, x, 0, 2, 2, "greyD")
        dots.append(Layer(d, cls, "char", False))
    return [Layer(_filled(_floor_band), "", "scene", False), Layer(ahead, "", "scene"), Layer(ch),
            Layer(foot, "a-tap"), *dots]


def _talking(lk: Look) -> list[Layer]:
    ch = _canvas()
    _blit(ch, _make_char(lk, l="gesture", mouth="open"), 4)
    b1 = _canvas()
    _rrect(b1, 33, 0, 14, 9, 2, "bubble")
    _pts(b1, [(35, 9), (35, 10)], "bubble")
    for x in (36, 40, 44):
        _rect(b1, x, 4, 2, 2, "ink")
    b2 = _canvas()
    _rrect(b2, 0, 3, 13, 9, 2, "greyL")
    _pts(b2, [(10, 12), (11, 12)], "greyL")
    for x in (2, 5, 8):
        _rect(b2, x, 7, 2, 2, "greyD")
    return [Layer(_filled(_floor_band), "", "scene", False), Layer(ch), Layer(b1, "a-talk1"), Layer(b2, "a-talk2")]


def _part_done(lk: Look) -> list[Layer]:
    ch = _make_char(lk, r="none", eyes="happy", mouth="open")
    badge = _canvas()
    _ell(badge, 43, 5, 4, 4, "green")
    _pts(badge, [(41, 5), (42, 6), (43, 5), (44, 4), (45, 3)], "paper")
    _pts(badge, [(6, 9), (9, 5), (4, 4)], "yellow")
    return [Layer(_filled(_floor_band), "", "scene", False), Layer(ch), Layer(_filled(_thumb_arm), "a-bob"),
            Layer(badge, "a-twinkle", "char", False)]


def _recharging(lk: Look) -> list[Layer]:
    ch = _make_char(lk, body="sit", l="lap", r="lap", eyes="tired", mouth="flat")
    frame = _canvas()
    _rect(frame, 33, 3, 12, 1, "ink")
    _rect(frame, 33, 10, 12, 1, "ink")
    _rect(frame, 33, 3, 1, 8, "ink")
    _rect(frame, 44, 3, 1, 8, "ink")
    _rect(frame, 45, 5, 2, 4, "ink")
    cells = []
    for x, cls in ((35, "a-chg1"), (38, "a-chg2"), (41, "a-chg3")):
        cell = _canvas()
        _rect(cell, x, 5, 2, 4, "green")
        cells.append(Layer(cell, cls, "char", False))
    return [Layer(_filled(_cushion), "", "scene"), Layer(ch), Layer(frame, "", "char", False), *cells]


def _error(lk: Look) -> list[Layer]:
    ch = _make_char(lk, eyes="x", mouth="wavy")
    cl = _canvas()
    _ell(cl, 24, 3, 8, 3, "cloud")
    _ell(cl, 18, 4.5, 4.5, 3, "cloud")
    _ell(cl, 30, 4.5, 4.5, 3, "cloud")
    _recolor(cl, "cloud", "cloudD", lambda x, y: y >= 6)
    rain = _canvas()
    for x, y in ((17, 9), (21, 11), (26, 9), (30, 11)):
        _rect(rain, x, y, 1, 2, "drop")
    return [Layer(_filled(_floor_band), "", "scene", False), Layer(ch), Layer(cl, "a-drift"),
            Layer(rain, "a-rain", "char", False)]


def _offline(lk: Look) -> list[Layer]:
    ch = _make_char(lk, body="sit", l="lap", r="lap", eyes="closed", mouth="tiny")
    plug = _canvas()
    _rect(plug, 37, 42, 7, 2, "greyD")
    _rect(plug, 44, 41, 2, 1, "grey")
    _rect(plug, 44, 44, 2, 1, "grey")
    return [Layer(_filled(_cushion), "", "scene"), Layer(ch), Layer(plug, "", "char", False)]


def _janitor(lk: Look) -> list[Layer]:
    ch = _make_char(lk, l="none", r="none", eyes="open", mouth="smile")
    br = _canvas()
    _rrect(br, 15, 30, 4, 8, 2, "outfit")
    _rrect(br, 30, 30, 4, 6, 2, "outfit")
    for i in range(22):
        x, y = 36 - i, _round(27 + i * 16 / 21)
        _set(br, x, y, "wood")
        _set(br, x, y + 1, "woodD")
    _rect(br, 8, 42, 9, 4, "yellow")
    _pts(br, [(9, 43), (11, 43), (13, 43), (15, 43)], "yellowL")
    _rect(br, 15, 41, 3, 2, "woodD")
    _ell(br, 32, 31.5, 2.2, 2.2, "skin")
    _ell(br, 22, 37.5, 2.2, 2.2, "skin")
    puff = _canvas()
    _pts(puff, [(5, 44), (6, 43), (4, 42), (7, 41), (3, 44)], "cloud")
    return [Layer(_filled(_floor_band), "", "scene", False), Layer(ch), Layer(br, "a-sweep"),
            Layer(puff, "a-puff", "scene", False)]


_DRAW: dict[str, Callable[[Look], list[Layer]]] = {
    "idle": _idle, "working": _working, "reviewing": _reviewing, "needs_you": _needs_you, "waiting": _waiting,
    "talking": _talking, "part_done": _part_done, "recharging": _recharging, "error": _error,
    "offline": _offline, "janitor": _janitor,
}
_GREY = frozenset({"offline"})


# --- animation (the board's CSS, in viewBox px; every class runs on a layer <g>) ----

_KEYFRAMES = {
    "kb1": "@keyframes kb1{0%,49%{transform:translateY(0)}50%,100%{transform:translateY(-1px)}}",
    "kz": "@keyframes kz{0%{opacity:0;transform:translate(0,2px)}25%{opacity:1}"
          "100%{opacity:0;transform:translate(2px,-4px)}}",
    "ksw": "@keyframes ksw{from{transform:rotate(-30deg)}to{transform:rotate(20deg)}}",
    "kbl": "@keyframes kbl{0%,49%{opacity:0}50%,100%{opacity:1}}",
    "ksc": "@keyframes ksc{0%,49%{transform:translateX(0)}50%,100%{transform:translateX(-1px)}}",
    "kwv": "@keyframes kwv{from{transform:rotate(-10deg)}to{transform:rotate(10deg)}}",
    "kpu": "@keyframes kpu{0%,60%{transform:translateY(0)}61%,100%{transform:translateY(-1px)}}",
    "kdt": "@keyframes kdt{0%,33%{opacity:1}34%,100%{opacity:.2}}",
    "ktk": "@keyframes ktk{0%,49%{opacity:1}50%,100%{opacity:.15}}",
    "kc1": "@keyframes kc1{0%,24%{opacity:0}25%,100%{opacity:1}}",
    "kc2": "@keyframes kc2{0%,49%{opacity:0}50%,100%{opacity:1}}",
    "kc3": "@keyframes kc3{0%,74%{opacity:0}75%,100%{opacity:1}}",
    "kdr": "@keyframes kdr{0%,49%{transform:translateX(0)}50%,100%{transform:translateX(1px)}}",
    "krn": "@keyframes krn{0%{opacity:1;transform:translateY(0)}100%{opacity:0;transform:translateY(4px)}}",
    "ksp": "@keyframes ksp{from{transform:rotate(-8deg)}to{transform:rotate(8deg)}}",
}
# class -> (rule body, keyframes it needs)
_CLASSES: dict[str, tuple[str, str]] = {
    "a-breathe": ("animation:kb1 2.4s steps(1) infinite", "kb1"),
    "a-z1": ("animation:kz 2.4s steps(4) infinite", "kz"),
    "a-z2": ("animation:kz 2.4s steps(4) infinite;animation-delay:1.2s", "kz"),
    "a-swing": ("transform-origin:26.5px 38px;animation:ksw .8s steps(3) infinite alternate", "ksw"),
    "a-spark": ("animation:kbl .8s steps(1) infinite", "kbl"),
    "a-scan": ("animation:ksc 1.6s steps(1) infinite", "ksc"),
    "a-wave": ("transform-origin:14px 29px;animation:kwv .9s steps(2) infinite alternate", "kwv"),
    "a-pulse": ("animation:kpu 1.2s steps(1) infinite", "kpu"),
    "a-tap": ("animation:kb1 .7s steps(1) infinite", "kb1"),
    "a-dot": ("opacity:.2;animation:kdt 1.5s steps(1) infinite", "kdt"),
    "a-dot2": ("animation-delay:.5s", ""),
    "a-dot3": ("animation-delay:1s", ""),
    "a-talk1": ("animation:ktk 2.4s steps(1) infinite", "ktk"),
    "a-talk2": ("animation:ktk 2.4s steps(1) infinite;animation-delay:-1.2s", "ktk"),
    "a-bob": ("animation:kb1 1s steps(1) infinite", "kb1"),
    "a-twinkle": ("animation:kbl 1.4s steps(1) infinite", "kbl"),
    "a-chg1": ("animation:kc1 3s steps(1) infinite", "kc1"),
    "a-chg2": ("animation:kc2 3s steps(1) infinite", "kc2"),
    "a-chg3": ("animation:kc3 3s steps(1) infinite", "kc3"),
    "a-drift": ("animation:kdr 2s steps(1) infinite", "kdr"),
    "a-rain": ("animation:krn .9s steps(3) infinite", "krn"),
    "a-sweep": ("transform-origin:28px 34px;animation:ksp .7s steps(2) infinite alternate", "ksp"),
    "a-puff": ("animation:kbl .7s steps(1) infinite", "kbl"),
}


def _style(classes: list[str]) -> str:
    rules, frames = ["g{transform-box:view-box}"], []
    for cls in _CLASSES:  # fixed order, so output is stable
        if cls in classes:
            body, kf = _CLASSES[cls]
            rules.append(f".{cls}{{{body}}}")
            if kf and _KEYFRAMES[kf] not in frames:
                frames.append(_KEYFRAMES[kf])
    rules.extend(frames)
    rules.append("@media (prefers-reduced-motion:reduce){g{animation:none!important}}")
    return "".join(rules)


# --- encoding -----------------------------------------------------------------------


def _paths(c: Canvas) -> dict[str, str]:
    """Row runs per slot, as the JS ``toPaths`` writes them."""
    d = {s: "" for s in SLOTS}
    for y in range(W):
        x = 0
        while x < W:
            k = c[y * W + x]
            if not k:
                x += 1
                continue
            x0 = x
            while x < W and c[y * W + x] == k:
                x += 1
            d[k] += f"M{x0} {y}h{x - x0}v1h-{x - x0}z"
    return d


def layers(version: int, seed: int, pose: str, *, scene: bool = True) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """``[(class, [(slot, path, color), ...]), ...]`` for each layer, bottom first, every slot listed."""
    _check(version, seed, pose)
    lk = make_look(seed)
    pal = {**SCENE, **lk.pal}
    if pose in _GREY:
        pal = {k: _grey_hex(v) for k, v in pal.items()}
    out = []
    for layer in _DRAW[pose](lk):
        if not scene and layer.kind != "char":
            continue
        if layer.outline:
            _outline_all(layer.canvas)
        d = _paths(layer.canvas)
        out.append((layer.cls, [(s, d[s], pal.get(s) or "#000000") for s in SLOTS]))
    return out


def render_svg(version: int, seed: int, pose: str, *, scene: bool = True) -> str:
    """The avatar as a standalone SVG document. ``scene=False`` is the character-only list-size version."""
    parts = []
    classes: list[str] = []
    for cls, slots in layers(version, seed, pose, scene=scene):
        body = "".join(f'<path d="{d}" fill="{color}"/>' for _, d, color in slots if d)
        if not body:
            continue
        classes.extend(cls.split())
        parts.append(f'<g class="{cls}">{body}</g>' if cls else f"<g>{body}</g>")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48" '
        f'shape-rendering="crispEdges"><style>{_style(classes)}</style>{"".join(parts)}</svg>'
    )


def _check(version: int, seed: int, pose: str) -> None:
    if version not in VERSIONS:
        raise ValueError(f"unknown avatar generator version {version!r}")
    if pose not in _DRAW:
        raise ValueError(f"unknown pose {pose!r}")
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2**32:
        raise ValueError("seed must be an integer in [0, 2**32)")


def new_seed() -> int:
    return secrets.randbelow(2**31)
