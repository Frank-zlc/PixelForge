"""Which adb server actually holds the phone.

The single most confusing failure in this tool is an empty device list next to a
terminal where ``adb devices`` clearly shows a phone. The cause is never obvious
from the UI: **a USB device can only be claimed by one adb server at a time.**
Whichever server opens the USB handle first owns it, and every other server --
on a different port, started by a different tool -- sees nothing at all.

So instead of guessing, we ask. This probes each candidate port with the adb
host protocol and reports what that server can see. It deliberately never starts
a server: probing must observe the machine, not change it.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from pixelforge.adb.track import (
    ADB_OKAY,
    AdbProtocolError,
    TrackedDevice,
    encode_request,
    parse_device_list,
    parse_length_prefix,
)

__all__ = ["DEFAULT_ADB_PORT", "ServerProbe", "probe_servers", "recommend_port"]

# The machine-wide default every other Android tool uses.
DEFAULT_ADB_PORT = 5037

_PROBE_TIMEOUT_S = 1.5


@dataclass(slots=True)
class ServerProbe:
    """What one adb server on one port reports about itself."""

    port: int
    running: bool = False
    version: str | None = None
    devices: list[TrackedDevice] = field(default_factory=list)
    error: str | None = None

    @property
    def usable_devices(self) -> list[TrackedDevice]:
        """Devices that could actually be driven, ignoring offline transports."""
        return [d for d in self.devices if d.state.value not in ("offline", "disconnected")]

    def as_dict(self) -> dict[str, object]:
        return {
            "port": self.port,
            "running": self.running,
            "version": self.version,
            "error": self.error,
            "devices": [
                {"serial": d.serial, "state": d.state.value} for d in self.devices
            ],
        }


async def _host_request(host: str, port: int, command: str) -> str:
    """One adb host: request. Each needs its own connection -- the server closes
    the socket after answering."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=_PROBE_TIMEOUT_S
    )
    try:
        writer.write(encode_request(command))
        await writer.drain()
        status = await asyncio.wait_for(reader.readexactly(4), timeout=_PROBE_TIMEOUT_S)
        length = parse_length_prefix(await reader.readexactly(4))
        payload = (await reader.readexactly(length)).decode("utf-8", errors="replace") if length else ""
        if status != ADB_OKAY:
            raise AdbProtocolError(payload or f"{command} rejected")
        return payload
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def probe_server(host: str, port: int) -> ServerProbe:
    probe = ServerProbe(port=port)
    try:
        raw_version = await _host_request(host, port, "host:version")
        probe.running = True
        # host:version answers in hex, which is meaningless to a reader.
        with contextlib.suppress(ValueError):
            probe.version = str(int(raw_version, 16))
        probe.devices = parse_device_list(await _host_request(host, port, "host:devices"))
    except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
        # Nothing listening is the normal case for a port nobody uses.
        probe.running = False
    except (AdbProtocolError, asyncio.IncompleteReadError) as exc:
        probe.running = True
        probe.error = str(exc) or exc.__class__.__name__
    return probe


async def probe_servers(host: str, ports: list[int]) -> list[ServerProbe]:
    """Probe every candidate port concurrently, in ascending port order."""
    unique = sorted(set(ports))
    return list(await asyncio.gather(*(probe_server(host, port) for port in unique)))


def recommend_port(probes: list[ServerProbe], active_port: int) -> int | None:
    """The port worth switching to, or None when the current one is fine.

    Only ever recommends a port that can see a device the active one cannot --
    the exact situation where the UI would otherwise just say "no devices".
    """
    active = next((p for p in probes if p.port == active_port), None)
    if active is not None and active.usable_devices:
        return None
    candidates = [p for p in probes if p.port != active_port and p.usable_devices]
    if not candidates:
        return None
    # Prefer the machine-wide default: it is the one other tools keep alive.
    for probe in candidates:
        if probe.port == DEFAULT_ADB_PORT:
            return probe.port
    return candidates[0].port
