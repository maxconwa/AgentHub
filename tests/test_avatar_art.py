"""Avatar art: deterministic, versioned, validated, and free of anything but palette colors."""

import re

import pytest

from agenthub.hub import avatar_art
from agenthub.hub.avatar_art import GENERATOR_VERSION, POSE_LABELS, POSES, render_svg

FILLS = re.compile(r'fill="(#[0-9a-f]{6,8})"')


def test_poses_and_labels_match():
    assert len(POSES) == 12 and set(POSE_LABELS) == set(POSES)
    assert POSE_LABELS["needs_you"] == "needs you" and POSE_LABELS["waiting"] == "waiting on an agent"
    assert POSE_LABELS["failed"] == "failed" and POSE_LABELS["error"] == "something went wrong"


def test_failed_is_its_own_look():
    for seed in range(20):
        failed, error = render_svg(GENERATOR_VERSION, seed, "failed"), render_svg(GENERATOR_VERSION, seed, "error")
        assert failed != error
        assert 'fill="#e5484d"' in failed  # the red warning sign
        assert 'fill="#c3c9d6"' in error and 'fill="#c3c9d6"' not in failed  # the rain cloud is error's alone
        for cls in ("a-warn", "a-smoke1", "a-smoke2"):
            assert f'class="{cls}"' in failed and f".{cls}{{" in failed
        assert "a-rain" not in failed


def test_failed_keeps_the_sign_and_smoke_at_list_size():
    mini = render_svg(GENERATOR_VERSION, 5, "failed", scene=False)
    assert 'class="a-warn"' in mini and 'class="a-smoke1"' in mini
    assert "#0000001a" not in mini  # the floor is scene


def test_same_look_renders_the_same():
    for pose in POSES:
        assert render_svg(GENERATOR_VERSION, 42, pose) == render_svg(GENERATOR_VERSION, 42, pose)


def test_different_seeds_look_different():
    looks = {render_svg(GENERATOR_VERSION, seed, "idle") for seed in range(20)}
    assert len(looks) >= 15


@pytest.mark.parametrize("pose", POSES)
def test_every_pose_renders_for_many_seeds(pose):
    for seed in range(50):
        svg = render_svg(GENERATOR_VERSION, seed * 7919, pose)
        assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"')
        assert svg.endswith("</svg>") and "<path" in svg


def test_character_only_drops_scene_props():
    full = render_svg(GENERATOR_VERSION, 5, "idle")
    mini = render_svg(GENERATOR_VERSION, 5, "idle", scene=False)
    assert len(mini) < len(full)
    assert 'fill="#8fa3d9"' in full and 'fill="#8fa3d9"' not in mini  # the cushion
    assert 'fill="#7f8fd6"' in mini  # the Zs belong to the character


def test_offline_is_grey():
    for seed in range(20):
        for color in FILLS.findall(render_svg(GENERATOR_VERSION, seed, "offline")):
            if len(color) == 9:  # translucent floor shadow
                continue
            r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            assert max(r, g, b) - min(r, g, b) <= 8, color


@pytest.mark.parametrize(("version", "seed", "pose"), [
    (2, 1, "idle"), (0, 1, "idle"), (GENERATOR_VERSION, 1, "dancing"), (GENERATOR_VERSION, -1, "idle"),
    (GENERATOR_VERSION, 2**32, "idle"), (GENERATOR_VERSION, True, "idle"), (GENERATOR_VERSION, "1", "idle"),
])
def test_rejects_unknown_looks(version, seed, pose):
    with pytest.raises(ValueError):
        render_svg(version, seed, pose)


def test_largest_seed_renders():
    assert "<path" in render_svg(GENERATOR_VERSION, 2**32 - 1, "working")


def test_output_is_only_shapes_colors_and_animation():
    for pose in POSES:
        svg = render_svg(GENERATOR_VERSION, 3, pose)
        assert "<script" not in svg and "href" not in svg and "url(" not in svg and "@import" not in svg
        assert "@media (prefers-reduced-motion:reduce){g{animation:none!important}}" in svg
        # Every element is one of the fixed few.
        assert set(re.findall(r"<(\w+)", svg)) <= {"svg", "style", "g", "path"}
        assert set(FILLS.findall(svg)) == set(re.findall(r'fill="([^"]*)"', svg))


def test_animations_use_steps_only():
    for pose in POSES:
        style = re.search(r"<style>(.*)</style>", render_svg(GENERATOR_VERSION, 3, pose)).group(1)
        for timing in re.findall(r"animation:\w+ [\d.]+s (\S+)", style):
            assert timing.startswith("steps("), (pose, timing)


def test_new_seed_is_in_range():
    seeds = {avatar_art.new_seed() for _ in range(50)}
    assert all(0 <= s < 2**31 for s in seeds) and len(seeds) > 40
