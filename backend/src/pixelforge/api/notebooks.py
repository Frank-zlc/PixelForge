"""Image notebook draft APIs (N2 persistence slice)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from pixelforge.image_lab.notebooks import NotebookConflictError, NotebookStore
from pixelforge.image_lab.runs import NotebookRunner, RunConflictError

router = APIRouter(prefix="/api/image-lab/notebooks", tags=["image-lab"])


def _store(request: Request) -> NotebookStore:
    return NotebookStore(request.app.state.image_lab)


def _runner(request: Request) -> NotebookRunner:
    return request.app.state.notebook_runner


@router.post("", status_code=status.HTTP_201_CREATED)
def create(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return _store(request).create(body.get("title"), body.get("description", ""))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("")
def list_notebooks(request: Request, limit: int = 100) -> dict[str, object]:
    try:
        return {"items": _store(request).list(limit)}
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/{notebook_id}")
def detail(notebook_id: str, request: Request) -> dict[str, Any]:
    item = _store(request).get(notebook_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown notebook")
    return item


@router.put("/{notebook_id}")
def save(notebook_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return _store(request).save(notebook_id, body)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except NotebookConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/{notebook_id}/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    notebook_id: str, request: Request, background: BackgroundTasks,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = body or {}
    runner = _runner(request)
    run_id = body.get("run_id") or uuid.uuid4().hex
    try:
        # Check for active run before accepting. The actual execution runs in a
        # background thread because image operators are CPU/IO bound.
        await run_in_threadpool(
            runner.run_sync, notebook_id,
            idempotency_key=body.get("idempotency_key"),
            run_id=run_id,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except RunConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    # BackgroundTasks is used to keep FastAPI's task tracking in scope, though
    # the runner completes synchronously within the threadpool call above.
    return {"notebook_id": notebook_id, "run_id": run_id, "status": "accepted"}


@router.get("/{notebook_id}/runs")
def list_runs(notebook_id: str, request: Request) -> dict[str, Any]:
    return {"items": _runner(request).list_runs(notebook_id)}


@router.get("/{notebook_id}/runs/{run_id}")
def get_run(notebook_id: str, run_id: str, request: Request) -> dict[str, Any]:
    item = _runner(request).get_run(run_id)
    if item is None or item["notebook_id"] != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown run")
    return item


@router.post("/{notebook_id}/runs/{run_id}/cancel")
def cancel_run(notebook_id: str, run_id: str, request: Request) -> dict[str, Any]:
    try:
        item = _runner(request).cancel(run_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if item["notebook_id"] != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown run")
    return item
