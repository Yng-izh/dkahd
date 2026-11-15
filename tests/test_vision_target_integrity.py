"""Geometry and decoded-pixel regressions for target rendering integrity."""
import base64
import io
import math

import pytest

from dwg_reader.recognition import vision as V

Image = pytest.importorskip("PIL.Image")
ImageFont = pytest.importorskip("PIL.ImageFont")


@pytest.fixture(autouse=True)
def no_platform_font(monkeypatch):
    monkeypatch.setattr(V, "_font", lambda size: None)


def image(shot):
    assert shot is not None
    return Image.open(io.BytesIO(base64.b64decode(shot["b64"]))).convert("RGB")


def line(handle, start, end):
    return {"type": "LINE", "handle": handle, "start": start, "end": end}


def square(lo, hi):
    return [(lo, lo), (hi, lo), (hi, hi), (lo, hi)]


def render(entities=(), derived=(), **kwargs):
    case = {"image_bbox": [0, 0, 100, 100], "target_bbox": [10, 10, 30, 30],
            "source_handles": ["shared"]}
    case.update(kwargs.pop("case", {}))
    shot = V.render_case({"entities": entities, "derived": derived}, case,
                         width=200, **kwargs)
    return shot, image(shot)


def pixel(img, x, y):
    # render() fixes the explicit viewport to 0..100 and its width to 200.
    return img.getpixel((int(x * 2), int((100 - y) * 2)))


def test_reused_handle_highlights_only_target_intersection(monkeypatch):
    monkeypatch.setattr(V, "_font", lambda size: ImageFont.load_default())
    target = line("shared", (10, 20), (30, 20))
    other = line("shared", (60, 20), (80, 20))
    shot, img = render([{"type": "INSERT", "handle": "shared", "insert": [20, 20]}],
                       [target, other], numbering=True)
    assert shot["focus"] == 1
    assert shot["base"] == 1
    assert shot["numbers"] == 1
    assert not shot["focus_box"]
    assert pixel(img, 20, 20) == V.COLORS["focus"]
    assert pixel(img, 70, 20) == V.COLORS["base"]


def test_single_arc_is_not_reported_as_35_reused_instances():
    arcs = [{"type": "ARC", "handle": "shared", "center": [20 + i * 2, 20],
             "radius": 0.5, "start_angle": 350, "end_angle": 10}
            for i in range(35)]
    shot, img = render(derived=arcs, case={"target_bbox": [19, 19, 21, 21]})
    assert shot["focus"] == 1
    assert shot["base"] == 34
    assert pixel(img, 20.5, 20) == V.COLORS["focus"]
    assert pixel(img, 22.5, 20) == V.COLORS["base"]


def test_target_matching_uses_intersection_not_containment():
    shot, img = render([line("shared", (0, 20), (40, 20))])
    assert shot["focus"] == 1
    assert pixel(img, 20, 20) == V.COLORS["focus"]


def test_coincident_handle_duplicates_remain_ambiguous_inside_target():
    shot, _ = render([line("shared", (10, 20), (30, 20)),
                      line("shared", (10, 25), (30, 25))])
    assert shot["focus"] == 2


def test_insert_placeholder_does_not_hide_exact_target_fallback():
    shot, img = render([{"type": "INSERT", "handle": "shared", "insert": [20, 20]}],
                       case={"bbox": [0, 0, 100, 100]})
    assert shot["focus"] == shot["base"] == 0
    assert shot["focus_box"]
    assert pixel(img, 10, 20) == V.COLORS["focus"]
    assert pixel(img, 30, 20) == V.COLORS["focus"]
    assert pixel(img, 20, 10) == V.COLORS["focus"]
    assert pixel(img, 20, 30) == V.COLORS["focus"]
    assert pixel(img, 20, 20) == (255, 255, 255)
    assert pixel(img, 1, 20) == (255, 255, 255)


@pytest.mark.parametrize("kind", ["ARC", "CIRCLE"])
def test_radius_extents_keep_geometry_with_center_outside_view(kind):
    rec = {"type": kind, "handle": "shared", "center": [-10, 20], "radius": 30,
           "start_angle": 350, "end_angle": 10, "bbox": [-10, 20, -10, 20]}
    assert V._bbox_of(rec) == (-40, -10, 20, 50)
    shot, img = render([rec])
    assert shot["focus"] == 1
    assert any(pixel(img, x, 20) == V.COLORS["focus"] for x in (19, 19.5, 20))


@pytest.mark.parametrize("start,end,sweep", [(350, 10, 20), (350, 0, 10), (10, 0, 350)])
def test_arc_sweep_is_counterclockwise_and_zero_is_not_missing(start, end, sweep):
    segments = V._segments({"type": "ARC", "center": [0, 0], "radius": 10,
                            "start_angle": start, "end_angle": end})
    points = [segments[0][0]] + [b for _, b in segments]
    angles = [math.degrees(math.atan2(y, x)) % 360 for x, y in points]
    actual_sweep = sum((b - a) % 360 for a, b in zip(angles, angles[1:]))
    assert actual_sweep == pytest.approx(sweep)
    assert points[-1] == pytest.approx((10 * math.cos(math.radians(end)),
                                       10 * math.sin(math.radians(end))))
    if sweep < 180:
        assert all(x > 9 for x, _ in points)


def test_wrapped_arc_pixels_show_short_arc_not_complement():
    arc = {"type": "ARC", "handle": "shared", "center": [50, 50], "radius": 30,
           "start_angle": 350, "end_angle": 10}
    shot, img = render([arc], case={"target_bbox": [0, 0, 100, 100]})
    assert shot["focus"] == 1
    assert pixel(img, 80, 50) == V.COLORS["focus"]
    assert pixel(img, 20, 50) == (255, 255, 255)


@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_solid_hatch_even_odd_holes_islands_and_underlying_pixels(focused, reverse):
    loops = [square(10, 90), square(30, 70), square(45, 55)]
    if reverse:
        loops = [list(reversed(loop)) for loop in reversed(loops)]
    hatch = {"type": "HATCH", "handle": "shared" if focused else "context",
             "solid": True, "loops": loops}
    underlying = line("context-line", (20, 40), (80, 40))
    shot, img = render([underlying, hatch], case={"target_bbox": [0, 0, 100, 100]})
    fill = V.COLORS["focus_fill" if focused else "base_fill"]
    # Avoid the fallback perimeter when this hatch is only context.
    assert pixel(img, 20, 20) == fill
    assert pixel(img, 40, 40) == V.COLORS["base"]
    assert pixel(img, 40, 60) == (255, 255, 255)
    assert pixel(img, 50, 50) == fill
    assert shot["focus"] == int(focused)


def test_target_drawn_after_context_hatches_and_both_wall_sources():
    target = line("shared", (10, 20), (30, 20))
    hatch = {"type": "HATCH", "handle": "context", "solid": True,
             "loops": [square(5, 40)]}
    wall = line("wall", (0, 20), (40, 20))
    shot, img = render([target, hatch, wall], walls=[wall],
                       case={"context_handles": ["shared", "wall"]})
    assert shot["focus"] == 1
    assert pixel(img, 20, 20) == V.COLORS["focus"]
    assert pixel(img, 35, 20) == V.COLORS["wall"]


def test_explicit_viewport_has_no_second_padding():
    case = {"image_bbox": [-50, -50, 150, 150], "target_bbox": [10, 10, 30, 30]}
    assert V._window(case, thickness=10, pad_ratio=2) == (-50, -50, 150, 150)
    assert V._window({"bbox": [0, 0, 100, 100]}, thickness=10,
                     pad_ratio=2) == (-21, -21, 121, 121)


def test_render_views_preserves_scale_windows_and_exact_target_box(monkeypatch):
    calls = []
    original = V.render_case

    def capture(snapshot, case, **kwargs):
        calls.append((case, V._window(case, thickness=kwargs["thickness"], pad_ratio=2)))
        return original(snapshot, case, **kwargs)

    monkeypatch.setattr(V, "render_case", capture)
    target = [0, 0, 100, 100]
    result = V.render_views({"bbox": [-100, -100, 200, 200]}, {"bbox": target},
                            scales=("global", "local", "detail", "closeup"),
                            thickness=10, numbering=False)
    assert result["rendered"] == 4
    expected = [(-100, -100, 200, 200), (-60, -60, 160, 160),
                (-10, -10, 110, 110), (-2.5, -2.5, 102.5, 102.5)]
    for (case, viewport), view, bounds in zip(calls, result["views"], expected):
        assert case["target_bbox"] == target
        assert viewport == bounds
        assert view["focus_box"]
        img = image(view)
        scale = view["width"] / (bounds[2] - bounds[0])
        # Target left edge at y=50 must stay at x=0 in world coordinates.
        px = int(-bounds[0] * scale)
        py = int((bounds[3] - 50) * scale)
        assert img.getpixel((px, py)) == V.COLORS["focus"]
        assert img.getpixel((max(0, px - 4), py)) == (255, 255, 255)
