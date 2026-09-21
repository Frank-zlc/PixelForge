"""ADB control remains usable when the optional scrcpy server is absent."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pixelforge.device.session import DeviceSession
from pixelforge.geometry.mapper import CoordinateMapper, Rect, Rotation, Size


def fallback_session() -> tuple[DeviceSession, AsyncMock]:
    session = object.__new__(DeviceSession)
    shell = AsyncMock(return_value="")
    session.serial = "ABC123"
    session._adb = SimpleNamespace(shell=shell)
    session.scrcpy = SimpleNamespace(state=SimpleNamespace(started=False))
    return session, shell


async def test_tap_falls_back_to_adb_input() -> None:
    session, shell = fallback_session()

    await session.tap(Rect(x=99, y=199, width=2, height=2))

    shell.assert_awaited_once_with("ABC123", ["input", "tap", "100", "200"])


async def test_swipe_falls_back_to_adb_input() -> None:
    session, shell = fallback_session()

    await session.swipe(
        Rect(x=9, y=19, width=2, height=2),
        Rect(x=109, y=219, width=2, height=2),
        350,
    )

    shell.assert_awaited_once_with(
        "ABC123",
        ["input", "swipe", "10", "20", "110", "220", "350"],
        timeout=15.0,
    )


async def test_key_falls_back_to_adb_input() -> None:
    session, shell = fallback_session()

    await session.key(4)

    shell.assert_awaited_once_with("ABC123", ["input", "keyevent", "4"])


async def test_ascii_text_falls_back_to_adb_input() -> None:
    session, shell = fallback_session()

    await session.input_text("hello world@example.com")

    shell.assert_awaited_once_with("ABC123", ["input", "text", "hello%sworld@example.com"])


@pytest.mark.parametrize("text", ["中文", "unsafe&command", "quote'text"])
async def test_unsafe_or_non_ascii_text_needs_scrcpy(text: str) -> None:
    session, shell = fallback_session()

    with pytest.raises(RuntimeError, match="scrcpy"):
        await session.input_text(text)

    shell.assert_not_awaited()


def test_screenshot_rotation_returns_to_natural_orientation() -> None:
    session, _ = fallback_session()
    session._mapper = CoordinateMapper(device=Size(1080, 2400), frame=Size(1080, 2400))
    session.scrcpy.state.frame = None

    session._sync_display(Size(2400, 1080))
    assert session.mapper.rotation is Rotation.R90

    session._sync_display(Size(1080, 2400))
    assert session.mapper.rotation is Rotation.R0
