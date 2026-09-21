"""AdbClient argv construction and safety rules.

No subprocess is spawned here: ``build_argv`` is deliberately separate from
``run`` so the security-relevant half is testable on its own.
"""

from __future__ import annotations

import pytest

from pixelforge.adb.client import AdbClient


@pytest.fixture
def adb() -> AdbClient:
    return AdbClient("adb", server_port=5038)


class TestBuildArgv:
    def test_always_pins_the_server_port(self, adb: AdbClient) -> None:
        # Not 5037: that port is a machine-wide singleton any other tool can
        # restart, which would drop every session we hold.
        assert adb.build_argv(["devices"]) == ["adb", "-P", "5038", "devices"]

    def test_inserts_serial_before_the_command(self, adb: AdbClient) -> None:
        argv = adb.build_argv(["shell", "wm", "size"], serial="ABC123")
        assert argv == ["adb", "-P", "5038", "-s", "ABC123", "shell", "wm", "size"]

    def test_arguments_stay_separate(self, adb: AdbClient) -> None:
        # Each element is its own argv entry, so no device-side shell ever
        # interprets a metacharacter inside one.
        argv = adb.build_argv(["shell", "am", "start", "-n", "a/b;rm -rf /"], serial="ABC")
        assert argv[-1] == "a/b;rm -rf /"
        assert argv == ["adb", "-P", "5038", "-s", "ABC", "shell", "am", "start", "-n",
                        "a/b;rm -rf /"]

    @pytest.mark.parametrize(
        "serial",
        ["has space", "semi;colon", "$(whoami)", "../../etc/passwd", "pipe|x", ""],
    )
    def test_rejects_unsafe_serial(self, adb: AdbClient, serial: str) -> None:
        with pytest.raises(ValueError, match="invalid adb serial"):
            adb.build_argv(["devices"], serial=serial)

    def test_rejects_shell_string_as_args(self, adb: AdbClient) -> None:
        with pytest.raises(TypeError, match="not a shell string"):
            adb.build_argv("devices -l")  # type: ignore[arg-type]

    def test_coerces_non_strings(self, adb: AdbClient) -> None:
        argv = adb.build_argv(["shell", "input", "tap", 540, 1200], serial="ABC")
        assert argv[-2:] == ["540", "1200"]


class TestShellSignature:
    async def test_shell_rejects_a_command_string(self, adb: AdbClient) -> None:
        # The common mistake this guards: adb.shell(s, "rm -rf /sdcard/*").
        with pytest.raises(TypeError, match="not a command string"):
            await adb.shell("ABC", "wm size")  # type: ignore[arg-type]

    async def test_shell_rejects_empty_argv(self, adb: AdbClient) -> None:
        with pytest.raises(ValueError, match="at least one argument"):
            await adb.shell("ABC", [])

    async def test_getprop_rejects_whitespace(self, adb: AdbClient) -> None:
        with pytest.raises(ValueError, match="invalid property name"):
            await adb.getprop("ABC", "ro.build.version.sdk; id")


class TestConfiguration:
    def test_rejects_invalid_port(self) -> None:
        with pytest.raises(ValueError, match="valid TCP port"):
            AdbClient("adb", server_port=0)
        with pytest.raises(ValueError, match="valid TCP port"):
            AdbClient("adb", server_port=70000)

    def test_env_points_children_at_our_server(self, adb: AdbClient) -> None:
        assert adb.env["ADB_SERVER_SOCKET"] == "tcp:127.0.0.1:5038"
