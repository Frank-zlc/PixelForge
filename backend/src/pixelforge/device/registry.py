"""Device registry: the single source of truth for what is connected.

Consumes :class:`~pixelforge.device.provider.DeviceProvider` events, enriches new
devices with their properties, and fans state out to API callers and WebSocket
subscribers.

Two design notes worth keeping in mind when extending this:

* **Property fetching is off the event path.** ``adb shell getprop`` on a slow
  USB link takes 100-400ms; doing it inline would delay the "device appeared"
  event and, with several phones plugged in at once, serialise them all. New
  devices are published immediately with ``props_pending=True`` and updated when
  the details land.
* **Subscribers cannot slow the registry down.** Each gets a bounded queue; a
  subscriber that stops draining loses its oldest events rather than applying
  backpressure to device tracking.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace

from pixelforge.adb.client import AdbClient, AdbError, DeviceProps
from pixelforge.adb.track import DeviceState
from pixelforge.device.lease import LeaseManager
from pixelforge.device.models import DeviceView
from pixelforge.device.provider import DeviceEvent, DeviceEventKind, DeviceProvider

logger = logging.getLogger(__name__)

__all__ = ["DeviceRegistry", "RegistryEvent"]

_SUBSCRIBER_QUEUE_SIZE = 256
_LEASE_SWEEP_INTERVAL_S = 5.0


@dataclass(frozen=True, slots=True)
class RegistryEvent:
    """Something changed about the device set."""

    kind: str  # "added" | "removed" | "updated" | "lease_expired"
    device: DeviceView


@dataclass
class _Entry:
    serial: str
    state: DeviceState
    props: DeviceProps | None = None
    rotation: int = 0
    props_pending: bool = False
    last_error: str | None = None
    props_task: asyncio.Task[None] | None = field(default=None, repr=False)


class DeviceRegistry:
    """Live view of all devices from one provider."""

    def __init__(
        self,
        provider: DeviceProvider,
        adb: AdbClient,
        leases: LeaseManager,
    ) -> None:
        self._provider = provider
        self._adb = adb
        self._leases = leases
        self._entries: dict[str, _Entry] = {}
        self._subscribers: set[asyncio.Queue[RegistryEvent]] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Seed from a one-shot listing so the first HTTP request does not have to
        # wait for the tracker's initial push.
        for device in await self._provider.discover():
            self._upsert(device.serial, device.state)
        self._tasks = [
            asyncio.create_task(self._watch_loop(), name="pixelforge-device-watch"),
            asyncio.create_task(self._sweep_loop(), name="pixelforge-lease-sweep"),
        ]

    async def stop(self) -> None:
        self._started = False
        for entry in self._entries.values():
            if entry.props_task is not None:
                entry.props_task.cancel()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._entries.clear()

    # ----------------------------------------------------------------- reads

    def list(self) -> list[DeviceView]:
        return [self._view(entry) for entry in sorted(self._entries.values(), key=lambda e: e.serial)]

    def get(self, serial: str) -> DeviceView | None:
        entry = self._entries.get(serial)
        return self._view(entry) if entry else None

    def props(self, serial: str) -> DeviceProps | None:
        """Raw device properties, needed to construct a session's mapper."""
        entry = self._entries.get(serial)
        return entry.props if entry else None

    def require_usable(self, serial: str) -> DeviceView:
        """Fetch a device that is ready for shell/screencap, or explain why not."""
        view = self.get(serial)
        if view is None:
            raise KeyError(f"unknown device: {serial}")
        if not view.state.usable:
            raise RuntimeError(f"device {serial} is not ready (state={view.state.value})")
        return view

    # ------------------------------------------------------------ subscribe

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[RegistryEvent]]:
        """Subscribe to registry events for the duration of the context."""
        queue: asyncio.Queue[RegistryEvent] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)

        async def drain() -> AsyncIterator[RegistryEvent]:
            while True:
                yield await queue.get()

        try:
            yield drain()
        finally:
            self._subscribers.discard(queue)

    # --------------------------------------------------------------- private

    async def _watch_loop(self) -> None:
        try:
            async for event in self._provider.watch():
                self._apply(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the watcher must never die silently
            logger.exception("device watch loop crashed")

    async def _sweep_loop(self) -> None:
        """Expire stale leases so the UI stops showing a device as held."""
        try:
            while True:
                await asyncio.sleep(_LEASE_SWEEP_INTERVAL_S)
                for lease in self._leases.sweep():
                    entry = self._entries.get(lease.device_id)
                    if entry is not None:
                        self._publish("lease_expired", entry)
        except asyncio.CancelledError:
            raise

    def _apply(self, event: DeviceEvent) -> None:
        if event.kind is DeviceEventKind.REMOVED:
            entry = self._entries.pop(event.serial, None)
            if entry is None:
                return
            if entry.props_task is not None:
                entry.props_task.cancel()
            # An unplugged device cannot be controlled; drop any lease on it so
            # the slot frees up instead of blocking until TTL expiry.
            held = self._leases.current(event.serial)
            if held is not None:
                self._leases.release(held.token)
            self._publish("removed", replace(entry, state=DeviceState.DISCONNECTED))
            return

        self._upsert(event.serial, event.state)

    def _upsert(self, serial: str, state: DeviceState) -> None:
        entry = self._entries.get(serial)
        if entry is None:
            entry = _Entry(serial=serial, state=state)
            self._entries[serial] = entry
            self._publish("added", entry)
        elif entry.state is not state:
            entry.state = state
            self._publish("updated", entry)
        else:
            return

        # Properties are only readable in DEVICE state, and are worth
        # re-reading after a state change (a reboot can change the display size).
        if state.usable and entry.props is None and entry.props_task is None:
            entry.props_pending = True
            entry.props_task = asyncio.create_task(
                self._load_props(serial), name=f"pixelforge-props-{serial}"
            )

    async def _load_props(self, serial: str) -> None:
        try:
            props = await self._adb.props(serial)
        except asyncio.CancelledError:
            raise
        except AdbError as exc:
            entry = self._entries.get(serial)
            if entry is not None:
                entry.props_pending = False
                entry.props_task = None
                entry.last_error = str(exc)
                self._publish("updated", entry)
            logger.warning("failed to read properties for %s: %s", serial, exc)
            return

        entry = self._entries.get(serial)
        if entry is None:  # unplugged while we were reading
            return
        entry.props = props
        entry.props_pending = False
        entry.props_task = None
        entry.last_error = None
        self._publish("updated", entry)

    def _view(self, entry: _Entry) -> DeviceView:
        lease = self._leases.current(entry.serial)
        return DeviceView.build(
            serial=entry.serial,
            state=entry.state,
            props=entry.props,
            rotation=entry.rotation,
            lease=lease.public() if lease else None,
            props_pending=entry.props_pending,
            last_error=entry.last_error,
        )

    def _publish(self, kind: str, entry: _Entry) -> None:
        event = RegistryEvent(kind=kind, device=self._view(entry))
        for queue in self._subscribers:
            if queue.full():
                # Drop the oldest rather than block device tracking on a slow
                # or abandoned WebSocket.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
