"""Script runs and live debugger control.

A run is a background task plus a :class:`Debugger` the client can poke while it
is in flight. That is what makes 'pause, take the device by hand, continue'
possible over HTTP: the pause blocks inside the runner, the device is untouched
by the engine meanwhile, and a resume unblocks it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from pixelforge.api.deps import (
    BusDep,
    LeasesDep,
    SessionsDep,
    StoreDep,
    require_lease,
    require_session,
)
from pixelforge.script.debugger import DebugMode, Debugger
from pixelforge.script.executor import RunResult, RunStatus, ScriptRunner

router = APIRouter(prefix="/api/runs", tags=["runs"])


@dataclass
class ActiveRun:
    run_id: str
    serial: str
    project_id: str
    script_id: str
    debugger: Debugger
    task: asyncio.Task[RunResult]
    result: RunResult | None = None

    def state(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "serial": self.serial,
            "project_id": self.project_id,
            "script_id": self.script_id,
            "done": self.task.done(),
            "paused": self.debugger.paused,
            "paused_at": self.debugger.paused_at,
            "mode": self.debugger.mode.value,
            "breakpoints": sorted(self.debugger.breakpoints),
            "result": self.result.as_dict() if self.result else None,
        }


# Runs live in process alongside the device sessions they drive -- the same reason
# the server must stay single-worker.
_RUNS: dict[str, ActiveRun] = {}


class StartRunRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    serial: str = Field(min_length=1, max_length=160)
    project_id: str = Field(min_length=1, max_length=64)
    script_id: str = Field(min_length=1, max_length=64)
    mode: DebugMode = DebugMode.RUN
    breakpoints: list[str] = Field(default_factory=list, max_length=500)
    run_only: list[str] = Field(default_factory=list, max_length=500)
    run_to: str | None = None
    start_from: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    body: StartRunRequest,
    leases: LeasesDep,
    sessions: SessionsDep,
    store: StoreDep,
    bus: BusDep,
) -> dict[str, object]:
    require_lease(leases, body.serial, body.token)
    session = require_session(sessions, body.serial)
    try:
        project = store.get(body.project_id)
        script = store.get_script(body.project_id, body.script_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    debugger = Debugger(
        mode=body.mode,
        breakpoints=set(body.breakpoints),
        run_only=set(body.run_only),
        run_to=body.run_to,
        start_from=body.start_from,
    )
    runner = ScriptRunner(
        session,
        sessions.chain(),
        bus,
        templates_dir=store.templates_dir(project.id),
        ocr_language=project.ocr_language,
    )
    run_id = uuid.uuid4().hex[:12]
    task = asyncio.create_task(
        runner.run(
            script,
            debugger=debugger,
            variables=project.resolve_variables(body.variables),
            run_id=run_id,
        ),
        name=f"pixelforge-run-{run_id}",
    )
    active = ActiveRun(
        run_id=run_id,
        serial=body.serial,
        project_id=body.project_id,
        script_id=body.script_id,
        debugger=debugger,
        task=task,
    )
    _RUNS[run_id] = active

    def _store_result(finished: asyncio.Task[RunResult]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            active.result = finished.result()

    task.add_done_callback(_store_result)
    return active.state()


def _run(run_id: str) -> ActiveRun:
    active = _RUNS.get(run_id)
    if active is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown run {run_id}")
    return active


@router.get("")
async def list_runs() -> list[dict[str, object]]:
    return [active.state() for active in _RUNS.values()]


@router.get("/{run_id}")
async def get_run(run_id: str) -> dict[str, object]:
    return _run(run_id).state()


@router.post("/{run_id}/pause")
async def pause_run(run_id: str) -> dict[str, object]:
    active = _run(run_id)
    active.debugger.pause(active.debugger.paused_at)
    return active.state()


@router.post("/{run_id}/resume")
async def resume_run(run_id: str) -> dict[str, object]:
    """Continue after a breakpoint -- typically after driving the device by hand."""
    active = _run(run_id)
    active.debugger.resume()
    return active.state()


@router.post("/{run_id}/stop")
async def stop_run(run_id: str) -> dict[str, object]:
    active = _run(run_id)
    active.debugger.stop()
    return active.state()


@router.post("/{run_id}/breakpoints/{step_id}")
async def toggle_breakpoint(run_id: str, step_id: str) -> dict[str, object]:
    active = _run(run_id)
    enabled = active.debugger.toggle_breakpoint(step_id)
    return {**active.state(), "enabled": enabled}


@router.get("/{run_id}/timeline")
async def run_timeline(run_id: str, bus: BusDep) -> dict[str, object]:
    active = _run(run_id)
    return {
        "run_id": run_id,
        "state": active.state(),
        "events": [
            event.as_dict(origin=bus.origin) for event in bus.history(run_id=run_id)
        ],
    }


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_run(run_id: str) -> None:
    active = _RUNS.pop(run_id, None)
    if active is not None and not active.task.done():
        active.debugger.stop()
        active.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await active.task


async def shutdown_runs() -> None:
    for run_id in list(_RUNS):
        await forget_run(run_id)
