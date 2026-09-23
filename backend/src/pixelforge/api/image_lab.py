"""Offline image catalog and managed-asset APIs."""

from __future__ import annotations

import base64
import json

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from pixelforge.geometry.mapper import Rect
from pixelforge.image_lab.operators import OPERATORS, run_operator
from pixelforge.image_lab.storage import MAX_IMAGE_BYTES, MAX_PIXELS, ImageLabStore

router = APIRouter(prefix="/api/image-lab", tags=["image-lab"])


def _store(request: Request) -> ImageLabStore:
    return request.app.state.image_lab


async def _body(request: Request) -> bytes:
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_IMAGE_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "image exceeds 20 MB")
    if not chunks:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "image is empty")
    return bytes(chunks)


@router.get("/tools")
def tools(request: Request) -> dict[str, object]:
    items = _store(request).list_tools()
    counts = {
        "ready_algorithms": sum(item["catalog_state"] == "ready" for item in items),
        "pending_adapter": sum(item["catalog_state"] == "pending_adapter" for item in items),
        "planned_algorithms": sum(item["catalog_state"] == "planned" for item in items),
        "planned_workflows": sum(item["catalog_state"] == "workflow_planned" for item in items),
    }
    return {"items": items, "counts": counts}


@router.get("/tools/{tool_id}")
def tool_detail(tool_id: str, request: Request) -> dict[str, object]:
    for item in _store(request).list_tools():
        if item["id"] == tool_id:
            return item
    raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image tool")


@router.get("/tools/{tool_id}/examples")
def tool_examples(tool_id: str, request: Request) -> list[dict[str, object]]:
    if not any(item["id"] == tool_id for item in _store(request).list_tools()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image tool")
    return _store(request).list_examples(tool_id)


def _run(
    payload: bytes, tool_id: str, roi: Rect | None, params: dict[str, object]
) -> dict[str, object]:
    try:
        image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise ValueError("unsupported image") from exc
    if image is None:
        raise ValueError("unsupported image")
    if image.shape[0] * image.shape[1] > MAX_PIXELS:
        raise ValueError("image exceeds 30 megapixels")
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    result = run_operator(tool_id, rgb, roi=roi, params=params)
    outputs: dict[str, object] = {}
    for name, pixels in result.images.items():
        encoded_input = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR) if pixels.ndim == 3 else pixels
        ok, encoded = cv2.imencode(".png", encoded_input)
        if not ok:
            raise ValueError(f"could not encode {name}")
        if len(encoded) > 40 * 1024 * 1024:
            raise ValueError(f"output {name} exceeds 40 MB")
        outputs[name] = {
            "type": OPERATORS[tool_id].outputs[name],
            "data_url": "data:image/png;base64," + base64.b64encode(encoded).decode("ascii"),
            "width": int(pixels.shape[1]),
            "height": int(pixels.shape[0]),
        }
    return {
        "tool_id": tool_id,
        "tool_version": OPERATORS[tool_id].version,
        "outputs": outputs,
        "regions": result.regions,
        "points": result.points,
        "metrics": result.metrics,
        "text": result.text,
        "notes": result.notes,
    }


@router.post("/tools/{tool_id}/run")
async def run_tool(
    tool_id: str, request: Request, params: str = Query(default="{}"),
    x: int | None = None, y: int | None = None,
    width: int | None = None, height: int | None = None,
) -> dict[str, object]:
    spec = OPERATORS.get(tool_id)
    if spec is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "tool has no offline adapter")
    current = next(
        (item for item in _store(request).list_tools() if item["id"] == tool_id), None
    )
    if current is None or current["catalog_state"] != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "tool is not ready for offline execution")
    if not isinstance(params, str) or len(params) > 4096:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid parameters")
    try:
        values = json.loads(params)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid parameters") from exc
    if not isinstance(values, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "parameters must be an object")
    coordinates = (x, y, width, height)
    if all(value is None for value in coordinates):
        roi = None
    elif None in coordinates or width is None or height is None or width <= 0 or height <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "complete positive ROI required")
    else:
        assert x is not None and y is not None
        roi = Rect(x, y, width, height)
    payload = await _body(request)
    try:
        return await run_in_threadpool(_run, payload, tool_id, roi, values)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/imports", status_code=status.HTTP_201_CREATED)
async def import_image(
    request: Request, relative_name: str = Query(...), batch_id: str | None = None,
) -> dict[str, object]:
    payload = await _body(request)
    try:
        return await run_in_threadpool(
            _store(request).import_image, payload, relative_name=relative_name, batch_id=batch_id
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/assets")
def list_assets(
    request: Request, batch_id: str | None = None, limit: int = 100
) -> dict[str, object]:
    try:
        items = _store(request).list_assets(batch_id=batch_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"items": items}


@router.get("/assets/{asset_id}")
def asset_detail(asset_id: str, request: Request) -> dict[str, object]:
    item = _store(request).get_asset(asset_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image asset")
    return item


@router.get("/assets/{asset_id}/content")
def asset_content(asset_id: str, request: Request, variant: str = "preview") -> FileResponse:
    if variant not in {"preview", "original", "thumbnail"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid asset variant")
    path = _store(request).content_path(asset_id, variant=variant)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image asset")
    media_type = "application/octet-stream" if variant == "original" else "image/png"
    return FileResponse(path, media_type=media_type)


@router.get("/outputs/{run_id}/{step_run_id}/{port}.png")
def run_output(run_id: str, step_run_id: str, port: str, request: Request) -> FileResponse:
    from pixelforge.image_lab.runs import NotebookRunner

    runner = NotebookRunner(_store(request))
    path = runner.output_path(run_id, step_run_id, port)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown run output")
    return FileResponse(path, media_type="image/png")
