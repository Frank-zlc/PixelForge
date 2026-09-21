"""ADB host protocol parsing.

These are the wire-format tests. They matter more than they look: a wrong length
prefix or a mis-split device line shows up as "the device list is sometimes
empty", which is very hard to attribute once there is a real phone involved.
"""

from __future__ import annotations

import pytest

from pixelforge.adb.track import (
    AdbProtocolError,
    DeviceState,
    TrackedDevice,
    encode_request,
    is_valid_serial,
    parse_device_list,
    parse_length_prefix,
    parse_wm_size,
)


class TestEncodeRequest:
    def test_prefixes_four_hex_digits(self) -> None:
        assert encode_request("host:track-devices") == b"0012host:track-devices"

    def test_prefix_is_lowercase_hex(self) -> None:
        # 26 chars -> 0x1a. Uppercase "001A" is rejected by some adb builds.
        assert encode_request("host:version" + "x" * 14).startswith(b"001a")

    def test_empty_command(self) -> None:
        assert encode_request("") == b"0000"

    def test_rejects_oversized(self) -> None:
        with pytest.raises(ValueError, match="65535"):
            encode_request("x" * 70000)


class TestParseLengthPrefix:
    def test_decodes_hex(self) -> None:
        assert parse_length_prefix(b"0012") == 18
        assert parse_length_prefix(b"ffff") == 65535
        assert parse_length_prefix(b"0000") == 0

    @pytest.mark.parametrize("raw", [b"", b"12", b"00123", b"zzzz", b"\xff\xff\xff\xff"])
    def test_rejects_malformed(self, raw: bytes) -> None:
        with pytest.raises(AdbProtocolError):
            parse_length_prefix(raw)


class TestParseDeviceList:
    def test_tab_separated(self) -> None:
        payload = "ABC123\tdevice\nemulator-5554\toffline\n"
        assert parse_device_list(payload) == [
            TrackedDevice("ABC123", DeviceState.DEVICE),
            TrackedDevice("emulator-5554", DeviceState.OFFLINE),
        ]

    def test_empty_payload_means_no_devices(self) -> None:
        # A zero-length push is legal and means "nothing attached" -- it must not
        # be read as end of stream.
        assert parse_device_list("") == []

    def test_wireless_serial(self) -> None:
        devices = parse_device_list("192.168.1.9:5555\tdevice\n")
        assert devices == [TrackedDevice("192.168.1.9:5555", DeviceState.DEVICE)]

    def test_space_separated_fallback(self) -> None:
        # Some adb proxies emit spaces instead of a tab.
        assert parse_device_list("ABC123 device") == [
            TrackedDevice("ABC123", DeviceState.DEVICE)
        ]

    def test_multiword_state(self) -> None:
        devices = parse_device_list("ABC123\tno permissions; see [http://x]\n")
        assert devices[0].state is DeviceState.NO_PERMISSIONS

    def test_unauthorized_is_distinct(self) -> None:
        # Worth its own state: the fix is "tap Allow on the phone", which is very
        # different from "device is broken".
        devices = parse_device_list("ABC123\tunauthorized\n")
        assert devices[0].state is DeviceState.UNAUTHORIZED
        assert not devices[0].usable

    def test_drops_invalid_serial_but_keeps_the_rest(self) -> None:
        payload = "bad serial with spaces\ndevice\nGOOD1\tdevice\n"
        serials = [d.serial for d in parse_device_list(payload)]
        assert "GOOD1" in serials

    def test_ignores_daemon_chatter(self) -> None:
        payload = "* daemon started successfully *\nABC123\tdevice\n"
        assert len(parse_device_list(payload)) == 1

    def test_cli_header_is_not_a_device(self) -> None:
        """Regression: a phantom device called "List" in the UI.

        `adb devices` prints "List of devices attached"; the track-devices stream
        does not. Parsed as a device line, "List" clears the serial pattern and
        "of devices attached" degrades to UNKNOWN, so the header showed up in the
        device list beside the real phone.
        """
        payload = "List of devices attached\na5b132b4\tdevice\n\n"
        assert parse_device_list(payload) == [
            TrackedDevice("a5b132b4", DeviceState.DEVICE)
        ]

    def test_header_only_means_no_devices(self) -> None:
        assert parse_device_list("List of devices attached\n\n") == []

    def test_long_format_qualifiers_do_not_corrupt_the_state(self) -> None:
        """Regression: `adb devices -l` made a healthy phone read as UNKNOWN.

        The long format appends product/model/transport_id after the state.
        Joining everything after the serial turned "device product:picasso ..."
        into an unrecognised state.
        """
        payload = (
            "List of devices attached\n"
            "a5b132b4          device product:picasso model:Redmi_K30_5G transport_id:3\n"
        )
        (device,) = parse_device_list(payload)
        assert device.serial == "a5b132b4"
        assert device.state is DeviceState.DEVICE
        assert device.usable

    def test_header_with_daemon_chatter(self) -> None:
        payload = (
            "* daemon not running; starting now at tcp:5038 *\n"
            "* daemon started successfully *\n"
            "List of devices attached\n"
            "a5b132b4\tdevice\n"
        )
        assert [d.serial for d in parse_device_list(payload)] == ["a5b132b4"]

    def test_unknown_state_degrades(self) -> None:
        # adb grows states over time; an unknown one must not take the registry down.
        assert parse_device_list("ABC\tsomethingnew")[0].state is DeviceState.UNKNOWN


class TestSerialValidation:
    @pytest.mark.parametrize(
        "serial",
        ["ABC123", "emulator-5554", "192.168.1.9:5555", "a.b_c-1", "0" * 160],
    )
    def test_accepts_real_serials(self, serial: str) -> None:
        assert is_valid_serial(serial)

    @pytest.mark.parametrize(
        "serial",
        [
            "",
            "0" * 161,
            "has space",
            "semi;colon",
            "pipe|x",
            "$(whoami)",
            "back`tick`",
            "../../etc/passwd",
            "new\nline",
            "quote'x",
        ],
    )
    def test_rejects_anything_shell_unsafe(self, serial: str) -> None:
        # Serials reach argv. Rejecting is cheaper and safer than escaping.
        assert not is_valid_serial(serial)


class TestParseWmSize:
    def test_physical_only(self) -> None:
        assert parse_wm_size("Physical size: 1080x2400") == (1080, 2400)

    def test_override_wins(self) -> None:
        # The override is what the compositor uses, so it is what touch
        # coordinates and screencap pixels are in. adb prints it second.
        output = "Physical size: 1440x3200\nOverride size: 1080x2400\n"
        assert parse_wm_size(output) == (1080, 2400)

    def test_missing(self) -> None:
        assert parse_wm_size("") is None
        assert parse_wm_size("error: closed") is None

    def test_rejects_zero(self) -> None:
        assert parse_wm_size("Physical size: 0x0") is None
