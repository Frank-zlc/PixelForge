"""UiAutomator2 client: the accessibility tree, without ``uiautomator dump``.

``adb shell uiautomator dump`` is the obvious approach and the wrong one. It
writes an XML file to the device and pulls it back, costing 1-3 seconds, and --
worse -- it waits for the UI to go idle, so on any screen with a running
animation, a spinner or a video it simply fails with "could not get idle state".
Those are exactly the screens automation gets stuck on.

The alternative is a resident server: the appium-uiautomator2-server APK, started
once via ``am instrument``, listening on a device port that is forwarded here. A
hierarchy query then costs 100-300ms and does not require idleness.

The tree is flattened into :class:`UiNode` with absolute device-pixel bounds, so a
found node hands straight to the tap path with no extra conversion.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from pixelforge.adb.client import AdbClient, AdbError
from pixelforge.geometry.mapper import Point, Rect

logger = logging.getLogger(__name__)

__all__ = ["UiAutomatorError", "UiAutomatorPort", "UiNode", "parse_hierarchy"]

SERVER_PACKAGE = "io.appium.uiautomator2.server"
SERVER_TEST_PACKAGE = f"{SERVER_PACKAGE}.test"
DEVICE_PORT = 6790

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_TRUE = {"true", "1"}


class UiAutomatorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UiNode:
    """One accessibility node, with bounds already in device pixels."""

    class_name: str
    resource_id: str | None
    text: str | None
    content_desc: str | None
    package: str | None
    bounds: Rect
    clickable: bool
    enabled: bool
    focused: bool
    scrollable: bool
    depth: int
    index: int

    @property
    def center(self) -> Point:
        return self.bounds.center

    @property
    def area(self) -> int:
        return self.bounds.width * self.bounds.height

    @property
    def label(self) -> str:
        """Short human description for the inspector."""
        for candidate in (self.text, self.content_desc, self.resource_id):
            if candidate:
                return candidate
        return self.class_name.rsplit(".", 1)[-1]

    def describe(self) -> str:
        parts = [self.class_name.rsplit(".", 1)[-1]]
        if self.resource_id:
            parts.append(f"id={self.resource_id}")
        if self.text:
            parts.append(f"text={self.text!r}")
        if self.content_desc:
            parts.append(f"desc={self.content_desc!r}")
        return " ".join(parts)


def _parse_bounds(raw: str | None) -> Rect | None:
    if not raw:
        return None
    match = _BOUNDS_RE.match(raw.strip())
    if match is None:
        return None
    left, top, right, bottom = (int(value) for value in match.groups())
    if right <= left or bottom <= top:
        return None  # a zero-area node is not clickable and not useful
    return Rect(left, top, right - left, bottom - top)


def parse_hierarchy(xml: str) -> list[UiNode]:
    """Flatten a UiAutomator XML dump into nodes, in document order.

    Document order matters: it is the order a human reads the screen, so an
    ``index`` disambiguator ("the second Confirm button") stays meaningful.
    Nodes without parsable bounds are dropped -- they cannot be tapped or
    highlighted, so keeping them would only add noise to the inspector.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise UiAutomatorError(f"could not parse the UI hierarchy: {exc}") from exc

    nodes: list[UiNode] = []

    def walk(element: ET.Element, depth: int) -> None:
        for index, child in enumerate(element):
            bounds = _parse_bounds(child.get("bounds"))
            if bounds is not None:
                nodes.append(
                    UiNode(
                        class_name=child.get("class") or child.tag,
                        resource_id=child.get("resource-id") or None,
                        text=child.get("text") or None,
                        content_desc=child.get("content-desc") or None,
                        package=child.get("package") or None,
                        bounds=bounds,
                        clickable=(child.get("clickable") or "").lower() in _TRUE,
                        enabled=(child.get("enabled") or "true").lower() in _TRUE,
                        focused=(child.get("focused") or "").lower() in _TRUE,
                        scrollable=(child.get("scrollable") or "").lower() in _TRUE,
                        depth=depth,
                        index=index,
                    )
                )
            walk(child, depth + 1)

    walk(root, 0)
    return nodes


def node_at(nodes: list[UiNode], point: Point) -> UiNode | None:
    """Smallest node containing a point.

    Smallest, not first: accessibility trees nest, so a tap lands inside the
    window, the layout, *and* the button. The smallest match is the button, which
    is what the user meant when they clicked the screenshot.
    """
    best: UiNode | None = None
    for node in nodes:
        box = node.bounds
        if box.x <= point.x < box.right and box.y <= point.y < box.bottom:
            if best is None or node.area < best.area:
                best = node
    return best


class UiAutomatorPort:
    """Manages the resident UiAutomator2 server for one device."""

    def __init__(self, adb: AdbClient, serial: str, *, timeout_s: float = 15.0) -> None:
        self._adb = adb
        self._serial = serial
        self._timeout = timeout_s
        self._local_port: str | None = None
        self._instrument: asyncio.subprocess.Process | None = None
        self.available = False
        self.last_error: str | None = None

    async def start(self) -> bool:
        """Start the server, reporting failure rather than raising.

        Failure is not fatal: the accessibility strategy simply becomes
        unavailable and the chain falls through to template and OCR. A game with
        no accessibility tree would have skipped it anyway.
        """
        try:
            installed = await self._adb.shell(
                self._serial, ["pm", "list", "packages", SERVER_PACKAGE]
            )
            if SERVER_PACKAGE not in installed:
                self.last_error = (
                    "uiautomator2 server APK is not installed; accessibility "
                    "selectors are unavailable (template and OCR still work). "
                    "See vendor/README.md."
                )
                logger.info("%s on %s", self.last_error, self._serial)
                return False
            await self._launch_instrumentation()
            self._local_port = await self._adb.forward(
                self._serial, "tcp:0", f"tcp:{DEVICE_PORT}"
            )
            self.available = await self._ping()
            return self.available
        except (AdbError, OSError) as exc:
            self.last_error = str(exc)
            logger.warning("uiautomator2 unavailable on %s: %s", self._serial, exc)
            return False

    async def _launch_instrumentation(self) -> None:
        argv = self._adb.build_argv(
            [
                "shell",
                "am",
                "instrument",
                "-w",
                f"{SERVER_TEST_PACKAGE}/androidx.test.runner.AndroidJUnitRunner",
            ],
            serial=self._serial,
        )
        # Long-running by design: the instrumentation *is* the server, so this
        # process stays alive for the session.
        self._instrument = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._adb.env,
        )
        await asyncio.sleep(1.5)  # give the JVM time to bind its port

    async def _ping(self) -> bool:
        payload = await self._get("/status")
        return payload is not None

    async def _get(self, path: str) -> dict | None:
        if self._local_port is None:
            return None
        import httpx

        url = f"http://127.0.0.1:{self._local_port}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except Exception as exc:  # noqa: BLE001 - any transport failure is "unavailable"
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    async def hierarchy(self) -> list[UiNode]:
        """Current accessibility tree.

        Falls back to ``uiautomator dump`` when the resident server is
        unavailable, accepting its cost and its idle requirement rather than
        offering nothing.
        """
        payload = await self._get("/source")
        if payload is not None:
            value = payload.get("value")
            if isinstance(value, str):
                return parse_hierarchy(value)
        return await self._dump_fallback()

    async def _dump_fallback(self) -> list[UiNode]:
        try:
            out = await self._adb.shell(
                self._serial,
                ["uiautomator", "dump", "--compressed", "/sdcard/pf-dump.xml"],
                timeout=20,
            )
            if "ERROR" in out or "could not get idle state" in out:
                raise UiAutomatorError(
                    "uiautomator dump could not reach an idle state -- the screen "
                    "is animating. Install the uiautomator2 server to avoid this."
                )
            xml = await self._adb.shell(
                self._serial, ["cat", "/sdcard/pf-dump.xml"], timeout=20
            )
            return parse_hierarchy(xml)
        except AdbError as exc:
            raise UiAutomatorError(f"could not read the UI hierarchy: {exc}") from exc

    async def stop(self) -> None:
        if self._instrument is not None and self._instrument.returncode is None:
            self._instrument.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._instrument.wait(), timeout=5)
        self._instrument = None
        if self._local_port is not None:
            with contextlib.suppress(AdbError):
                await self._adb.forward_remove(self._serial, f"tcp:{self._local_port}")
            self._local_port = None
        self.available = False
