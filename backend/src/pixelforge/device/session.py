"""One device, everything attached to it.

Composes the four device-facing pieces and implements
:class:`~pixelforge.script.backend.DeviceBackend`, so the step engine sees a
single object:

    ScrcpySession    live H.264 frames and the control channel
    ScreencapPort    lossless stills for cropping and evidence
    UiAutomatorPort  the accessibility tree
    CoordinateMapper the only place any of their coordinate spaces are converted

Two decisions worth naming.

**Taps are sent in frame coordinates.** scrcpy's touch message carries the sender's
screen size next to the point and rescales on the device, so sending frame
coordinates with the frame size skips a conversion to physical pixels entirely --
and every conversion skipped is a place an off-by-three cannot happen.

**Screenshots are cached for a few hundred milliseconds.** A single step's locate
chain can ask for the image more than once (template, then OCR), and each capture
is 200-800ms. The cache is deliberately short: long enough to serve one step,
short enough that nothing ever matches against a stale screen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pixelforge.adb.client import AdbClient, AdbError, DeviceProps
from pixelforge.device.capture import Capture, CaptureMode, ScreencapPort
from pixelforge.device.scrcpy.control import (
    Action,
    MotionAction,
    encode_key,
    encode_text,
    encode_touch,
)
from pixelforge.device.scrcpy.session import ScrcpyConfig, ScrcpySession, ScrcpyStartupError
from pixelforge.device.secure_probe import SecureProbeResult, diagnose_black_screen
from pixelforge.device.uiautomator import UiAutomatorPort, UiNode, node_at
from pixelforge.geometry.mapper import CoordinateMapper, Point, Rect, Rotation, Size, Space

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

logger = logging.getLogger(__name__)

__all__ = ["DeviceSession"]

# Long enough for one step's locate chain, short enough that nothing matches a
# stale screen.
_SCREENSHOT_TTL_S = 0.4
_SWIPE_STEPS = 12


@dataclass
class SessionStatus:
    serial: str
    video: bool = False
    control: bool = False
    a11y: bool = False
    display: tuple[int, int] | None = None
    frame: tuple[int, int] | None = None
    rotation: int = 0
    secure_screen: bool = False
    notes: list[str] = field(default_factory=list)


class DeviceSession:
    """The step engine's view of one device."""

    def __init__(
        self,
        adb: AdbClient,
        serial: str,
        props: DeviceProps,
        *,
        scrcpy_config: ScrcpyConfig | None = None,
        capture_mode: CaptureMode = CaptureMode.PNG,
        templates_dir: Path | None = None,
    ) -> None:
        self._adb = adb
        self.serial = serial
        self.props = props
        self.templates_dir = templates_dir

        natural = Size(*(props.size or (1080, 1920)))
        self._mapper = CoordinateMapper(device=natural, frame=natural)
        self._rotations: asyncio.Queue[Rotation] = asyncio.Queue(maxsize=8)
        self.scrcpy = ScrcpySession(
            adb, serial, config=scrcpy_config, on_rotation=self._rotations
        )
        self.screencap = ScreencapPort(adb, mode=capture_mode)
        self.uiautomator = UiAutomatorPort(adb, serial)

        self._cached: tuple[float, Capture] | None = None
        self._capture_lock = asyncio.Lock()
        self._rotation_task: asyncio.Task[None] | None = None
        self.notes: list[str] = []
        self.last_secure_probe: SecureProbeResult | None = None

    # ------------------------------------------------------- DeviceBackend

    @property
    def display(self) -> Size:
        return self._mapper.display

    @property
    def a11y_available(self) -> bool:
        return self.uiautomator.available

    @property
    def mapper(self) -> CoordinateMapper:
        return self._mapper

    async def screenshot(self) -> "np.ndarray":
        return (await self.capture()).to_array()

    async def hierarchy(self) -> list[UiNode]:
        return await self.uiautomator.hierarchy()

    async def tap(self, box: Rect) -> None:
        point = self._to_frame(box.center)
        frame = self._mapper.frame
        await self.scrcpy.send(
            encode_touch(MotionAction.DOWN, point.x, point.y, frame.width, frame.height),
            encode_touch(MotionAction.UP, point.x, point.y, frame.width, frame.height),
        )

    async def long_press(self, box: Rect, duration_ms: int) -> None:
        point = self._to_frame(box.center)
        frame = self._mapper.frame
        await self.scrcpy.send(
            encode_touch(MotionAction.DOWN, point.x, point.y, frame.width, frame.height)
        )
        await asyncio.sleep(duration_ms / 1000)
        await self.scrcpy.send(
            encode_touch(MotionAction.UP, point.x, point.y, frame.width, frame.height)
        )

    async def swipe(self, start: Rect, end: Rect, duration_ms: int) -> None:
        """Interpolated down-move-up.

        Sent as real intermediate MOVE events rather than one jump, because fling
        detection and most drag handlers need the movement to look continuous --
        a single large MOVE is frequently treated as a tap or ignored.
        """
        frame = self._mapper.frame
        origin = self._to_frame(start.center)
        target = self._to_frame(end.center)
        await self.scrcpy.send(
            encode_touch(MotionAction.DOWN, origin.x, origin.y, frame.width, frame.height)
        )
        interval = max(0.004, duration_ms / 1000 / _SWIPE_STEPS)
        for index in range(1, _SWIPE_STEPS + 1):
            ratio = index / _SWIPE_STEPS
            await asyncio.sleep(interval)
            await self.scrcpy.send(
                encode_touch(
                    MotionAction.MOVE,
                    origin.x + (target.x - origin.x) * ratio,
                    origin.y + (target.y - origin.y) * ratio,
                    frame.width,
                    frame.height,
                )
            )
        await self.scrcpy.send(
            encode_touch(MotionAction.UP, target.x, target.y, frame.width, frame.height)
        )

    async def input_text(self, text: str) -> None:
        messages = encode_text(text)
        if messages:
            await self.scrcpy.send(*messages)

    async def key(self, keycode: int) -> None:
        await self.scrcpy.send(
            encode_key(Action.DOWN, keycode), encode_key(Action.UP, keycode)
        )

    async def launch_app(self, package: str) -> None:
        await self._adb.shell(
            self.serial,
            ["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=20,
        )

    async def stop_app(self, package: str) -> None:
        await self._adb.shell(self.serial, ["am", "force-stop", package], timeout=20)

    # -------------------------------------------------------------- capture

    async def capture(self, *, fresh: bool = False) -> Capture:
        """Lossless screenshot, briefly cached.

        ``fresh=True`` for anything that becomes a stored artefact -- cropping a
        template, or evidence attached to a step -- so the pixels are never a few
        hundred milliseconds behind what the user was looking at.
        """
        async with self._capture_lock:
            now = time.monotonic()
            if not fresh and self._cached is not None:
                stamped, cached = self._cached
                if now - stamped < _SCREENSHOT_TTL_S:
                    return cached
            capture = await self.screencap.capture(self.serial)
            self._cached = (now, capture)
            # The image is the authority on the current display size; wm size
            # reports the natural orientation and does not follow rotation.
            if capture.size != self._mapper.display:
                self._sync_display(capture.size)
            return capture

    async def crop(self, rect: Rect, space: Space = Space.DEVICE) -> "np.ndarray":
        """Crop for template creation -- always from a fresh lossless capture."""
        capture = await self.capture(fresh=True)
        device_rect = (
            rect
            if space is Space.DEVICE
            else self._mapper.convert_rect(rect, space, Space.DEVICE)
        )
        return capture.crop(device_rect)

    async def probe_secure(self) -> SecureProbeResult:
        """Work out whether a black capture means FLAG_SECURE."""
        capture = await self.capture(fresh=True)
        nodes = []
        with contextlib.suppress(Exception):
            nodes = await self.hierarchy()
        screen_on: bool | None = None
        with contextlib.suppress(AdbError):
            power = await self._adb.shell(self.serial, ["dumpsys", "power"], timeout=8)
            if "mWakefulness=" in power:
                screen_on = "mWakefulness=Awake" in power
        result = diagnose_black_screen(
            capture.to_array(), node_count=len(nodes), screen_on=screen_on
        )
        self.last_secure_probe = result
        return result

    async def node_at_point(self, point: Point, space: Space = Space.DEVICE) -> UiNode | None:
        """Which control is under a point -- the inspector's click-to-identify."""
        device_point = (
            point if space is Space.DEVICE else self._mapper.convert(point, space, Space.DEVICE)
        )
        return node_at(await self.hierarchy(), device_point)

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> SessionStatus:
        """Bring up what can be brought up; report the rest instead of failing.

        Partial capability is genuinely useful: a game with no accessibility tree
        still works through template matching, and a device whose scrcpy refuses
        to start can still be inspected and screenshotted. Refusing to open the
        session at all would throw that away.
        """
        status = SessionStatus(serial=self.serial)
        try:
            await self.scrcpy.start()
            status.video = status.control = True
            if self.scrcpy.state.frame is not None:
                self._mapper = self._mapper.with_(frame=self.scrcpy.state.frame)
                status.frame = self.scrcpy.state.frame.as_tuple()
            self._rotation_task = asyncio.create_task(
                self._follow_rotation(), name=f"pixelforge-rot-follow-{self.serial}"
            )
        except ScrcpyStartupError as exc:
            self.notes.append(f"video/control unavailable: {exc}")
            logger.warning("scrcpy unavailable on %s: %s", self.serial, exc)

        status.a11y = await self.uiautomator.start()
        if not status.a11y and self.uiautomator.last_error:
            self.notes.append(self.uiautomator.last_error)

        with contextlib.suppress(Exception):
            capture = await self.capture(fresh=True)
            status.display = capture.size.as_tuple()

        with contextlib.suppress(Exception):
            probe = await self.probe_secure()
            if not probe.capture_usable:
                status.secure_screen = True
                self.notes.append(probe.message())

        status.rotation = int(self._mapper.rotation)
        status.notes = list(self.notes)
        return status

    async def _follow_rotation(self) -> None:
        try:
            while True:
                rotation = await self._rotations.get()
                frame = self.scrcpy.state.frame or self._mapper.frame
                self._mapper = self._mapper.with_(rotation=rotation, frame=frame)
                self._cached = None  # a rotated screen invalidates the cache
                logger.info("%s rotated to %d degrees", self.serial, rotation.degrees)
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        if self._rotation_task is not None:
            self._rotation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._rotation_task
            self._rotation_task = None
        await self.scrcpy.stop()
        await self.uiautomator.stop()
        self._cached = None

    def status(self) -> SessionStatus:
        state = self.scrcpy.state
        return SessionStatus(
            serial=self.serial,
            video=state.started,
            control=state.started,
            a11y=self.uiautomator.available,
            display=self._mapper.display.as_tuple(),
            frame=self._mapper.frame.as_tuple(),
            rotation=int(self._mapper.rotation),
            secure_screen=bool(
                self.last_secure_probe and not self.last_secure_probe.capture_usable
            ),
            notes=list(self.notes),
        )

    # --------------------------------------------------------------- private

    def _to_frame(self, point: Point) -> Point:
        return self._mapper.convert(point, Space.DEVICE, Space.FRAME, strict=False)

    def _sync_display(self, observed: Size) -> None:
        """Reconcile the mapper with what the pixels actually say.

        If the capture is landscape while ``wm size`` is portrait, the device is
        rotated; adopt that rather than trusting the stale rotation value.
        """
        natural = self._mapper.device
        if observed.as_tuple() == natural.swapped().as_tuple():
            if not self._mapper.rotation.swaps_axes:
                self._mapper = self._mapper.with_(rotation=Rotation.R90)
        elif observed.as_tuple() != natural.as_tuple():
            # An override or a resolution change; the image is authoritative.
            self._mapper = self._mapper.with_(device=observed, rotation=Rotation.R0)
        if self.scrcpy.state.frame is None:
            self._mapper = self._mapper.with_(frame=self._mapper.display)
