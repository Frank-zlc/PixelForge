"""Device tracking against a fake adb server.

The fake server speaks the real host protocol over a real socket, so this
exercises the framing, the OKAY handshake, length-prefixed pushes and the
snapshot diffing without needing adb or a phone. That combination -- protocol
plus reconnect plus diffing -- is where plug/unplug bugs actually live.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from pixelforge.adb.client import AdbClient
from pixelforge.adb.track import ADB_OKAY, DeviceState, encode_request
from pixelforge.device.provider import DeviceEventKind, LocalAdbProvider, _diff
from pixelforge.device.provider import TrackedDevice as _TD  # re-exported type


class FakeAdbServer:
    """Minimal ``host:track-devices`` server.

    Pushes whatever payloads the test queues, then optionally drops the
    connection so reconnect behaviour can be observed.
    """

    def __init__(self) -> None:
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.connections = 0
        self.requests: list[bytes] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def push(self, payload: str) -> None:
        """Queue a device-list payload to send."""
        self._queue.put_nowait(payload)

    def drop(self) -> None:
        """Queue a connection close, to test reconnect."""
        self._queue.put_nowait(None)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            length = int((await reader.readexactly(4)).decode(), 16)
            self.requests.append(await reader.readexactly(length))
            writer.write(ADB_OKAY)
            await writer.drain()
            while True:
                payload = await self._queue.get()
                if payload is None:
                    return
                raw = payload.encode()
                writer.write(f"{len(raw):04x}".encode() + raw)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


@pytest.fixture
async def server() -> AsyncIterator[FakeAdbServer]:
    fake = FakeAdbServer()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
def provider(server: FakeAdbServer) -> LocalAdbProvider:
    # "adb" is absent in the test environment; start_server() fails and is
    # suppressed, which is exactly the production path when the binary is
    # missing. The tracking socket still works because it talks to our fake.
    return LocalAdbProvider(AdbClient("adb-does-not-exist", server_port=server.port))


async def collect(
    provider: LocalAdbProvider, count: int, *, timeout: float = 5.0
) -> list[object]:
    """Read ``count`` events from watch(), then cancel the generator."""
    events: list[object] = []
    stream = provider.watch()

    async def pump() -> None:
        async for event in stream:
            events.append(event)
            if len(events) >= count:
                return

    await asyncio.wait_for(pump(), timeout=timeout)
    await stream.aclose()
    return events


class TestTrackDevices:
    async def test_sends_the_right_request(
        self, provider: LocalAdbProvider, server: FakeAdbServer
    ) -> None:
        server.push("ABC123\tdevice\n")
        await collect(provider, 1)
        assert server.requests[0] == b"host:track-devices"
        assert encode_request("host:track-devices").endswith(server.requests[0])

    async def test_added_event(
        self, provider: LocalAdbProvider, server: FakeAdbServer
    ) -> None:
        server.push("ABC123\tdevice\n")
        (event,) = await collect(provider, 1)
        assert event.kind is DeviceEventKind.ADDED  # type: ignore[attr-defined]
        assert event.serial == "ABC123"  # type: ignore[attr-defined]
        assert event.state is DeviceState.DEVICE  # type: ignore[attr-defined]

    async def test_removed_when_device_leaves_the_list(
        self, provider: LocalAdbProvider, server: FakeAdbServer
    ) -> None:
        server.push("ABC123\tdevice\n")
        server.push("")  # empty push == nothing attached, not end of stream
        events = await collect(provider, 2)
        assert events[1].kind is DeviceEventKind.REMOVED  # type: ignore[attr-defined]
        assert events[1].serial == "ABC123"  # type: ignore[attr-defined]

    async def test_state_change_event(
        self, provider: LocalAdbProvider, server: FakeAdbServer
    ) -> None:
        server.push("ABC123\tunauthorized\n")
        server.push("ABC123\tdevice\n")
        events = await collect(provider, 2)
        assert events[1].kind is DeviceEventKind.STATE_CHANGED  # type: ignore[attr-defined]
        assert events[1].previous is DeviceState.UNAUTHORIZED  # type: ignore[attr-defined]
        assert events[1].state is DeviceState.DEVICE  # type: ignore[attr-defined]

    async def test_no_event_when_nothing_changed(
        self, provider: LocalAdbProvider, server: FakeAdbServer
    ) -> None:
        # adb re-pushes on any change; an identical list must stay silent so the
        # UI does not flicker.
        server.push("ABC123\tdevice\n")
        server.push("ABC123\tdevice\n")
        server.push("DEF456\tdevice\n")
        events = await collect(provider, 2)
        assert [e.serial for e in events] == ["ABC123", "DEF456"]  # type: ignore[attr-defined]
        assert events[1].kind is DeviceEventKind.ADDED  # type: ignore[attr-defined]

    async def test_multiple_devices_in_one_push(
        self, provider: LocalAdbProvider, server: FakeAdbServer
    ) -> None:
        server.push("ABC\tdevice\nDEF\tdevice\n192.168.1.9:5555\tdevice\n")
        events = await collect(provider, 3)
        assert {e.serial for e in events} == {"ABC", "DEF", "192.168.1.9:5555"}  # type: ignore[attr-defined]

    async def test_reconnects_and_reports_devices_gone(
        self, provider: LocalAdbProvider, server: FakeAdbServer
    ) -> None:
        """A dropped adb server must not end tracking.

        Anyone running ``adb kill-server`` triggers this, so it is a normal
        transient, not an error path. On reconnect every previously visible
        device is reported gone first, because we cannot assume it is still there.
        """
        server.push("ABC123\tdevice\n")
        server.drop()
        events = await collect(provider, 2, timeout=10.0)
        assert events[0].kind is DeviceEventKind.ADDED  # type: ignore[attr-defined]
        assert events[1].kind is DeviceEventKind.REMOVED  # type: ignore[attr-defined]
        assert events[1].state is DeviceState.DISCONNECTED  # type: ignore[attr-defined]


class TestDiff:
    def test_added(self) -> None:
        events = _diff({}, [_TD("A", DeviceState.DEVICE)])
        assert [(e.kind, e.serial) for e in events] == [(DeviceEventKind.ADDED, "A")]

    def test_removed(self) -> None:
        events = _diff({"A": DeviceState.DEVICE}, [])
        assert events[0].kind is DeviceEventKind.REMOVED
        assert events[0].previous is DeviceState.DEVICE

    def test_unchanged_yields_nothing(self) -> None:
        assert _diff({"A": DeviceState.DEVICE}, [_TD("A", DeviceState.DEVICE)]) == []

    def test_simultaneous_add_and_remove(self) -> None:
        events = _diff({"A": DeviceState.DEVICE}, [_TD("B", DeviceState.DEVICE)])
        kinds = {(e.kind, e.serial) for e in events}
        assert kinds == {(DeviceEventKind.ADDED, "B"), (DeviceEventKind.REMOVED, "A")}


class TestEndpoint:
    async def test_local_endpoint_is_the_serial(self, provider: LocalAdbProvider) -> None:
        assert await provider.adb_endpoint("ABC123") == "ABC123"

    async def test_discover_survives_missing_adb(self, provider: LocalAdbProvider) -> None:
        # A missing or dead adb binary must read as "no devices", not crash the
        # registry on startup.
        assert await provider.discover() == []
