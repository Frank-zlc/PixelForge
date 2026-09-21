"""ADB host protocol: request framing and ``host:track-devices`` parsing.

Everything here is pure -- no sockets, no subprocesses -- so the wire format can
be unit tested without a device or a running adb server. The I/O half lives in
:mod:`pixelforge.adb.client` and :mod:`pixelforge.device.provider`.

Wire format (see ADB's OVERVIEW.TXT / SERVICES.TXT):

    request   ::= <4 lowercase hex digits = payload length><payload>
    response  ::= "OKAY" | "FAIL" <4 hex length> <reason>
    push      ::= <4 hex length><payload>        # repeated, for track-devices

``host:track-devices`` answers OKAY once, then pushes one length-prefixed device
list every time the set of devices changes. A zero-length push is legal and
means "no devices attached" -- not end of stream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ADB_OKAY",
    "AdbProtocolError",
    "DeviceState",
    "TrackedDevice",
    "encode_request",
    "is_valid_serial",
    "parse_device_list",
    "parse_length_prefix",
    "parse_wm_size",
]

ADB_OKAY = b"OKAY"
ADB_FAIL = b"FAIL"

# Serials seen in the wild: USB serials, "emulator-5554", "192.168.1.9:5555",
# and Genymotion-style names. Deliberately strict: this value is interpolated
# into argv, so anything outside this set is rejected rather than escaped.
_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")

_WM_SIZE_RE = re.compile(r"(?:Override|Physical) size:\s*(\d+)x(\d+)")


class AdbProtocolError(RuntimeError):
    """The adb server sent something that does not match the host protocol."""


class DeviceState(str, Enum):
    """Transport state as reported by adb.

    ``DEVICE`` is the only state in which shell commands work. ``UNAUTHORIZED``
    means the user has not accepted the RSA prompt yet, which is a common and
    recoverable situation worth surfacing distinctly in the UI.
    """

    DEVICE = "device"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    BOOTLOADER = "bootloader"
    RECOVERY = "recovery"
    SIDELOAD = "sideload"
    RESCUE = "rescue"
    HOST = "host"
    NO_PERMISSIONS = "no permissions"
    AUTHORIZING = "authorizing"
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: str) -> DeviceState:
        normalised = raw.strip().lower()
        try:
            return cls(normalised)
        except ValueError:
            # adb grows new states over time (and prefixes some with
            # "no permissions; see [...]"). Degrade instead of raising so an
            # unknown state never takes the whole registry down.
            if normalised.startswith("no permissions"):
                return cls.NO_PERMISSIONS
            return cls.UNKNOWN

    @property
    def usable(self) -> bool:
        """True when shell / screencap / scrcpy can be expected to work."""
        return self is DeviceState.DEVICE


@dataclass(frozen=True, slots=True)
class TrackedDevice:
    serial: str
    state: DeviceState

    @property
    def usable(self) -> bool:
        return self.state.usable


def is_valid_serial(serial: str) -> bool:
    return bool(_SERIAL_RE.fullmatch(serial))


def encode_request(command: str) -> bytes:
    """Frame a host-protocol request.

    >>> encode_request("host:track-devices")
    b'0012host:track-devices'
    """
    payload = command.encode("utf-8")
    if len(payload) > 0xFFFF:
        raise ValueError("adb host request exceeds 65535 bytes")
    return f"{len(payload):04x}".encode("ascii") + payload


def parse_length_prefix(raw: bytes) -> int:
    """Decode the 4-hex-digit length prefix that precedes each pushed payload."""
    if len(raw) != 4:
        raise AdbProtocolError(f"length prefix must be 4 bytes, got {len(raw)}")
    try:
        return int(raw.decode("ascii"), 16)
    except (UnicodeDecodeError, ValueError) as exc:
        raise AdbProtocolError(f"malformed length prefix: {raw!r}") from exc


def parse_device_list(payload: str) -> list[TrackedDevice]:
    """Parse a ``host:track-devices`` payload into devices.

    Each line is ``<serial>\\t<state>``. Blank payloads (no devices attached)
    yield an empty list. Lines whose serial fails validation are dropped rather
    than raising: one malformed entry must not blind the registry to the rest.
    """
    devices: list[TrackedDevice] = []
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        # Real adb uses a tab, but some proxies emit spaces. Split on any run
        # of whitespace and keep only the first two fields.
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state_text = parts[0], " ".join(parts[1:])
        if not is_valid_serial(serial):
            continue
        devices.append(TrackedDevice(serial=serial, state=DeviceState.parse(state_text)))
    return devices


def parse_wm_size(output: str) -> tuple[int, int] | None:
    """Extract display size from ``adb shell wm size``.

    A device with a DPI/resolution override prints both lines::

        Physical size: 1440x3200
        Override size: 1080x2400

    The override is what the compositor actually uses, so it is what touch
    coordinates and screencap pixels are in. Returning the *last* match picks
    it, because adb prints Override after Physical.
    """
    matches = _WM_SIZE_RE.findall(output)
    if not matches:
        return None
    width, height = matches[-1]
    size = (int(width), int(height))
    return size if size[0] > 0 and size[1] > 0 else None
