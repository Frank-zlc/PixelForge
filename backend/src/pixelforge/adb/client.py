"""Asynchronous, allow-listed ADB client.

Two rules shape this module, both of which exist because PixelForge drives many
devices from one event loop:

1. **Never block the loop.** Every adb invocation goes through
   ``asyncio.create_subprocess_exec``. A single synchronous ``subprocess.run``
   here would stall the H.264 relay for *every* connected device, which presents
   as "the video freezes when someone takes a screenshot" and is miserable to
   track down.

2. **Never accept a shell string.** Callers pass an argv list. ``shell()`` takes
   ``Sequence[str]`` and refuses a bare ``str``, so there is no code path where
   a template-derived value reaches ``sh -c``.

The client also pins its own adb server port. The default ``127.0.0.1:5037`` is
a machine-wide singleton: anyone opening Android Studio, or any tool running
``adb kill-server``, drops every session PixelForge holds.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pixelforge.adb.track import (
    DeviceState,
    TrackedDevice,
    is_valid_serial,
    parse_device_list,
    parse_wm_size,
)

__all__ = [
    "AdbClient",
    "AdbError",
    "AdbNotFoundError",
    "AdbTimeoutError",
    "DeviceProps",
]

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class AdbError(RuntimeError):
    """An adb invocation returned a non-zero exit status."""

    def __init__(self, message: str, *, argv: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.argv = list(argv or ())


class AdbNotFoundError(AdbError):
    """The adb executable could not be found."""


class AdbTimeoutError(AdbError):
    """An adb invocation exceeded its timeout and was killed."""


@dataclass(frozen=True, slots=True)
class DeviceProps:
    """Static-ish device facts gathered once per connection."""

    serial: str
    model: str | None
    manufacturer: str | None
    android_release: str | None
    sdk_int: int | None
    size: tuple[int, int] | None
    density: int | None

    @property
    def label(self) -> str:
        if self.manufacturer and self.model:
            return f"{self.manufacturer} {self.model}"
        return self.model or self.serial


class AdbClient:
    """Thin async wrapper around the adb CLI.

    Parameters
    ----------
    executable:
        Path to the adb binary. Vendor a pinned copy into the repo rather than
        relying on ``$PATH``: a version mismatch between two adb binaries makes
        them kill each other's servers.
    server_port:
        Port for this client's private adb server. Passed as ``-P``.
    timeout:
        Default per-invocation timeout in seconds.
    """

    def __init__(
        self,
        executable: str | Path = "adb",
        *,
        server_port: int = 5038,
        timeout: float = 15.0,
    ) -> None:
        if not 1 <= server_port <= 65535:
            raise ValueError("server_port must be a valid TCP port")
        self._executable = str(executable)
        self._server_port = server_port
        self._timeout = timeout

    @property
    def server_port(self) -> int:
        return self._server_port

    @property
    def env(self) -> dict[str, str]:
        """Environment that points child adb clients at our private server."""
        return {
            **os.environ,
            "ADB_SERVER_SOCKET": f"tcp:127.0.0.1:{self._server_port}",
        }

    def resolve_executable(self) -> str:
        """Return an absolute path to adb, raising if it is not usable.

        Checked eagerly at startup so a missing adb is a clear boot error rather
        than a confusing failure on the first device call.
        """
        found = shutil.which(self._executable)
        if found is None:
            candidate = Path(self._executable)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
            raise AdbNotFoundError(f"adb executable not found: {self._executable}")
        return found

    # ------------------------------------------------------------------ core

    async def run(
        self,
        args: Sequence[str],
        *,
        serial: str | None = None,
        timeout: float | None = None,
        binary: bool = False,
    ) -> bytes:
        """Invoke adb with an explicit argv and return stdout.

        Always returns bytes; use :meth:`run_text` when a string is wanted.
        """
        argv = self.build_argv(args, serial=serial)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except FileNotFoundError as exc:
            raise AdbNotFoundError(
                f"adb executable not found: {self._executable}", argv=argv
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout or self._timeout
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            # communicate() leaves the child running on timeout; reap it so we
            # do not leak an adb process per timed-out call.
            process.kill()
            await process.wait()
            raise AdbTimeoutError(
                f"adb timed out after {timeout or self._timeout}s: {' '.join(args)}",
                argv=argv,
            ) from exc

        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise AdbError(detail or f"adb exited with {process.returncode}", argv=argv)
        _ = binary  # kept for call-site readability; stdout is always bytes
        return stdout

    async def run_text(
        self,
        args: Sequence[str],
        *,
        serial: str | None = None,
        timeout: float | None = None,
    ) -> str:
        raw = await self.run(args, serial=serial, timeout=timeout)
        return raw.decode("utf-8", errors="replace")

    def build_argv(self, args: Sequence[str], *, serial: str | None = None) -> list[str]:
        """Build the full argv, validating the serial.

        Separated from :meth:`run` so argv construction is unit-testable without
        spawning anything.
        """
        if isinstance(args, str):  # pragma: no cover - guards a common mistake
            raise TypeError("args must be a sequence of strings, not a shell string")
        argv = [self._executable, "-P", str(self._server_port)]
        if serial is not None:
            if not is_valid_serial(serial):
                raise ValueError(f"invalid adb serial: {serial!r}")
            argv += ["-s", serial]
        argv += [str(a) for a in args]
        return argv

    # ------------------------------------------------------------- lifecycle

    async def start_server(self) -> None:
        await self.run(["start-server"], timeout=30.0)

    async def version(self) -> str:
        return (await self.run_text(["version"])).strip()

    # --------------------------------------------------------------- devices

    async def devices(self) -> list[TrackedDevice]:
        """One-shot device list. Prefer the tracking provider for live state."""
        return parse_device_list(await self.run_text(["devices"]))

    async def shell(
        self,
        serial: str,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str:
        """Run a shell command from an argv list.

        ``argv`` is forwarded as separate arguments, so no shell metacharacter in
        any element is interpreted by the device's shell.
        """
        if isinstance(argv, str):
            raise TypeError("shell() takes a sequence of arguments, not a command string")
        if not argv:
            raise ValueError("shell() requires at least one argument")
        return await self.run_text(["shell", *argv], serial=serial, timeout=timeout)

    async def getprop(self, serial: str, name: str) -> str | None:
        if not name or any(c.isspace() for c in name):
            raise ValueError(f"invalid property name: {name!r}")
        value = (await self.shell(serial, ["getprop", name])).strip()
        return value or None

    async def props(self, serial: str) -> DeviceProps:
        """Collect the device facts the UI and CoordinateMapper need.

        Queries run concurrently: on a slow USB link these are ~50-150ms each and
        serialising five of them is a visible delay when a device is plugged in.
        """
        release, sdk_raw, model, manufacturer, size_out, density_out = await asyncio.gather(
            self.getprop(serial, "ro.build.version.release"),
            self.getprop(serial, "ro.build.version.sdk"),
            self.getprop(serial, "ro.product.model"),
            self.getprop(serial, "ro.product.manufacturer"),
            self.shell(serial, ["wm", "size"]),
            self.shell(serial, ["wm", "density"]),
        )
        density: int | None = None
        for token in density_out.replace(":", " ").split():
            if token.isdigit():
                density = int(token)  # last number wins: Override beats Physical
        return DeviceProps(
            serial=serial,
            model=model,
            manufacturer=manufacturer,
            android_release=release,
            sdk_int=int(sdk_raw) if sdk_raw and sdk_raw.isdigit() else None,
            size=parse_wm_size(size_out),
            density=density,
        )

    async def state(self, serial: str) -> DeviceState:
        return DeviceState.parse(await self.run_text(["get-state"], serial=serial))

    # ------------------------------------------------------------ screencap

    async def screencap_png(self, serial: str, *, timeout: float = 30.0) -> bytes:
        """Capture a **lossless** PNG screenshot.

        This is the material-capture path and is deliberately separate from the
        H.264 video stream: a template cropped from a lossy video frame will not
        match reliably against a later PNG, and the resulting flaky
        ``matchTemplate`` scores are very hard to diagnose.
        """
        payload = await self.run(["exec-out", "screencap", "-p"], serial=serial, timeout=timeout)
        if not payload.startswith(_PNG_MAGIC):
            raise AdbError(
                "screencap did not return a PNG "
                f"(got {len(payload)} bytes starting {payload[:8]!r})"
            )
        return payload

    # -------------------------------------------------------------- forward

    async def forward(self, serial: str, local: str, remote: str) -> str:
        """Create a port forward and return the resolved local port.

        Pass ``local='tcp:0'`` to let adb pick a free port -- it prints the
        chosen one. Hardcoding a port breaks the moment two devices are used at
        once, which is the normal case here.
        """
        out = (await self.run_text(["forward", local, remote], serial=serial)).strip()
        if local == "tcp:0":
            if not out.isdigit():
                raise AdbError(f"adb forward did not report a port, got {out!r}")
            return out
        return local.removeprefix("tcp:")

    async def forward_remove(self, serial: str, local: str) -> None:
        await self.run(["forward", "--remove", local], serial=serial)

    async def push(self, serial: str, local: Path, remote: str) -> None:
        source = Path(local)
        if not source.is_file():
            raise FileNotFoundError(f"cannot push missing file: {source}")
        await self.run(["push", str(source), remote], serial=serial, timeout=120.0)
