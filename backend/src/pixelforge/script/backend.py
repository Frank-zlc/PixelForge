"""What the executor needs from a device, as a Protocol.

Defined as an interface rather than reaching for the real session so the whole
step engine -- including the debugger's pause and resume behaviour -- is testable
against a fake device. Step sequencing, retry and breakpoint logic are where the
subtle bugs live, and none of them need a phone to exercise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pixelforge.geometry.mapper import Rect, Size

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

    from pixelforge.device.uiautomator import UiNode

__all__ = ["DeviceBackend"]


@runtime_checkable
class DeviceBackend(Protocol):
    """One device, as the step engine sees it. Coordinates are device pixels."""

    @property
    def display(self) -> Size: ...

    @property
    def a11y_available(self) -> bool: ...

    async def screenshot(self) -> "np.ndarray": ...

    async def hierarchy(self) -> list["UiNode"]: ...

    async def tap(self, box: Rect) -> None: ...

    async def long_press(self, box: Rect, duration_ms: int) -> None: ...

    async def swipe(self, start: Rect, end: Rect, duration_ms: int) -> None: ...

    async def input_text(self, text: str) -> None: ...

    async def key(self, keycode: int) -> None: ...

    async def launch_app(self, package: str) -> None: ...

    async def stop_app(self, package: str) -> None: ...
