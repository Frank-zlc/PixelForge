"""Lossless screen capture. Deliberately separate from the video stream.

The video stream and this module answer different questions and cannot be the
same channel:

    stream     "what is on screen right now"     lossy H.264, 50-120ms
    capture    "exactly which pixels are here"   lossless, 200-800ms

A template cropped out of an H.264 frame carries chroma subsampling and
quantisation artefacts. Matched later against a lossless ``screencap`` it scores
*almost* right -- high enough to pass sometimes and fail other times, with no
pattern. That intermittency is far more expensive to debug than an outright
failure, which is why cropping always re-captures rather than reusing the
displayed frame.

Two transports, because neither wins everywhere:

    PNG   ``screencap -p``  the device spends CPU compressing; less to transfer
    RAW   ``screencap``     no device CPU; ~10MB for a 1080x2400 screen

On USB 2 the two land within a few hundred milliseconds of each other, so the
default is PNG (smaller, self-describing) with RAW available for devices whose
PNG encoder is slow.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pixelforge.adb.client import AdbClient, AdbError
from pixelforge.geometry.mapper import Rect, Size

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

__all__ = ["Capture", "CaptureError", "CaptureMode", "ScreencapPort", "decode_raw"]

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# screencap's raw header is 3 or 4 little-endian uint32 depending on Android
# version (newer builds append a colour space). Both are probed by checking
# which one makes the payload length work out.
_RAW_HEADER_SIZES = (12, 16)


class CaptureError(RuntimeError):
    pass


class CaptureMode(StrEnum):
    PNG = "png"
    RAW = "raw"


@dataclass(frozen=True, slots=True)
class Capture:
    """One lossless screenshot, with the provenance a template needs.

    ``size`` comes from the image itself rather than from ``wm size``: on a
    rotated device they differ, and the pixels are the authority for anything
    that will later be matched against them.
    """

    data: bytes
    size: Size
    mode: CaptureMode
    captured_at: float
    serial: str

    @property
    def is_png(self) -> bool:
        return self.mode is CaptureMode.PNG

    def to_array(self) -> "np.ndarray":
        """Decode to an RGB ``numpy`` array.

        Alpha is dropped: ``screencap`` reports a meaningless alpha channel, and
        carrying it into template matching only adds a constant plane that
        dilutes the correlation score.
        """
        import numpy as np

        if self.mode is CaptureMode.RAW:
            width, height, pixels = decode_raw(self.data)
            return pixels[:, :, :3]
        import cv2

        buffer = np.frombuffer(self.data, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if image is None:
            raise CaptureError("could not decode the captured PNG")
        return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    def crop(self, rect: Rect) -> "np.ndarray":
        """Crop in device pixels, clamped to the image."""
        box = rect.clamped_to(self.size)
        return self.to_array()[box.y : box.bottom, box.x : box.right]

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.is_png:
            path.write_bytes(self.data)
            return path
        import cv2

        array = self.to_array()
        if not cv2.imwrite(str(path), cv2.cvtColor(array, cv2.COLOR_RGB2BGR)):
            raise CaptureError(f"could not write {path}")
        return path


def decode_raw(payload: bytes) -> tuple[int, int, "np.ndarray"]:
    """Decode ``screencap``'s raw framebuffer format.

    The header is 3 or 4 little-endian uint32 depending on Android version. Both
    are tried and the one whose dimensions account for the payload wins --
    reading the version and branching on it would be one more thing to keep
    correct as Android changes.
    """
    import numpy as np

    for header_size in _RAW_HEADER_SIZES:
        if len(payload) < header_size:
            continue
        width, height, pixel_format = struct.unpack_from("<III", payload, 0)
        if width <= 0 or height <= 0 or width > 20000 or height > 20000:
            continue
        expected = width * height * 4
        if len(payload) - header_size == expected:
            pixels = np.frombuffer(payload, dtype=np.uint8, offset=header_size)
            return width, height, pixels.reshape((height, width, 4))
    raise CaptureError(
        f"raw screencap payload of {len(payload)} bytes matches no known header "
        "layout; the device may have returned an error on stdout"
    )


class ScreencapPort:
    """Takes lossless screenshots of one device."""

    def __init__(self, adb: AdbClient, *, mode: CaptureMode = CaptureMode.PNG) -> None:
        self._adb = adb
        self._mode = mode

    async def capture(
        self,
        serial: str,
        *,
        mode: CaptureMode | None = None,
        timeout: float = 30.0,
    ) -> Capture:
        mode = mode or self._mode
        started = time.monotonic()
        try:
            if mode is CaptureMode.PNG:
                payload = await self._adb.screencap_png(serial, timeout=timeout)
                size = Size(*_png_size(payload))
            else:
                payload = await self._adb.run(
                    ["exec-out", "screencap"], serial=serial, timeout=timeout
                )
                width, height, _ = decode_raw(payload)
                size = Size(width, height)
        except AdbError as exc:
            raise CaptureError(f"screencap failed on {serial}: {exc}") from exc
        return Capture(
            data=payload,
            size=size,
            mode=mode,
            captured_at=started,
            serial=serial,
        )

    async def benchmark(self, serial: str) -> dict[str, float]:
        """Time both transports so the faster one can be chosen per device.

        Which wins depends on the phone's PNG encoder and the USB link, so it is
        measured rather than assumed.
        """
        results: dict[str, float] = {}
        for mode in CaptureMode:
            start = time.monotonic()
            capture = await self.capture(serial, mode=mode)
            results[f"{mode.value}_seconds"] = round(time.monotonic() - start, 3)
            results[f"{mode.value}_bytes"] = len(capture.data)
        return results


def _png_size(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != _PNG_MAGIC or payload[12:16] != b"IHDR":
        raise CaptureError("not a valid PNG screenshot")
    width, height = struct.unpack(">II", payload[16:24])
    if not width or not height:
        raise CaptureError("PNG reports a zero dimension")
    return width, height
