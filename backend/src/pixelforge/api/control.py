"""Direct device control: taps, gestures, keys, text."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from pixelforge.api.deps import LeasesDep, SessionsDep, require_lease, require_session
from pixelforge.geometry.mapper import Point, Rect, Size, Space

router = APIRouter(prefix="/api/devices", tags=["control"])


class _Controlled(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    space: Space = Space.DEVICE
    element_width: int | None = Field(default=None, gt=0)
    element_height: int | None = Field(default=None, gt=0)


class TapRequest(_Controlled):
    x: float
    y: float
    long_press_ms: int | None = Field(default=None, ge=100, le=10_000)


class SwipeRequest(_Controlled):
    x1: float
    y1: float
    x2: float
    y2: float
    duration_ms: int = Field(default=300, ge=50, le=10_000)


class TextRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)


class KeyRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    keycode: int = Field(ge=0, le=0xFFFF)


def _point(session, body: _Controlled, x: float, y: float) -> Rect:
    mapper = session.mapper
    if body.space is Space.CSS:
        if body.element_width is None or body.element_height is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "a CSS-space point needs element_width and element_height",
            )
        mapper = mapper.with_(element=Size(body.element_width, body.element_height))
    try:
        device = mapper.convert(Point(x, y), body.space, Space.DEVICE, strict=True)
    except ValueError as exc:
        # A click on the letterbox is not a click near the edge; refusing it beats
        # inventing an interaction at the border.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return Rect(x=int(device.x) - 1, y=int(device.y) - 1, width=2, height=2)


@router.post("/{serial}/tap", status_code=status.HTTP_204_NO_CONTENT)
async def tap(serial: str, body: TapRequest, leases: LeasesDep, sessions: SessionsDep) -> None:
    require_lease(leases, serial, body.token)
    session = require_session(sessions, serial)
    box = _point(session, body, body.x, body.y)
    if body.long_press_ms:
        await session.long_press(box, body.long_press_ms)
    else:
        await session.tap(box)


@router.post("/{serial}/swipe", status_code=status.HTTP_204_NO_CONTENT)
async def swipe(serial: str, body: SwipeRequest, leases: LeasesDep, sessions: SessionsDep) -> None:
    require_lease(leases, serial, body.token)
    session = require_session(sessions, serial)
    await session.swipe(
        _point(session, body, body.x1, body.y1),
        _point(session, body, body.x2, body.y2),
        body.duration_ms,
    )


@router.post("/{serial}/text", status_code=status.HTTP_204_NO_CONTENT)
async def text(serial: str, body: TextRequest, leases: LeasesDep, sessions: SessionsDep) -> None:
    """UTF-8 text, including CJK -- which `adb shell input text` cannot send."""
    require_lease(leases, serial, body.token)
    try:
        await require_session(sessions, serial).input_text(body.text)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/{serial}/key", status_code=status.HTTP_204_NO_CONTENT)
async def key(serial: str, body: KeyRequest, leases: LeasesDep, sessions: SessionsDep) -> None:
    require_lease(leases, serial, body.token)
    await require_session(sessions, serial).key(body.keycode)
