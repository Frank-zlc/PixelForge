"""Probing adb servers against a fake one.

The behaviour under test is a diagnosis, so the cases that matter are the
awkward ones: a port with nothing on it, a server that answers but sees no
phone, and the split where one server holds the device and the other does not.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from pixelforge.adb.probe import (
    DEFAULT_ADB_PORT,
    ServerProbe,
    probe_server,
    probe_servers,
    recommend_port,
)
from pixelforge.adb.track import ADB_OKAY, DeviceState, TrackedDevice


class FakeHostServer:
    """Answers one ``host:`` request per connection, as adb does."""

    def __init__(self, *, version: str = "29", devices: str = "") -> None:
        self.port = 0
        self.version = version
        self.devices = devices
        self.requests: list[str] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            length = int((await reader.readexactly(4)).decode(), 16)
            command = (await reader.readexactly(length)).decode()
            self.requests.append(command)
            payload = self.version if command == "host:version" else self.devices
            raw = payload.encode()
            writer.write(ADB_OKAY + f"{len(raw):04x}".encode() + raw)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


@pytest.fixture
async def empty_server() -> AsyncIterator[FakeHostServer]:
    fake = FakeHostServer()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def busy_server() -> AsyncIterator[FakeHostServer]:
    fake = FakeHostServer(devices="a5b132b4\tdevice\nemulator-5554\toffline\n")
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


class TestProbeServer:
    async def test_reports_a_port_with_nothing_listening(self) -> None:
        # Port 1 needs root to bind, so nothing of ours can be there.
        probe = await probe_server("127.0.0.1", 1)
        assert probe.running is False
        assert probe.devices == []

    async def test_reads_version_and_devices(self, busy_server: FakeHostServer) -> None:
        probe = await probe_server("127.0.0.1", busy_server.port)
        assert probe.running is True
        # adb answers host:version in hex; a raw "29" would be a lie to the reader.
        assert probe.version == str(0x29)
        assert [d.serial for d in probe.devices] == ["a5b132b4", "emulator-5554"]
        assert busy_server.requests == ["host:version", "host:devices"]

    async def test_offline_transports_are_not_usable(self, busy_server: FakeHostServer) -> None:
        probe = await probe_server("127.0.0.1", busy_server.port)
        assert [d.serial for d in probe.usable_devices] == ["a5b132b4"]

    async def test_probes_run_concurrently_and_sort_by_port(
        self, empty_server: FakeHostServer, busy_server: FakeHostServer
    ) -> None:
        ports = [busy_server.port, empty_server.port, 1]
        probes = await probe_servers("127.0.0.1", ports)
        assert [p.port for p in probes] == sorted(ports)


class TestRecommendPort:
    def _probe(self, port: int, *, serial: str | None, state: str = "device") -> ServerProbe:
        devices = (
            [TrackedDevice(serial=serial, state=DeviceState.parse(state))] if serial else []
        )
        return ServerProbe(port=port, running=True, version="41", devices=devices)

    def test_silent_when_the_active_server_has_the_device(self) -> None:
        probes = [self._probe(5038, serial="a5b132b4"), self._probe(5037, serial=None)]
        assert recommend_port(probes, 5038) is None

    def test_points_at_the_server_holding_the_device(self) -> None:
        probes = [self._probe(5038, serial=None), self._probe(5037, serial="a5b132b4")]
        assert recommend_port(probes, 5038) == 5037

    def test_silent_when_nobody_has_a_device(self) -> None:
        probes = [self._probe(5038, serial=None), self._probe(5037, serial=None)]
        assert recommend_port(probes, 5038) is None

    def test_an_offline_transport_is_not_worth_switching_to(self) -> None:
        probes = [
            self._probe(5038, serial=None),
            self._probe(5037, serial="a5b132b4", state="offline"),
        ]
        assert recommend_port(probes, 5038) is None

    def test_prefers_the_machine_wide_default_over_another_private_port(self) -> None:
        probes = [
            self._probe(5100, serial=None),
            self._probe(5039, serial="other"),
            self._probe(DEFAULT_ADB_PORT, serial="a5b132b4"),
        ]
        assert recommend_port(probes, 5100) == DEFAULT_ADB_PORT
