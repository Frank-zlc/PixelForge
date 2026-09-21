"""Owns the live :class:`DeviceSession` objects.

One session per device, created when someone takes a lease and torn down when the
lease ends or the device disappears. Sessions hold sockets, forwards and a server
process on the phone, so leaving them behind leaks all three.

This is also the concrete reason the backend runs single-process: these objects
cannot be reached from another worker, so a request that landed elsewhere would
report the device as having no session at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from pixelforge.adb.client import AdbClient
from pixelforge.device.capture import CaptureMode
from pixelforge.device.scrcpy.session import ScrcpyConfig
from pixelforge.device.session import DeviceSession, SessionStatus
from pixelforge.locators.base import LocatorChain
from pixelforge.locators.strategies import (
    A11yLocator,
    CoordLocator,
    OcrLocator,
    TemplateLocator,
)
from pixelforge.script.model import Strategy
from pixelforge.vision.ocr import TesseractOcr

logger = logging.getLogger(__name__)

__all__ = ["SessionManager"]


class SessionManager:
    def __init__(
        self,
        adb: AdbClient,
        *,
        scrcpy_config: ScrcpyConfig | None = None,
        capture_mode: CaptureMode = CaptureMode.PNG,
    ) -> None:
        self._adb = adb
        self._scrcpy_config = scrcpy_config
        self._capture_mode = capture_mode
        self._sessions: dict[str, DeviceSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ocr = TesseractOcr()

    @property
    def ocr_available(self) -> bool:
        return self._ocr.available

    def chain(self) -> LocatorChain:
        return LocatorChain(
            {
                Strategy.A11Y: A11yLocator(),
                Strategy.TEMPLATE: TemplateLocator(),
                Strategy.OCR: OcrLocator(self._ocr),
                Strategy.COORD: CoordLocator(),
            }
        )

    def get(self, serial: str) -> DeviceSession | None:
        return self._sessions.get(serial)

    def require(self, serial: str) -> DeviceSession:
        session = self._sessions.get(serial)
        if session is None:
            raise KeyError(
                f"no open session for {serial}; acquire a device session first"
            )
        return session

    async def open(
        self,
        serial: str,
        props,
        *,
        adb_serial: str | None = None,
        templates_dir: Path | None = None,
    ) -> tuple[DeviceSession, SessionStatus]:
        """Open a session, or return the existing one.

        Serialised per device: two concurrent opens would each push the jar and
        start a server, and the second would find the abstract socket taken.
        """
        lock = self._locks.setdefault(serial, asyncio.Lock())
        async with lock:
            existing = self._sessions.get(serial)
            if existing is not None:
                return existing, existing.status()
            session = DeviceSession(
                self._adb,
                adb_serial or serial,
                props,
                scrcpy_config=self._scrcpy_config,
                capture_mode=self._capture_mode,
                templates_dir=templates_dir,
            )
            status = await session.start()
            self._sessions[serial] = session
            logger.info(
                "session open for %s (video=%s control=%s a11y=%s)",
                serial, status.video, status.control, status.a11y,
            )
            return session, status

    async def close(self, serial: str) -> None:
        lock = self._locks.setdefault(serial, asyncio.Lock())
        async with lock:
            session = self._sessions.pop(serial, None)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.stop()
            logger.info("session closed for %s", serial)

    async def close_all(self) -> None:
        for serial in list(self._sessions):
            await self.close(serial)

    def statuses(self) -> dict[str, SessionStatus]:
        return {serial: session.status() for serial, session in self._sessions.items()}
