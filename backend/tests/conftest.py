"""Shared fixtures: a fake device the step engine can be driven against.

The engine talks to :class:`~pixelforge.script.backend.DeviceBackend`, so the
whole of step sequencing, retry, assertion and breakpoint behaviour is exercisable
with no phone attached. Those are exactly the parts with subtle bugs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest

from pixelforge.device.uiautomator import UiNode, parse_hierarchy
from pixelforge.geometry.mapper import Rect, Size
from pixelforge.locators.base import LocatorChain
from pixelforge.locators.strategies import (
    A11yLocator,
    CoordLocator,
    OcrLocator,
    TemplateLocator,
)
from pixelforge.script.model import Strategy
from pixelforge.timeline.bus import TimelineBus

HIERARCHY = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" package="com.example" bounds="[0,0][1080,2400]">
    <node class="android.widget.Button" resource-id="com.example:id/login"
          text="Log in" clickable="true" bounds="[340,1900][740,2030]"/>
    <node class="android.widget.EditText" resource-id="com.example:id/account"
          text="" clickable="true" bounds="[120,1500][960,1600]"/>
    <node class="android.widget.TextView" resource-id="com.example:id/home"
          text="Welcome" bounds="[120,300][960,400]"/>
  </node>
</hierarchy>"""


@dataclass
class FakeDevice:
    """Scriptable stand-in for a real device."""

    size: Size = field(default_factory=lambda: Size(1080, 2400))
    a11y: bool = True
    nodes_xml: str = HIERARCHY
    taps: list[Rect] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    keys: list[int] = field(default_factory=list)
    swipes: list[tuple[Rect, Rect, int]] = field(default_factory=list)
    long_presses: list[tuple[Rect, int]] = field(default_factory=list)
    launched: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    screenshots: int = 0
    hierarchy_calls: int = 0
    # Set to have the tree vanish after N queries, simulating a screen change.
    nodes_after: dict[int, str] = field(default_factory=dict)
    frames: list[np.ndarray] | None = None
    fail_tap_with: Exception | None = None

    @property
    def display(self) -> Size:
        return self.size

    @property
    def a11y_available(self) -> bool:
        return self.a11y

    async def screenshot(self) -> np.ndarray:
        self.screenshots += 1
        if self.frames:
            # Walk the supplied frames, holding on the last one.
            index = min(self.screenshots - 1, len(self.frames) - 1)
            return self.frames[index]
        rng = np.random.default_rng(42)
        return rng.integers(0, 255, (self.size.height, self.size.width, 3), dtype=np.uint8)

    async def hierarchy(self) -> list[UiNode]:
        self.hierarchy_calls += 1
        xml = self.nodes_after.get(self.hierarchy_calls, self.nodes_xml)
        return parse_hierarchy(xml)

    async def tap(self, box: Rect) -> None:
        if self.fail_tap_with is not None:
            raise self.fail_tap_with
        self.taps.append(box)

    async def long_press(self, box: Rect, duration_ms: int) -> None:
        self.long_presses.append((box, duration_ms))

    async def swipe(self, start: Rect, end: Rect, duration_ms: int) -> None:
        self.swipes.append((start, end, duration_ms))

    async def input_text(self, text: str) -> None:
        self.texts.append(text)

    async def key(self, keycode: int) -> None:
        self.keys.append(keycode)

    async def launch_app(self, package: str) -> None:
        self.launched.append(package)

    async def stop_app(self, package: str) -> None:
        self.stopped.append(package)


@pytest.fixture
def device() -> FakeDevice:
    return FakeDevice()


@pytest.fixture
def bus() -> TimelineBus:
    return TimelineBus()


@pytest.fixture
def chain() -> LocatorChain:
    return LocatorChain(
        {
            Strategy.A11Y: A11yLocator(),
            Strategy.TEMPLATE: TemplateLocator(),
            Strategy.OCR: OcrLocator(),
            Strategy.COORD: CoordLocator(),
        }
    )
