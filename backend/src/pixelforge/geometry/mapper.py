"""The one place coordinates are converted. Nowhere else may multiply or divide one.

Four spaces are in play at once, and mixing any two of them silently produces a
tap that lands a few pixels off -- the kind of bug that survives manual clicking
because a finger-sized target absorbs it, then breaks template matching later.

    CSS      Browser element pixels. The frame is letterboxed inside the element,
             so there are usually dead bars that map to nothing.
    FRAME    scrcpy's encoded frame. Scaled by max_size and, crucially, *aligned
             down to a multiple of 8* by the H.264 encoder, so it is never
             exactly `device * scale`.
    DEVICE   What the display actually shows right now, in physical pixels.
             Equals `screencap` output. At 90/270 this is the natural size
             swapped, because `wm size` reports the natural orientation and does
             not change when the device rotates.
    NORM     [0,1] against DEVICE. What scripts persist.

Two rules make this robust:

* **Per-axis scale, derived from measured sizes.** Because the encoder aligns
  width and height independently, the x and y scale factors genuinely differ.
  Assuming one uniform scale is the classic off-by-three-pixels bug.
* **Letterbox is rejected, not clamped.** A click on a black bar is not a click
  near the edge; silently clamping it fabricates an interaction the user did not
  make.

Note on taps: scrcpy's control protocol carries `(x, y, width, height)` and does
its own scaling on the device, so injecting a touch only needs CSS -> FRAME.
Cropping a screenshot needs CSS -> DEVICE, because `screencap` is at DEVICE
resolution. Both go through this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum, StrEnum

__all__ = [
    "CoordinateMapper",
    "OutsideFrameError",
    "Point",
    "Rect",
    "Rotation",
    "Size",
    "Space",
]


class Rotation(IntEnum):
    """Display rotation as Android reports it."""

    R0 = 0
    R90 = 1
    R180 = 2
    R270 = 3

    @property
    def degrees(self) -> int:
        return int(self) * 90

    @property
    def swaps_axes(self) -> bool:
        return self in (Rotation.R90, Rotation.R270)

    @classmethod
    def parse(cls, value: int) -> Rotation:
        try:
            return cls(value % 4)
        except ValueError as exc:  # pragma: no cover - unreachable after %4
            raise ValueError(f"invalid rotation: {value}") from exc


class Space(StrEnum):
    CSS = "css"
    FRAME = "frame"
    DEVICE = "device"
    NORM = "norm"


class OutsideFrameError(ValueError):
    """A CSS point fell in the letterbox, outside the rendered frame."""


@dataclass(frozen=True, slots=True)
class Size:
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"size must be positive, got {self.width}x{self.height}")

    @property
    def aspect(self) -> float:
        return self.width / self.height

    def swapped(self) -> Size:
        return Size(self.height, self.width)

    def as_tuple(self) -> tuple[int, int]:
        return (self.width, self.height)


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)

    def rounded(self) -> tuple[int, int]:
        return (round(self.x), round(self.y))


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"rect must be positive, got {self.width}x{self.height}")

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center(self) -> Point:
        return Point(self.x + self.width / 2, self.y + self.height / 2)

    def clamped_to(self, size: Size) -> Rect:
        """Clip to a bounding size, keeping at least one pixel."""
        left = max(0, min(self.x, size.width - 1))
        top = max(0, min(self.y, size.height - 1))
        right = max(left + 1, min(self.right, size.width))
        bottom = max(top + 1, min(self.bottom, size.height))
        return Rect(left, top, right - left, bottom - top)

    def as_tuple(self) -> tuple[int, int, int, int]:
        """(left, top, right, bottom) -- the box form Pillow and OpenCV want."""
        return (self.x, self.y, self.right, self.bottom)


@dataclass(frozen=True, slots=True)
class CoordinateMapper:
    """Converts a point or rect between any two coordinate spaces.

    Immutable: a rotation or a resize produces a new mapper via :meth:`with_`.
    Every field comes from something the device or browser *reported* -- none is
    inferred -- because a guessed frame size is exactly how per-axis scale errors
    creep back in.

    Parameters
    ----------
    device:
        Natural display size from ``wm size`` (does not change with rotation).
    frame:
        Actual encoded frame size reported in the scrcpy stream header.
    rotation:
        Current display rotation, from scrcpy's rotation events.
    element:
        CSS size of the canvas element. Only needed for CSS conversions.
    """

    device: Size
    frame: Size
    rotation: Rotation = Rotation.R0
    element: Size | None = None

    # ------------------------------------------------------------- derived

    @property
    def display(self) -> Size:
        """DEVICE space: what is on screen now, matching ``screencap`` output."""
        return self.device.swapped() if self.rotation.swaps_axes else self.device

    @property
    def scale_x(self) -> float:
        """FRAME -> DEVICE scale on x. Differs from y: the encoder aligns axes
        independently, so this is measured, never assumed."""
        return self.display.width / self.frame.width

    @property
    def scale_y(self) -> float:
        return self.display.height / self.frame.height

    @property
    def is_axis_aligned(self) -> bool:
        """True when x and y scales agree to within a pixel of the frame.

        False is normal (encoder alignment) and not an error -- it is surfaced so
        the UI can show that a few pixels of the frame do not correspond to any
        device pixel.
        """
        return math.isclose(self.scale_x, self.scale_y, rel_tol=1e-3)

    def _content_box(self) -> tuple[float, float, float, float, float]:
        """Exact (x, y, width, height, scale) of the drawn frame, in CSS px.

        Floats, not integers, and that matters more than it looks: rounding the
        letterbox offset costs up to half a CSS pixel, which at a 2.35x
        frame-to-device ratio becomes more than a whole device pixel -- and near
        the edge it flips the in-frame test itself, so a click on a black bar can
        read as a click on the first column. All conversion uses this; the
        rounded :attr:`content_rect_css` exists only for drawing.
        """
        element = self._require_element()
        scale = min(
            element.width / self.frame.width, element.height / self.frame.height
        )
        width = self.frame.width * scale
        height = self.frame.height * scale
        return (
            (element.width - width) / 2,
            (element.height - height) / 2,
            width,
            height,
            scale,
        )

    @property
    def content_rect_css(self) -> Rect:
        """Rounded content box, for drawing an overlay. Never for conversion."""
        x, y, width, height, _ = self._content_box()
        return Rect(
            x=round(x), y=round(y), width=max(1, round(width)), height=max(1, round(height))
        )

    @property
    def css_scale(self) -> float:
        """CSS px per frame px under 'contain' fitting."""
        element = self._require_element()
        return min(element.width / self.frame.width, element.height / self.frame.height)

    # ------------------------------------------------------------ mutation

    def with_(
        self,
        *,
        device: Size | None = None,
        frame: Size | None = None,
        rotation: Rotation | None = None,
        element: Size | None = None,
    ) -> CoordinateMapper:
        """Return a new mapper with some fields replaced."""
        return CoordinateMapper(
            device=device or self.device,
            frame=frame or self.frame,
            rotation=self.rotation if rotation is None else rotation,
            element=element or self.element,
        )

    # ----------------------------------------------------- pairwise (point)

    def css_to_frame(self, point: Point, *, strict: bool = True) -> Point:
        """Map a browser click onto the encoded frame.

        Raises :class:`OutsideFrameError` for a click in the letterbox when
        ``strict``; clamps to the edge otherwise. Strict is the default because
        a click on a black bar is not a click near the edge, and clamping would
        fabricate an interaction the user did not make.
        """
        offset_x, offset_y, _, _, scale = self._content_box()
        x = (point.x - offset_x) / scale
        y = (point.y - offset_y) / scale
        if not (0 <= x <= self.frame.width and 0 <= y <= self.frame.height):
            if strict:
                raise OutsideFrameError(
                    f"({point.x:.1f}, {point.y:.1f}) CSS is outside the rendered "
                    f"frame at +{offset_x:.1f},{offset_y:.1f}"
                )
            x = min(max(x, 0.0), float(self.frame.width))
            y = min(max(y, 0.0), float(self.frame.height))
        return Point(x, y)

    def frame_to_css(self, point: Point) -> Point:
        offset_x, offset_y, _, _, scale = self._content_box()
        return Point(offset_x + point.x * scale, offset_y + point.y * scale)

    def frame_to_device(self, point: Point) -> Point:
        return Point(point.x * self.scale_x, point.y * self.scale_y)

    def device_to_frame(self, point: Point) -> Point:
        return Point(point.x / self.scale_x, point.y / self.scale_y)

    def device_to_norm(self, point: Point) -> Point:
        """Normalise against DEVICE, i.e. the *current* orientation.

        Rotation is stored alongside the coordinate rather than folded into it:
        rotating a device reflows the UI, so a normalised point recorded in
        landscape does not identify the same control in portrait. Transforming it
        geometrically would be arithmetically valid and semantically wrong.
        """
        display = self.display
        return Point(point.x / display.width, point.y / display.height)

    def norm_to_device(self, point: Point) -> Point:
        display = self.display
        return Point(point.x * display.width, point.y * display.height)

    # ------------------------------------------------------- generic convert

    _CHAIN: tuple[Space, ...] = (Space.CSS, Space.FRAME, Space.DEVICE, Space.NORM)

    def convert(
        self, point: Point, source: Space, target: Space, *, strict: bool = True
    ) -> Point:
        """Convert between any two spaces by walking the chain.

        CSS -- FRAME -- DEVICE -- NORM is a straight line, so any pair is reached
        by stepping along it. Having exactly one implementation of each adjacent
        step is what keeps the round trips consistent.

        The steps are dispatched by index rather than by comparing bound methods:
        ``self.method is self.method`` is False in CPython (each attribute access
        builds a fresh bound method), which silently made ``strict`` a no-op here
        until a selection dragged past the frame edge exposed it.
        """
        source, target = Space(source), Space(target)
        if source is target:
            return point
        start, end = self._CHAIN.index(source), self._CHAIN.index(target)

        current = point
        if end > start:
            for index in range(start, end):
                if index == 0:
                    current = self.css_to_frame(current, strict=strict)
                elif index == 1:
                    current = self.frame_to_device(current)
                else:
                    current = self.device_to_norm(current)
        else:
            for index in range(start, end, -1):
                if index == 3:
                    current = self.norm_to_device(current)
                elif index == 2:
                    current = self.device_to_frame(current)
                else:
                    current = self.frame_to_css(current)
        return current

    def convert_rect(
        self, rect: Rect, source: Space, target: Space, *, strict: bool = False
    ) -> Rect:
        """Convert a rectangle, rounding outward so the result covers the input.

        Outward rounding matters for cropping: a template that loses its edge row
        matches worse than one carrying an extra pixel of background. ``strict``
        defaults to False here because a drag selection routinely starts or ends
        slightly outside the frame, and clamping a selection is reasonable in a
        way that clamping a click is not.
        """
        top_left = self.convert(Point(rect.x, rect.y), source, target, strict=strict)
        bottom_right = self.convert(
            Point(rect.right, rect.bottom), source, target, strict=strict
        )
        left, top = math.floor(top_left.x), math.floor(top_left.y)
        right, bottom = math.ceil(bottom_right.x), math.ceil(bottom_right.y)
        out = Rect(left, top, max(1, right - left), max(1, bottom - top))
        bounds = self._bounds(target)
        return out.clamped_to(bounds) if bounds is not None else out

    # ------------------------------------------------- rotation of raw pixels

    def rotate_device_point(self, point: Point, to: Rotation) -> Point:
        """Rotate a DEVICE point into the geometry of another rotation.

        This is a pure pixel transform for image work -- rotating a screenshot,
        say. It is deliberately *not* used for replaying recorded steps: the UI
        reflows on rotation, so moving a coordinate across rotations would point
        at the wrong control while looking perfectly correct.
        """
        turns = (int(to) - int(self.rotation)) % 4
        width, height = self.display.width, self.display.height
        x, y = point.x, point.y
        for _ in range(turns):
            x, y, width, height = height - y, x, height, width
        return Point(x, y)

    # ------------------------------------------------------------- reporting

    def describe(self, point: Point, source: Space) -> dict[str, tuple[float, float]]:
        """All four representations of one point, for the coordinate readout.

        Spaces that cannot be computed (CSS without an element) are omitted
        rather than faked.
        """
        out: dict[str, tuple[float, float]] = {}
        for space in self._CHAIN:
            if space is Space.CSS and self.element is None:
                continue
            try:
                converted = self.convert(point, source, space, strict=False)
            except ValueError:
                continue
            out[space.value] = (
                round(converted.x, 4) if space is Space.NORM else round(converted.x, 1),
                round(converted.y, 4) if space is Space.NORM else round(converted.y, 1),
            )
        return out

    # --------------------------------------------------------------- private

    def _require_element(self) -> Size:
        if self.element is None:
            raise ValueError(
                "CSS conversions need the element size; construct the mapper with "
                "element=Size(...) from the browser"
            )
        return self.element

    def _bounds(self, space: Space) -> Size | None:
        if space is Space.FRAME:
            return self.frame
        if space is Space.DEVICE:
            return self.display
        if space is Space.CSS:
            return self.element
        return None  # NORM is unbounded by pixels; callers clamp if they care
