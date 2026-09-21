"""Listener interface: anything that puts events on the timeline.

The point of this seam is not to collect data -- other tools already do that well.
It is to put whatever they collect on the *same axis* as the steps, because the
expensive question when debugging automation is "I tapped it; did anything
happen?" and that needs both halves side by side.

Capture and protocol parsing stay with whoever owns the protocol. PixelForge
aligns and displays.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pixelforge.timeline.bus import TimelineBus

__all__ = ["ListenerContext", "Listener"]


@dataclass(frozen=True, slots=True)
class ListenerContext:
    serial: str
    bus: TimelineBus
    app_package: str | None = None


@runtime_checkable
class Listener(Protocol):
    name: str
    description: str

    async def start(self, context: ListenerContext) -> None: ...

    async def stop(self) -> None: ...

    @property
    def running(self) -> bool: ...
