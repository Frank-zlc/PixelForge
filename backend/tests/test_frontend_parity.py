"""The frontend's coordinate maths must match the backend's, exactly.

`frontend/geometry.js` duplicates `geometry/mapper.py` on purpose: the crosshair
readout has to follow the cursor, and a round trip per mousemove would lag
visibly. Duplication is only safe while the two agree, so this test runs the real
JS through node and compares every value.

It caught a real defect on first run: the Python side rounded the letterbox offset
to an integer (because `Rect` holds ints) while JS kept it as a float. Half a CSS
pixel becomes more than a device pixel at a 2.35x frame-to-device ratio, and near
the edge it flipped the in-frame test itself -- a click on a black bar read as a
click on the first column.

Skipped when node is unavailable rather than failing: a Python-only checkout
should still have a green suite.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pixelforge.geometry.mapper import (
    CoordinateMapper,
    OutsideFrameError,
    Point,
    Rect,
    Size,
    Space,
)

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "geometry.js"

# Deliberately awkward: odd element sizes, encoder-aligned frames, letterboxing on
# both axes, and points exactly on the content edge.
CASES = [
    ((1080, 2400), (1080, 2400), (500, 1000),
     [(0, 0), (250, 500), (499, 999), (25, 0), (474, 999), (10, 10)]),
    ((1080, 2400), (460, 1024), (731, 397),
     [(0, 0), (365, 198), (300, 100), (500, 300)]),
    ((1440, 3200), (1440, 3200), (2000, 1000),
     [(1000, 500), (775, 0), (1224, 999), (100, 500)]),
    ((1170, 2532), (1168, 2528), (412, 915), [(206, 457), (0, 0), (411, 914)]),
    ((1600, 2560), (640, 1024), (900, 700), [(450, 350), (231, 0), (668, 699)]),
]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not _FRONTEND.is_file(),
    reason="node or frontend/geometry.js unavailable",
)


def _run_js() -> list[dict]:
    payload = json.dumps(
        [
            {"device": list(device), "frame": list(frame), "element": list(element),
             "pts": [list(point) for point in points]}
            for device, frame, element, points in CASES
        ]
    )
    script = f"""
      import * as geo from {json.dumps(str(_FRONTEND))};
      const cases = {payload};
      const out = [];
      for (const c of cases) {{
        const element = {{width: c.element[0], height: c.element[1]}};
        const frame = {{width: c.frame[0], height: c.frame[1]}};
        const display = {{width: c.device[0], height: c.device[1]}};
        for (const [x, y] of c.pts) {{
          out.push({{c, pt: [x, y], r: geo.describe({{x, y}}, element, frame, display)}});
        }}
      }}
      console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode:
        pytest.skip(f"node could not run geometry.js: {result.stderr.strip()[:200]}")
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def js_results() -> list[dict]:
    return _run_js()


def test_every_value_agrees(js_results: list[dict]) -> None:
    compared = 0
    for row in js_results:
        case, (x, y), js = row["c"], row["pt"], row["r"]
        mapper = CoordinateMapper(
            device=Size(*case["device"]),
            frame=Size(*case["frame"]),
            element=Size(*case["element"]),
        )
        try:
            frame_point = mapper.convert(Point(x, y), Space.CSS, Space.FRAME, strict=True)
        except OutsideFrameError:
            assert js is None, (
                f"Python says ({x},{y}) is in the letterbox, JS disagrees: {js}"
            )
            compared += 1
            continue
        assert js is not None, f"JS says ({x},{y}) is in the letterbox, Python disagrees"

        device = mapper.convert(Point(x, y), Space.CSS, Space.DEVICE, strict=False)
        norm = mapper.convert(Point(x, y), Space.CSS, Space.NORM, strict=False)
        where = f"element={case['element']} frame={case['frame']} @({x},{y})"
        assert (round(frame_point.x), round(frame_point.y)) == tuple(js["frame"]), where
        assert (round(device.x), round(device.y)) == tuple(js["device"]), where
        assert (round(norm.x, 4), round(norm.y, 4)) == tuple(
            round(value, 4) for value in js["norm"]
        ), where
        compared += 1
    assert compared == sum(len(points) for *_, points in CASES)


def test_letterbox_offset_is_not_rounded() -> None:
    """Regression for the defect this parity check found.

    Element 900x700 with a 640x1024 frame puts the content edge at x=231.25, so
    CSS x=231 is in the bar. Rounding the offset to 231 made Python accept it.
    """
    mapper = CoordinateMapper(
        device=Size(1600, 2560), frame=Size(640, 1024), element=Size(900, 700)
    )
    with pytest.raises(OutsideFrameError):
        mapper.css_to_frame(Point(231, 0))
    assert mapper.css_to_frame(Point(232, 0)).x >= 0


def test_selection_rect_round_trips_through_device_space() -> None:
    """The editable crop panel stores DEVICE pixels and draws them in CSS."""
    mapper = CoordinateMapper(
        device=Size(2400, 1080),
        frame=Size(2400, 1080),
        element=Size(1277, 575),
    )
    css_rect = Rect(301, 117, 243, 139)
    expected = mapper.convert_rect(css_rect, Space.CSS, Space.DEVICE)
    script = f"""
      import * as geo from {json.dumps(str(_FRONTEND))};
      const element = {{width: 1277, height: 575}};
      const frame = {{width: 2400, height: 1080}};
      const display = {{width: 2400, height: 1080}};
      const device = geo.cssRectToDevice(
        {{x: 301, y: 117, width: 243, height: 139}}, element, frame, display
      );
      const css = geo.deviceRectToCss(device, element, frame, display);
      console.log(JSON.stringify({{device, css}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120, check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["device"] == {
        "x": expected.x,
        "y": expected.y,
        "width": expected.width,
        "height": expected.height,
    }
    # Outward pixel rounding can grow the box by less than one displayed pixel.
    assert abs(payload["css"]["x"] - css_rect.x) < 1
    assert abs(payload["css"]["y"] - css_rect.y) < 1
    assert abs(payload["css"]["width"] - css_rect.width) < 2
    assert abs(payload["css"]["height"] - css_rect.height) < 2
