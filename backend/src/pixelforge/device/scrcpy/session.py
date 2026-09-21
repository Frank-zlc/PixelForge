"""Lifecycle of one scrcpy server on one device.

Sequence, and why each step is where it is:

1. ``adb push`` the server jar. Re-pushed every session rather than cached --
   a stale jar whose protocol no longer matches produces a confusing decode
   failure rather than a clear error.
2. Launch it through ``app_process``. The server is a plain Java program run in
   the shell UID, using only public APIs (``MediaCodec``, ``InputManager``),
   which is why it keeps working from Android 8 through 16 while ``minicap``
   needs a freshly compiled ``.so`` per version and ABI.
3. ``adb forward`` a local port onto the server's abstract socket.
4. Connect twice: the first connection is video, the second is control. Order is
   fixed by the server, not negotiated.
5. Consume the one dummy byte the server sends to signal readiness.

Frames are fanned out to subscribers without decoding. Each subscriber gets a
bounded queue and, on joining, the retained parameter sets plus the newest
keyframe -- without those a mid-stream viewer sees a permanently black canvas
rather than a brief glitch.

Rotation is read on demand rather than polled: the display size changing is the
signal that a rotation happened, so one ``dumpsys`` query is issued at that
moment instead of once a second forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from pixelforge.adb.client import AdbClient, AdbError
from pixelforge.device.scrcpy.control import ControlMessage
from pixelforge.device.scrcpy.video import Codec, VideoPacket, VideoStreamParser
from pixelforge.geometry.mapper import Rotation, Size

logger = logging.getLogger(__name__)

__all__ = ["ScrcpyConfig", "ScrcpySession", "ScrcpyStartupError"]

REMOTE_JAR = "/data/local/tmp/pixelforge-scrcpy-server.jar"
SOCKET_NAME = "scrcpy"
_ROTATION_RE = re.compile(r"\brot(?:ation)?=(\d)")
_ORIENTATION_RE = re.compile(r"SurfaceOrientation:\s*(\d)")

# One frame of 1080p H.264 is tens of KB; 120 frames is a couple of seconds of
# slack for a subscriber that briefly stalls, and a hard ceiling on memory.
SUBSCRIBER_QUEUE_SIZE = 120
_READ_CHUNK = 65536


class ScrcpyStartupError(RuntimeError):
    """The server could not be started or did not complete its handshake."""


@dataclass(frozen=True, slots=True)
class ScrcpyConfig:
    """Server options.

    ``scrcpy_version`` must match the jar: the server validates it and exits if
    it disagrees, which is the friendliest failure available here -- the
    alternative is a stream that decodes to garbage.
    """

    scrcpy_version: str = "3.1"
    jar_path: Path = Path("vendor/scrcpy-server.jar")
    max_size: int = 0  # 0 = native resolution
    video_codec: str = "h264"
    video_bit_rate: int = 8_000_000
    max_fps: int = 0  # 0 = unlimited
    stay_awake: bool = True
    power_off_on_close: bool = False
    connect_timeout_s: float = 15.0

    def server_args(self) -> list[str]:
        return [
            f"log_level=info",
            f"video=true",
            f"audio=false",
            f"control=true",
            f"max_size={self.max_size}",
            f"video_codec={self.video_codec}",
            f"video_bit_rate={self.video_bit_rate}",
            f"max_fps={self.max_fps}",
            f"stay_awake={str(self.stay_awake).lower()}",
            f"power_off_on_close={str(self.power_off_on_close).lower()}",
            "tunnel_forward=true",
            "send_device_meta=true",
            "send_frame_meta=true",
            "send_codec_meta=true",
            "send_dummy_byte=true",
            "cleanup=true",
        ]


@dataclass
class _Subscriber:
    queue: asyncio.Queue[VideoPacket]
    dropped: int = 0


@dataclass
class ScrcpyState:
    """Everything the UI needs to know about a live session."""

    device_name: str | None = None
    codec: Codec | None = None
    frame: Size | None = None
    display: Size | None = None
    rotation: Rotation = Rotation.R0
    frames_relayed: int = 0
    subscribers: int = 0
    started: bool = False
    last_error: str | None = None


class ScrcpySession:
    """A running scrcpy server plus its video and control sockets."""

    def __init__(
        self,
        adb: AdbClient,
        serial: str,
        *,
        config: ScrcpyConfig | None = None,
        on_rotation: "asyncio.Queue[Rotation] | None" = None,
    ) -> None:
        self._adb = adb
        self._serial = serial
        self._config = config or ScrcpyConfig()
        self._on_rotation = on_rotation

        self._process: asyncio.subprocess.Process | None = None
        self._video: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._control: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._parser = VideoStreamParser()
        self._subscribers: set[_Subscriber] = set()
        self._relay_task: asyncio.Task[None] | None = None
        self._forward_port: str | None = None
        self._control_lock = asyncio.Lock()
        self.state = ScrcpyState()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self.state.started:
            return
        jar = self._config.jar_path
        if not jar.is_file():
            raise ScrcpyStartupError(
                f"scrcpy server jar not found at {jar}. Download the jar matching "
                f"scrcpy {self._config.scrcpy_version} into vendor/ -- see "
                "vendor/README.md."
            )
        try:
            await self._adb.push(self._serial, jar, REMOTE_JAR)
            await self._launch()
            self._forward_port = await self._adb.forward(
                self._serial, "tcp:0", f"localabstract:{SOCKET_NAME}"
            )
            await self._connect_sockets()
        except (AdbError, OSError) as exc:
            await self.stop()
            raise ScrcpyStartupError(f"could not start scrcpy on {self._serial}: {exc}") from exc

        self.state.started = True
        self._relay_task = asyncio.create_task(
            self._relay_loop(), name=f"pixelforge-scrcpy-{self._serial}"
        )
        await self._await_metadata()

    async def _launch(self) -> None:
        argv = self._adb.build_argv(
            [
                "shell",
                f"CLASSPATH={REMOTE_JAR}",
                "app_process",
                "/",
                "com.genymobile.scrcpy.Server",
                self._config.scrcpy_version,
                *self._config.server_args(),
            ],
            serial=self._serial,
        )
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._adb.env,
        )
        asyncio.create_task(  # noqa: RUF006 - lifetime is the session's
            self._drain_server_log(), name=f"pixelforge-scrcpy-log-{self._serial}"
        )

    async def _drain_server_log(self) -> None:
        """Surface the server's own log.

        Not optional plumbing: when the server refuses to start, its reason
        ('Could not find any ADB device', a version mismatch, an encoder the
        device lacks) is only ever printed here.
        """
        process = self._process
        if process is None or process.stdout is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            async for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if "ERROR" in line or "Exception" in line:
                    self.state.last_error = line
                    logger.error("scrcpy[%s]: %s", self._serial, line)
                else:
                    logger.debug("scrcpy[%s]: %s", self._serial, line)

    async def _connect_sockets(self) -> None:
        """Open the video then control connections, in the server's fixed order."""
        port = int(self._forward_port or 0)
        deadline = asyncio.get_running_loop().time() + self._config.connect_timeout_s

        async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            last: Exception | None = None
            while asyncio.get_running_loop().time() < deadline:
                try:
                    # The forward lives on the adb server's host, not ours.
                    return await asyncio.open_connection(self._adb.server_host, port)
                except OSError as exc:
                    # The forward exists before the server binds, so a refusal
                    # here is normal for the first few hundred milliseconds.
                    last = exc
                    await asyncio.sleep(0.1)
            raise ScrcpyStartupError(
                f"scrcpy did not accept a connection within "
                f"{self._config.connect_timeout_s}s"
                + (f": {last}" if last else "")
                + (f" (server said: {self.state.last_error})" if self.state.last_error else "")
            )

        self._video = await connect()
        # send_dummy_byte: one byte on the first socket means "server is up".
        await self._video[0].readexactly(1)
        self._control = await connect()

    async def _await_metadata(self) -> None:
        """Block until the codec header has been parsed, so callers get a size."""
        deadline = asyncio.get_running_loop().time() + self._config.connect_timeout_s
        while asyncio.get_running_loop().time() < deadline:
            if self._parser.ready:
                return
            await asyncio.sleep(0.05)
        raise ScrcpyStartupError("scrcpy sent no codec metadata")

    async def stop(self) -> None:
        self.state.started = False
        if self._relay_task is not None:
            self._relay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._relay_task
            self._relay_task = None
        for pair in (self._video, self._control):
            if pair is not None:
                pair[1].close()
                with contextlib.suppress(Exception):
                    await pair[1].wait_closed()
        self._video = self._control = None
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._process.wait(), timeout=5)
            self._process = None
        if self._forward_port is not None:
            with contextlib.suppress(AdbError):
                await self._adb.forward_remove(self._serial, f"tcp:{self._forward_port}")
            self._forward_port = None

    # ----------------------------------------------------------------- video

    async def _relay_loop(self) -> None:
        assert self._video is not None
        reader = self._video[0]
        try:
            while True:
                chunk = await reader.read(_READ_CHUNK)
                if not chunk:
                    logger.info("scrcpy video stream closed for %s", self._serial)
                    return
                for packet in self._parser.feed(chunk):
                    self._observe(packet)
                    self._fan_out(packet)
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError) as exc:
            self.state.last_error = str(exc)
            logger.warning("scrcpy relay failed for %s: %s", self._serial, exc)

    def _observe(self, packet: VideoPacket) -> None:
        self.state.frames_relayed += 1
        self.state.device_name = self._parser.device_name
        self.state.codec = self._parser.codec
        size = self._parser.size
        if size is None:
            return
        frame = Size(*size)
        if self.state.frame != frame:
            # Dimensions changing *is* the rotation signal. Query once here,
            # rather than polling dumpsys on a timer forever.
            self.state.frame = frame
            asyncio.create_task(  # noqa: RUF006
                self._refresh_rotation(), name=f"pixelforge-rot-{self._serial}"
            )

    async def _refresh_rotation(self) -> None:
        try:
            out = await self._adb.shell(self._serial, ["dumpsys", "input"], timeout=8)
            match = _ORIENTATION_RE.search(out) or _ROTATION_RE.search(out)
            if match is None:
                out = await self._adb.shell(
                    self._serial, ["dumpsys", "window", "displays"], timeout=8
                )
                match = _ROTATION_RE.search(out)
            if match is not None:
                rotation = Rotation.parse(int(match.group(1)))
                self.state.rotation = rotation
                if self._on_rotation is not None:
                    with contextlib.suppress(asyncio.QueueFull):
                        self._on_rotation.put_nowait(rotation)
        except (AdbError, ValueError) as exc:
            logger.debug("rotation probe failed for %s: %s", self._serial, exc)

    def _fan_out(self, packet: VideoPacket) -> None:
        for subscriber in self._subscribers:
            if subscriber.queue.full():
                # Drop the oldest. Blocking here would stall the socket read and
                # back-pressure every other viewer because of one slow client.
                with contextlib.suppress(asyncio.QueueEmpty):
                    subscriber.queue.get_nowait()
                subscriber.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                subscriber.queue.put_nowait(packet)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[VideoPacket]]:
        """Stream packets, starting with whatever a fresh decoder needs."""
        subscriber = _Subscriber(queue=asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE))
        for packet in self._parser.bootstrap_packets():
            subscriber.queue.put_nowait(packet)
        self._subscribers.add(subscriber)
        self.state.subscribers = len(self._subscribers)

        async def drain() -> AsyncIterator[VideoPacket]:
            while True:
                yield await subscriber.queue.get()

        try:
            yield drain()
        finally:
            self._subscribers.discard(subscriber)
            self.state.subscribers = len(self._subscribers)
            if subscriber.dropped:
                logger.info(
                    "subscriber on %s dropped %d frames", self._serial, subscriber.dropped
                )

    # --------------------------------------------------------------- control

    async def send(self, *messages: ControlMessage) -> None:
        """Write control messages.

        Serialised behind a lock: the protocol has no framing on this socket, so
        two coroutines writing at once would interleave bytes and produce a
        message the device silently misparses.
        """
        if self._control is None:
            raise RuntimeError("scrcpy control channel is not connected")
        writer = self._control[1]
        async with self._control_lock:
            for message in messages:
                writer.write(message.payload)
            await writer.drain()
