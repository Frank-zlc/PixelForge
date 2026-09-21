"""Diagnose FLAG_SECURE instead of leaving the user staring at a black screen.

Android 12 removed the shell UID from SurfaceFlinger's ``CAPTURE_BLACKOUT_CONTENT``
allowlist, so a window marked ``FLAG_SECURE`` captures as pure black through
``screencap``, ``minicap`` *and* scrcpy alike. It is a framework decision, not a
tool limitation, and there is no workaround short of root.

What *is* available is telling the user the truth quickly. A black screenshot
alone is ambiguous -- it could be a genuinely dark screen, a powered-off display
or a stalled encoder. Combined with a non-empty accessibility tree it is not
ambiguous at all: something is definitely being rendered, and we are definitely
not allowed to see it.

Turning a dead end into a five-second diagnosis is the difference between a
general-purpose tool and a one-off script.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

__all__ = ["BlackScreenCause", "SecureProbeResult", "diagnose_black_screen"]

# Mean luminance below this, with near-zero variance, is "black" rather than dark.
_BLACK_MEAN = 6.0
_BLACK_STD = 3.0
_BLACK_COVERAGE = 0.92


class BlackScreenCause(StrEnum):
    NOT_BLACK = "not_black"
    FLAG_SECURE = "flag_secure"
    DISPLAY_OFF = "display_off"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SecureProbeResult:
    cause: BlackScreenCause
    black_ratio: float
    node_count: int

    @property
    def capture_usable(self) -> bool:
        return self.cause is BlackScreenCause.NOT_BLACK

    def message(self) -> str:
        if self.cause is BlackScreenCause.NOT_BLACK:
            return ""
        if self.cause is BlackScreenCause.FLAG_SECURE:
            return (
                "This screen is protected by FLAG_SECURE, so no image can be "
                "captured -- scrcpy, screencap and minicap are all blocked by the "
                "same framework check. The accessibility tree still works, so "
                "a11y selectors can locate elements here; coordinate picking and "
                "template matching cannot be used on this screen."
            )
        if self.cause is BlackScreenCause.DISPLAY_OFF:
            return (
                "The display appears to be off or locked: the capture is black and "
                "no UI nodes are present. Wake the device (and unlock it) before "
                "continuing."
            )
        return (
            "The capture is black for an undetermined reason. If the device screen "
            "looks normal, the encoder may have stalled -- restarting the session "
            "usually clears it."
        )


def diagnose_black_screen(
    image: "np.ndarray", *, node_count: int, screen_on: bool | None = None
) -> SecureProbeResult:
    """Classify a possibly-black capture.

    ``screen_on`` comes from ``dumpsys power`` when the caller has it; without it
    the node count alone separates FLAG_SECURE (rendering, not visible) from a
    dark or off display (nothing rendering).
    """
    import numpy as np

    grey = image if image.ndim == 2 else image[..., :3].mean(axis=2)
    dark_ratio = float(np.count_nonzero(grey <= _BLACK_MEAN)) / float(grey.size)

    if dark_ratio < _BLACK_COVERAGE or (
        float(grey.mean()) > _BLACK_MEAN and float(grey.std()) > _BLACK_STD
    ):
        return SecureProbeResult(BlackScreenCause.NOT_BLACK, dark_ratio, node_count)

    if node_count > 0:
        # Something is definitely being composited; we are simply not permitted
        # to see it.
        return SecureProbeResult(BlackScreenCause.FLAG_SECURE, dark_ratio, node_count)
    if screen_on is False:
        return SecureProbeResult(BlackScreenCause.DISPLAY_OFF, dark_ratio, node_count)
    if screen_on is None:
        return SecureProbeResult(BlackScreenCause.DISPLAY_OFF, dark_ratio, node_count)
    return SecureProbeResult(BlackScreenCause.UNKNOWN, dark_ratio, node_count)
