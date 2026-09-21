"""Device providers: where devices come from.

``DeviceProvider`` is the seam that lets PixelForge use a local USB phone, a
DeviceFarmer/STF pool, or a cloud device farm without the layers above knowing
which. The important method is :meth:`DeviceProvider.adb_endpoint`: whatever the
source, the rest of the system only ever receives *an adb-reachable serial*, so
the scrcpy, screencap and uiautomator paths are identical in every case.

``LocalAdbProvider`` watches devices over ``host:track-devices``, a long-lived
socket the adb server pushes to on every change. Polling ``adb devices`` in a
loop would fork a process per second and still report plug/unplug up to a second
late; tracking is both cheaper and effectively instant.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pixelforge.adb.client import AdbClient, AdbError, DeviceProps
from pixelforge.adb.track import (
    ADB_OKAY,
    AdbProtocolError,
    DeviceState,
    TrackedDevice,
    encode_request,
    parse_device_list,
    parse_length_prefix,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DeviceEvent",
    "DeviceEventKind",
    "DeviceProvider",
    "LocalAdbProvider",
]

# Backoff for reconnecting the tracking socket. The adb server can be killed by
# any other tool on the machine, so dropping out is expected, not exceptional.
_RECONNECT_MIN_S = 0.5
_RECONNECT_MAX_S = 10.0


class DeviceEventKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    STATE_CHANGED = "state_changed"


@dataclass(frozen=True, slots=True)
class DeviceEvent:
    kind: DeviceEventKind
    serial: str
    state: DeviceState
    previous: DeviceState | None = None


@runtime_checkable
class DeviceProvider(Protocol):
    """A source of devices."""

    name: str

    async def discover(self) -> list[TrackedDevice]:
        """One-shot listing of currently visible devices."""
        ...

    def watch(
        self, known: dict[str, DeviceState] | None = None
    ) -> AsyncIterator[DeviceEvent]:
        """Yield events as devices appear, disappear or change state.

        ``known`` seeds the diff with what the caller already believes is
        attached, so the first snapshot can retire anything stale. Should run
        until cancelled, reconnecting internally on transport loss.
        """
        ...

    async def adb_endpoint(self, serial: str) -> str:
        """Return the adb serial to address this device by.

        For local USB this is the serial itself. For a remote pool it is
        whatever ``adb connect`` produced (e.g. ``10.0.0.5:5555``), after any
        reservation the pool requires.
        """
        ...

    async def properties(self, serial: str) -> DeviceProps:
        """Return enough display/device metadata to open a session."""
        ...

    async def acquire(self, serial: str, *, ttl_s: float) -> str:
        """Reserve the device and return its adb-reachable serial."""
        ...

    async def renew(self, serial: str, *, ttl_s: float) -> None:
        """Keep an external reservation alive, if the provider has one."""
        ...

    async def release(self, serial: str) -> None:
        """Release any provider-side reservation and remote adb tunnel."""
        ...

    async def close(self) -> None:
        """Close provider resources."""
        ...


class LocalAdbProvider:
    """Devices attached to this machine's adb server."""

    name = "local-adb"

    def __init__(self, adb: AdbClient) -> None:
        self._adb = adb

    async def discover(self) -> list[TrackedDevice]:
        try:
            return await self._adb.devices()
        except AdbError:
            # An unreachable adb server is a normal transient state (someone
            # ran kill-server). Report "no devices" and let watch() recover.
            logger.warning("adb devices failed; reporting empty device list", exc_info=True)
            return []

    async def adb_endpoint(self, serial: str) -> str:
        return serial

    async def properties(self, serial: str) -> DeviceProps:
        return await self._adb.props(serial)

    async def acquire(self, serial: str, *, ttl_s: float) -> str:
        _ = ttl_s
        return serial

    async def renew(self, serial: str, *, ttl_s: float) -> None:
        _ = (serial, ttl_s)

    async def release(self, serial: str) -> None:
        _ = serial

    async def close(self) -> None:
        pass

    async def watch(
        self, known: dict[str, DeviceState] | None = None
    ) -> AsyncIterator[DeviceEvent]:
        """Track devices, diffing successive snapshots into events.

        ``host:track-devices`` pushes a full list each time anything changes, so
        the diffing happens here rather than on the wire.

        ``known`` seeds the diff with what the caller already believes is
        attached. Starting from empty instead leaves a gap: anything the initial
        ``discover()`` found but the stream does not report is never diffed away,
        so it lingers in the UI forever. That is how a phantom entry survives
        even after the thing that produced it is gone.
        """
        known = dict(known or {})
        delay = _RECONNECT_MIN_S
        first_attempt = True

        while True:
            try:
                async for devices in self._stream_device_lists(
                    ensure_server=not first_attempt or True
                ):
                    delay = _RECONNECT_MIN_S  # a successful push resets backoff
                    for event in _diff(known, devices):
                        yield event
                    known = {d.serial: d.state for d in devices}
            except asyncio.CancelledError:
                raise
            except (OSError, AdbProtocolError, AdbError) as exc:
                logger.warning("device tracking connection lost: %s", exc)
            finally:
                first_attempt = False

            # Everything that was visible through the dead socket is now unknown.
            for serial, state in list(known.items()):
                yield DeviceEvent(
                    kind=DeviceEventKind.REMOVED,
                    serial=serial,
                    state=DeviceState.DISCONNECTED,
                    previous=state,
                )
            known.clear()

            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX_S)

    async def _stream_device_lists(
        self, *, ensure_server: bool
    ) -> AsyncIterator[list[TrackedDevice]]:
        """Yield each device list the adb server pushes on one connection."""
        if ensure_server:
            with contextlib.suppress(AdbError):
                await self._adb.start_server()

        reader, writer = await asyncio.open_connection(
            self._adb.server_host, self._adb.server_port
        )
        try:
            writer.write(encode_request("host:track-devices"))
            await writer.drain()

            status = await reader.readexactly(4)
            if status != ADB_OKAY:
                reason = await _read_failure_reason(reader)
                raise AdbProtocolError(f"track-devices rejected: {status!r} {reason}")

            logger.info(
                "device tracking established on %s:%d",
                self._adb.server_host,
                self._adb.server_port,
            )
            while True:
                length = parse_length_prefix(await reader.readexactly(4))
                # A zero-length push is valid and means "nothing attached".
                payload = await reader.readexactly(length) if length else b""
                yield parse_device_list(payload.decode("utf-8", errors="replace"))
        except asyncio.IncompleteReadError as exc:
            raise OSError("adb server closed the tracking connection") from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def _read_failure_reason(reader: asyncio.StreamReader) -> str:
    with contextlib.suppress(Exception):
        length = parse_length_prefix(await reader.readexactly(4))
        return (await reader.readexactly(length)).decode("utf-8", errors="replace")
    return ""


def _diff(
    known: dict[str, DeviceState], devices: list[TrackedDevice]
) -> list[DeviceEvent]:
    """Turn two snapshots into added / removed / state_changed events."""
    events: list[DeviceEvent] = []
    seen = {d.serial: d.state for d in devices}

    for serial, state in seen.items():
        previous = known.get(serial)
        if previous is None:
            events.append(
                DeviceEvent(kind=DeviceEventKind.ADDED, serial=serial, state=state)
            )
        elif previous is not state:
            events.append(
                DeviceEvent(
                    kind=DeviceEventKind.STATE_CHANGED,
                    serial=serial,
                    state=state,
                    previous=previous,
                )
            )

    for serial, previous in known.items():
        if serial not in seen:
            events.append(
                DeviceEvent(
                    kind=DeviceEventKind.REMOVED,
                    serial=serial,
                    state=DeviceState.DISCONNECTED,
                    previous=previous,
                )
            )
    return events
