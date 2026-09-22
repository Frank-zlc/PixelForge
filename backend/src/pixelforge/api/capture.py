"""Capture, cropping and inspection.

The crop endpoint is the feature that replaces 'screenshot, measure pixels in an
image editor, divide by the resolution'. It takes a rectangle in any coordinate
space, re-captures losslessly, crops, stores the template, and returns the
normalised coordinates alongside it -- so the numbers that go into a step are
produced by the tool rather than typed by hand.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from pixelforge.api.deps import (
    LeasesDep,
    SessionsDep,
    SettingsDep,
    StoreDep,
    require_lease,
    require_session,
)
from pixelforge.geometry.mapper import Point, Rect, Size, Space

router = APIRouter(prefix="/api/devices", tags=["capture"])


def _encode_png(pixels: object) -> bytes:
    """Encode an RGB array to PNG bytes without touching the filesystem."""
    import cv2

    ok, encoded = cv2.imencode(".png", cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR))
    if not ok:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "could not encode the capture"
        )
    return bytes(encoded.tobytes())


class CaptureRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    fresh: bool = True


class CropRegion(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    space: Space = Space.DEVICE
    # For CSS-space selections the browser must say how big the canvas was, or
    # there is no way to undo the letterboxing.
    element_width: int | None = Field(default=None, gt=0)
    element_height: int | None = Field(default=None, gt=0)


class CropRequest(CropRegion):
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    folder: str | None = Field(default=None, max_length=240)
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        value = value.strip()
        if value.lower().endswith(".png"):
            value = value[:-4].rstrip()
        forbidden = '<>:"/\\|?*'
        if (
            not value
            or value in {".", ".."}
            or value.endswith((".", " "))
            or any(char in forbidden or ord(char) < 32 for char in value)
        ):
            raise ValueError("name contains characters that are unsafe in a file name")
        return value


class CropResponse(BaseModel):
    name: str
    file: str
    directory: str
    destination: str
    device_rect: list[int]
    norm_rect: list[float]
    center_norm: list[float]
    width: int
    height: int
    warning: str | None = None


def _asset_output_path(root: Path, folder: str | None, name: str) -> Path:
    """Resolve a browser-supplied subdirectory without permitting path escape."""
    relative = PurePosixPath(folder or "")
    parts = relative.parts
    if relative.is_absolute() or any(
        part in {"", ".", ".."}
        or not all(char.isalnum() or char in "-_" for char in part)
        for part in parts
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "folder must be a relative path using letters, digits, '-' and '_'",
        )
    root = root.resolve()
    directory = root.joinpath(*parts).resolve()
    if directory != root and root not in directory.parents:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "folder escapes asset root")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{name}.png"


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
    # RAW captures are re-encoded in memory. The previous version wrote a
    # "_tmp.png" into the project's template directory, which both littered the
    # template library and crashed outright when no project was bound
    # (templates_dir is None -> None / "_tmp.png").
    payload = shot.data if shot.is_png else _encode_png(shot.to_array())
    return Response(
        content=payload,
        media_type="image/png",
        headers={
            "X-PixelForge-Width": str(shot.size.width),
            "X-PixelForge-Height": str(shot.size.height),
            "X-PixelForge-Lossless": "1",
        },
    )


async def _crop_region(session, body: CropRegion):
    mapper = session.mapper
    if body.space is Space.CSS:
        if body.element_width is None or body.element_height is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "a CSS-space selection needs element_width and element_height; "
                "without them the letterbox offset cannot be removed",
            )
        mapper = mapper.with_(element=Size(body.element_width, body.element_height))

    selection = Rect(
        x=int(body.x), y=int(body.y), width=int(body.width), height=int(body.height)
    )
    device_rect = (
        selection
        if body.space is Space.DEVICE
        else mapper.convert_rect(selection, body.space, Space.DEVICE)
    )
    try:
        return await session.crop(device_rect, Space.DEVICE), device_rect
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/{serial}/crop-image")
async def crop_image(
    serial: str,
    body: CropRegion,
    leases: LeasesDep,
    sessions: SessionsDep,
) -> Response:
    """Return a selected lossless PNG for the browser's system directory picker."""
    require_lease(leases, serial, body.token)
    session = require_session(sessions, serial)
    pixels, device_rect = await _crop_region(session, body)
    return Response(
        content=_encode_png(pixels),
        media_type="image/png",
        headers={
            "X-PixelForge-Width": str(device_rect.width),
            "X-PixelForge-Height": str(device_rect.height),
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
    settings: SettingsDep,
) -> CropResponse:
    require_lease(leases, serial, body.token)
    session = require_session(sessions, serial)
    pixels, device_rect = await _crop_region(session, body)
    encoded = _encode_png(pixels)
    if body.project_id:
        try:
            path = store.save_template(body.project_id, body.name, encoded)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        destination = "project_template"
    else:
        path = _asset_output_path(settings.assets_dir, body.folder, body.name)
        path.write_bytes(encoded)
        destination = "asset"

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
        directory=str(path.parent.resolve()),
        destination=destination,
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
