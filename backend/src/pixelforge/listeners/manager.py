"""Own listener instances for the lifetime of an acquired device session."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from pixelforge.adb.client import AdbClient
from pixelforge.listeners.base import Listener, ListenerContext
from pixelforge.listeners.registry import build_listener
from pixelforge.script.model import ListenerConfig
from pixelforge.timeline.bus import TimelineBus

__all__ = ["ListenerManager", "ListenerStatus"]


@dataclass(frozen=True, slots=True)
class ListenerStatus:
    name: str
    running: bool
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "running": self.running, "error": self.error}


@dataclass(slots=True)
class _ManagedListener:
    listener: Listener | None = None
    error: str | None = None


class ListenerManager:
    """Start, report and stop listeners without letting one break the session."""

    def __init__(self, adb: AdbClient, bus: TimelineBus) -> None:
        self._adb = adb
        self._bus = bus
        self._active: dict[str, dict[str, _ManagedListener]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(
        self,
        serial: str,
        *,
        adb_serial: str,
        configs: list[ListenerConfig],
        app_package: str | None,
    ) -> list[ListenerStatus]:
        """Replace the listener set for ``serial`` with the requested config."""
        lock = self._locks.setdefault(serial, asyncio.Lock())
        async with lock:
            await self._stop_unlocked(serial)
            managed: dict[str, _ManagedListener] = {}
            self._active[serial] = managed
            context = ListenerContext(
                serial=adb_serial,
                bus=self._bus,
                app_package=app_package,
            )
            for config in configs:
                if not config.enabled:
                    continue
                entry = _ManagedListener()
                managed[config.name] = entry
                try:
                    listener = build_listener(config.name, self._adb, **config.options)
                    entry.listener = listener
                    await listener.start(context)
                except Exception as exc:  # partial capability is intentional
                    entry.error = f"{type(exc).__name__}: {exc}"
            return self._statuses(serial)

    async def stop(self, serial: str) -> None:
        lock = self._locks.setdefault(serial, asyncio.Lock())
        async with lock:
            await self._stop_unlocked(serial)

    async def close_all(self) -> None:
        for serial in list(self._active):
            await self.stop(serial)

    def statuses(self, serial: str) -> list[ListenerStatus]:
        return self._statuses(serial)

    def _statuses(self, serial: str) -> list[ListenerStatus]:
        return [
            ListenerStatus(
                name=name,
                running=bool(entry.listener and entry.listener.running),
                error=entry.error,
            )
            for name, entry in sorted(self._active.get(serial, {}).items())
        ]

    async def _stop_unlocked(self, serial: str) -> None:
        managed = self._active.pop(serial, {})
        for entry in managed.values():
            if entry.listener is not None:
                with contextlib.suppress(Exception):
                    await entry.listener.stop()
