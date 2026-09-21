"""Stream logcat onto the timeline.

Filtered hard by default. An unfiltered logcat is thousands of lines a minute of
system chatter, and burying two useful lines in it is the same as not having them.
The defaults keep warnings and errors plus anything tagged with the app's own
package, which is what actually explains a step failing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from pixelforge.adb.client import AdbClient
from pixelforge.listeners.base import ListenerContext
from pixelforge.timeline.bus import EventKind

logger = logging.getLogger(__name__)

__all__ = ["LogcatListener"]

# threadtime: date time pid tid level tag: message
_LINE_RE = re.compile(
    r"^(?P<ts>\d\d-\d\d\s[\d:.]+)\s+(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
    r"(?P<level>[VDIWEF])\s+(?P<tag>[^:]*):\s?(?P<message>.*)$"
)
_INTERESTING = {"W", "E", "F"}
# ANR and crash markers: the lines that most often explain a stuck flow.
_ALWAYS = re.compile(
    r"ANR in|FATAL EXCEPTION|Force finishing|beginning of crash|"
    r"has died|Application is not responding",
    re.IGNORECASE,
)


class LogcatListener:
    name = "logcat"
    description = "Device log, filtered to warnings, errors, crashes and the app's own tags"

    def __init__(
        self,
        adb: AdbClient,
        *,
        min_level: str = "W",
        include_tags: tuple[str, ...] = (),
        clear_first: bool = True,
    ) -> None:
        if min_level not in "VDIWEF":
            raise ValueError("min_level must be one of V, D, I, W, E or F")
        self._adb = adb
        self._min_level = min_level
        self._include_tags = tuple(include_tags)
        self._clear_first = clear_first
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._context: ListenerContext | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, context: ListenerContext) -> None:
        if self.running:
            return
        self._context = context
        if self._clear_first:
            # Without this the first second of the timeline is filled with
            # whatever accumulated before the run began.
            with contextlib.suppress(Exception):
                await self._adb.run(["logcat", "-c"], serial=context.serial, timeout=10)
        argv = self._adb.build_argv(["logcat", "-v", "threadtime"], serial=context.serial)
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._adb.env,
        )
        self._task = asyncio.create_task(
            self._pump(), name=f"pixelforge-logcat-{context.serial}"
        )

    async def _pump(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        context = self._context
        assert context is not None
        package = context.app_package
        try:
            async for raw in self._process.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                match = _LINE_RE.match(line)
                if match is None:
                    continue
                level = match.group("level")
                tag = match.group("tag").strip()
                message = match.group("message")
                if not self._keep(level, tag, message, package):
                    continue
                context.bus.emit(
                    EventKind.LOG,
                    message=f"[{level}] {tag}: {message}"[:800],
                    serial=context.serial,
                    level=level,
                    tag=tag,
                    pid=int(match.group("pid")),
                    crash=bool(_ALWAYS.search(line)),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("logcat listener stopped: %s", exc)

    def _keep(self, level: str, tag: str, message: str, package: str | None) -> bool:
        if _ALWAYS.search(message):
            return True
        if level in _INTERESTING and self._level_at_least(level):
            return True
        if package and (package in tag or package in message):
            return True
        return any(wanted in tag for wanted in self._include_tags)

    def _level_at_least(self, level: str) -> bool:
        order = "VDIWEF"
        return order.index(level) >= order.index(self._min_level)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._process.wait(), timeout=5)
        self._process = None
