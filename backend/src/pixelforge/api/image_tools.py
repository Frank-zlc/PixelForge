"""Offline image workbench: catalog, examples and pixel processing."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from pixelforge.geometry.mapper import Rect
from pixelforge.vision.tool_catalog import (
    IMPLEMENTATIONS,
    ImageToolCatalog,
    demo_image,
    process_image,
)

router = APIRouter(prefix="/api/image-tools", tags=["image-tools"])
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 30_000_000


def _catalog(request: Request) -> ImageToolCatalog:
    return request.app.state.image_tools


def _encode(image) -> bytes:
    import cv2

    ok, encoded = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    if not ok:
        raise ValueError("could not encode processed image")
    return bytes(encoded)


def _decode(payload: bytes):
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("file is not a supported image")
    if image.shape[0] * image.shape[1] > MAX_PIXELS:
        raise ValueError("image is too large (maximum 30 megapixels)")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def _process(payload: bytes, tool_id: str, roi: Rect | None, params: dict[str, str]) -> bytes:
    image = _decode(payload)
    return _encode(process_image(tool_id, image, roi=roi, params=params))


@router.get("")
def list_tools(request: Request) -> list[dict[str, object]]:
    return _catalog(request).list()


@router.get("/demo-source")
def demo_source() -> Response:
    return Response(_encode(demo_image()), media_type="image/png")


@router.get("/{tool_id}/demo")
def tool_demo(tool_id: str, request: Request) -> Response:
    tool = _catalog(request).get(tool_id)
    if tool is None or tool["availability"] != "available" or tool_id not in IMPLEMENTATIONS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tool demonstration is unavailable")
    try:
        roi = Rect(18, 20, 142, 65) if tool_id == "crop" else None
        image = process_image(tool_id, demo_image(), roi=roi)
        return Response(_encode(image), media_type="image/png")
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/{tool_id}/run")
async def run_tool(
    tool_id: str,
    request: Request,
    x: int | None = Query(default=None),
    y: int | None = Query(default=None),
    width: int | None = Query(default=None),
    height: int | None = Query(default=None),
) -> Response:
    tool = _catalog(request).get(tool_id)
    if tool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image tool")
    if tool["availability"] != "available" or tool_id not in IMPLEMENTATIONS:
        raise HTTPException(status.HTTP_409_CONFLICT, "tool is not available for offline images")
    coordinates = (x, y, width, height)
    if any(value is not None for value in coordinates):
        if x is None or y is None or width is None or height is None or width <= 0 or height <= 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "complete positive selection required"
            )
        roi = Rect(x, y, width, height)
    else:
        roi = None
    if tool_id == "crop" and roi is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "select a region to crop")
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_IMAGE_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "image exceeds 20 MB")
    if not chunks:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "image is empty")
    params = {
        key: value
        for key, value in request.query_params.items()
        if key not in {"x", "y", "width", "height"}
    }
    try:
        result = await run_in_threadpool(_process, bytes(chunks), tool_id, roi, params)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return Response(result, media_type="image/png")
