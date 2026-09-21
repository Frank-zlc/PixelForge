"""Capture, cropping and inspection.

The crop endpoint is the feature that replaces 'screenshot, measure pixels in an
image editor, divide by the resolution'. It takes a rectangle in any coordinate
space, re-captures losslessly, crops, stores the template, and returns the
normalised coordinates alongside it -- so the numbers that go into a step are
produced by the tool rather than typed by hand.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from pixelforge.api.deps import (
    LeasesDep,
    SessionsDep,
    StoreDep,
    require_lease,
    require_session,
)
from pixelforge.geometry.mapper import Point, Rect, Size, Space

router = APIRouter(prefix="/api/devices", tags=["capture"])


class CaptureRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    fresh: bool = True


class CropRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    space: Space = Space.DEVICE
    # For CSS-space selections the browser must say how big the canvas was, or
    # there is no way to undo the letterboxing.
    element_width: int | None = Field(default=None, gt=0)
    element_height: int | None = Field(default=None, gt=0)


class CropResponse(BaseModel):
    name: str
    file: str
    device_rect: list[int]
    norm_rect: list[float]
    center_norm: list[float]
    width: int
    height: int
    warning: str | None = None


class PointRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    x: float
    y: float
    space: Space = Space.DEVICE
    element_width: int | None = Field(default=None, gt=0)
    element_height: int | None = Field(default=None, gt=0)


@router.post("/{serial}/capture")
async def capture(
    serial: str, body: CaptureRequest, leases: LeasesDep, sessions: SessionsDep
) -> Response:
    """Lossless PNG. Returned as image bytes so the browser can show it directly."""
    require_lease(leases, serial, body.token)
    session = require_session(sessions, serial)
    shot = await session.capture(fresh=body.fresh)
    payload = shot.data if shot.is_png else shot.save(
        session.templates_dir / "_tmp.png"
    ).read_bytes()
    return Response(
        content=payload,
        media_type="image/png",
        headers={
            "X-PixelForge-Width": str(shot.size.width),
            "X-PixelForge-Height": str(shot.size.height),
            "X-PixelForge-Lossless": "1",
        },
    )


@router.post("/{serial}/crop", response_model=CropResponse)
async def crop(
    serial: str,
    body: CropRequest,
    leases: LeasesDep,
    sessions: SessionsDep,
    store: StoreDep,
) -> CropResponse:
    require_lease(leases, serial, body.token)
    session = require_session(sessions, serial)

    mapper = session.mapper
    if body.space is Space.CSS:
        if body.element_width is None or body.element_height is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "a CSS-space selection needs element_width and element_height; "
                "without them the letterbox offset cannot be removed",
            )
        mapper = mapper.with_(
            element=Size(body.element_width, body.element_height)
        )

    selection = Rect(
        x=int(body.x), y=int(body.y), width=int(body.width), height=int(body.height)
    )
    device_rect = (
        selection
        if body.space is Space.DEVICE
        else mapper.convert_rect(selection, body.space, Space.DEVICE)
    )

    try:
        pixels = await session.crop(device_rect, Space.DEVICE)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    import cv2

    ok, encoded = cv2.imencode(".png", cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR))
    if not ok:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "could not encode the crop")
    path = store.save_template(body.project_id, body.name, encoded.tobytes())

    display = session.display
    warning = None
    # A near-uniform crop cannot identify a location, so say so at capture time
    # rather than letting it fail mysteriously at replay.
    import numpy as np

    if float(np.std(pixels)) < 1.0:
        warning = (
            "this crop is a flat colour, so it cannot identify a unique location -- "
            "include an edge, icon or text"
        )

    return CropResponse(
        name=body.name,
        file=path.name,
        device_rect=[device_rect.x, device_rect.y, device_rect.width, device_rect.height],
        norm_rect=[
            round(device_rect.x / display.width, 6),
            round(device_rect.y / display.height, 6),
            round(device_rect.right / display.width, 6),
            round(device_rect.bottom / display.height, 6),
        ],
        center_norm=[
            round(device_rect.center.x / display.width, 6),
            round(device_rect.center.y / display.height, 6),
        ],
        width=int(pixels.shape[1]),
        height=int(pixels.shape[0]),
        warning=warning,
    )


@router.post("/{serial}/point")
async def describe_point(
    serial: str, body: PointRequest, leases: LeasesDep, sessions: SessionsDep
) -> dict[str, object]:
    """Everything known about one point: all four spaces, plus the control under it.

    This is the coordinate readout and the click-to-identify inspector in one call,
    because the UI shows them together and two round trips would make the crosshair
    lag the cursor.
    """
    require_lease(leases, serial, body.token)
    session = require_session(sessions, serial)
    mapper = session.mapper
    if body.space is Space.CSS:
        if body.element_width is None or body.element_height is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "a CSS-space point needs element_width and element_height",
            )
        mapper = mapper.with_(element=Size(body.element_width, body.element_height))

    point = Point(body.x, body.y)
    spaces = mapper.describe(point, body.space)
    node = None
    if session.a11y_available:
        try:
            device_point = mapper.convert(point, body.space, Space.DEVICE, strict=False)
            found = await session.node_at_point(device_point, Space.DEVICE)
            if found is not None:
                node = {
                    "class": found.class_name,
                    "resource_id": found.resource_id,
                    "text": found.text,
                    "content_desc": found.content_desc,
                    "clickable": found.clickable,
                    "bounds": [
                        found.bounds.x, found.bounds.y,
                        found.bounds.width, found.bounds.height,
                    ],
                    "label": found.label,
                }
        except Exception:  # noqa: BLE001 - the readout must still work
            node = None
    return {"spaces": spaces, "node": node, "a11y_available": session.a11y_available}


@router.post("/{serial}/hierarchy")
async def hierarchy(
    serial: str, body: CaptureRequest, leases: LeasesDep, sessions: SessionsDep
) -> dict[str, object]:
    require_lease(leases, serial, body.token)
    session = require_session(sessions, serial)
    if not session.a11y_available:
        return {
            "available": False,
            "reason": session.uiautomator.last_error
            or "the uiautomator2 server is not running",
            "nodes": [],
        }
    nodes = await session.hierarchy()
    return {
        "available": True,
        "nodes": [
            {
                "class": node.class_name,
                "resource_id": node.resource_id,
                "text": node.text,
                "content_desc": node.content_desc,
                "clickable": node.clickable,
                "depth": node.depth,
                "label": node.label,
                "bounds": [
                    node.bounds.x, node.bounds.y, node.bounds.width, node.bounds.height
                ],
            }
            for node in nodes
        ],
    }


@router.post("/{serial}/diagnose")
async def diagnose(
    serial: str, body: CaptureRequest, leases: LeasesDep, sessions: SessionsDep
) -> dict[str, object]:
    """Explain a black screen. Usually FLAG_SECURE; say so in five seconds."""
    require_lease(leases, serial, body.token)
    session = require_session(sessions, serial)
    probe = await session.probe_secure()
    return {
        "cause": probe.cause.value,
        "capture_usable": probe.capture_usable,
        "black_ratio": round(probe.black_ratio, 4),
        "node_count": probe.node_count,
        "message": probe.message(),
    }
