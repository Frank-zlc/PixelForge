"""Immutable notebook execution, snapshotting, and artifact persistence.

A notebook run locks the current draft revision, resolves inputs to concrete
asset hashes and step_run ids, executes each step sequentially, and persists
named outputs. Step outputs are cached so re-running after a draft change only
recomputes the affected suffix.

Execution is cancelable and time-bounded. The runner stores enough state in
SQLite to resume history after a server restart, but the in-memory cancel token
is lost on restart; interrupted runs are marked ``interrupted`` and may be
retried.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from pixelforge.geometry.mapper import Rect
from pixelforge.image_lab.operators import OPERATORS, run_operator
from pixelforge.image_lab.storage import ImageLabStore

_RUN_TIMEOUT_S = 300.0


class RunCancelledError(Exception):
    pass


class RunConflictError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_hash(notebook_id: str, revision: int, snapshot: dict[str, object]) -> str:
    payload = f"{notebook_id}:{revision}:".encode() + _json(snapshot).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass
class _RunContext:
    run_id: str
    store: ImageLabStore
    cancel_event: threading.Event
    db: sqlite3.Connection


def _encode_png(pixels: np.ndarray) -> bytes:
    encoded_input = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR) if pixels.ndim == 3 else pixels
    ok, encoded = cv2.imencode(".png", encoded_input)
    if not ok:
        raise ValueError("could not encode output image")
    return bytes(encoded)


def _load_asset_rgb(store: ImageLabStore, asset_id: str) -> tuple[np.ndarray, str]:
    row = store.get_asset(asset_id)
    if row is None:
        raise ValueError("input asset does not exist")
    path = store.content_path(asset_id, variant="preview")
    if path is None:
        raise ValueError("input asset is not readable")
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("could not decode input asset")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return rgb, row["sha256"]


def _resolve_input(
    ctx: _RunContext, step: dict[str, Any], built: dict[str, dict[str, Any]]
) -> tuple[np.ndarray, dict[str, Any]]:
    ref = step["inputs"]["image"]
    if ref["kind"] == "asset":
        rgb, digest = _load_asset_rgb(ctx.store, ref["asset_id"])
        return rgb, {"kind": "asset", "asset_id": ref["asset_id"], "sha256": digest}
    if ref["kind"] == "step":
        source = built.get(ref["step_id"])
        if source is None:
            raise ValueError("input references an unresolved step")
        outputs = source["outputs"]
        port = outputs.get(ref["port"])
        if port is None:
            raise ValueError("input references an unknown port")
        path = ctx.store.files / Path(port["storage_path"])
        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR if port["channels"] != "MASK8" else cv2.IMREAD_GRAYSCALE)
        if bgr is None:
            raise ValueError("could not load step output")
        if port["channels"] == "MASK8":
            rgb = bgr
        else:
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return rgb, {
            "kind": "step_run",
            "step_run_id": source["step_run_id"],
            "step_id": ref["step_id"],
            "port": ref["port"],
            "sha256": port["sha256"],
        }
    raise ValueError("unsupported input reference")


def _persist_output(
    ctx: _RunContext, run_id: str, step_run_id: str, port: str, pixels: np.ndarray
) -> dict[str, Any]:
    payload = _encode_png(pixels)
    digest = hashlib.sha256(payload).hexdigest()
    channels = "MASK8" if pixels.ndim == 2 else "RGB8"
    relative = Path("outputs") / run_id[:2] / run_id / step_run_id[:2] / step_run_id / f"{port}.png"
    path = ctx.store.files / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(payload)
    os.replace(temp, path)
    return {
        "storage_path": str(relative),
        "sha256": digest,
        "channels": channels,
        "width": int(pixels.shape[1]),
        "height": int(pixels.shape[0]),
        "url": f"/api/image-lab/outputs/{run_id}/{step_run_id}/{port}.png",
    }


def _step_run_record(
    ctx: _RunContext, run_id: str, position: int, step: dict[str, Any]
) -> dict[str, Any]:
    step_run_id = uuid.uuid4().hex
    ctx.db.execute(
        """INSERT INTO step_runs
        (id, run_id, step_id_snapshot, position_snapshot, tool_version_id,
         resolved_inputs_json, outputs_json, status, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
        (
            step_run_id, run_id, step["id"], position,
            f"{step['tool_id']}@{step['tool_version']}",
            _json({}), _json({}), _now(),
        ),
    )
    return {"step_run_id": step_run_id, "outputs": {}}


def _execute_step(
    ctx: _RunContext,
    position: int,
    step: dict[str, Any],
    built: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if ctx.cancel_event.is_set():
        raise RunCancelledError("run was cancelled")
    record = _step_run_record(ctx, ctx.run_id, position, step)
    step_run_id = record["step_run_id"]
    started = time.monotonic()
    try:
        image, resolved_input = _resolve_input(ctx, step, built)
        roi = None
        if step.get("roi"):
            roi = Rect(*step["roi"])
        spec = OPERATORS[step["tool_id"]]
        result = run_operator(spec.id, image, roi=roi, params=step.get("params", {}))
        if not isinstance(result.images, dict) or "image" not in result.images:
            raise ValueError("tool did not produce the required image output")
        outputs: dict[str, Any] = {}
        for port, pixels in result.images.items():
            outputs[port] = _persist_output(ctx, ctx.run_id, step_run_id, port, pixels)
        duration_ms = round((time.monotonic() - started) * 1000)
        ctx.db.execute(
            """UPDATE step_runs SET resolved_inputs_json=?, outputs_json=?,
            status='completed', finished_at=?, duration_ms=?
            WHERE id=?""",
            (_json({"image": resolved_input}), _json(outputs), _now(), duration_ms, step_run_id),
        )
        record["outputs"] = outputs
        return record
    except Exception as exc:
        duration_ms = round((time.monotonic() - started) * 1000)
        ctx.db.execute(
            """UPDATE step_runs SET status='failed', finished_at=?, duration_ms=?, error_json=?
            WHERE id=?""",
            (_now(), duration_ms, _json({"type": type(exc).__name__, "message": str(exc)}), step_run_id),
        )
        raise


def _build_snapshot(notebook: dict[str, Any]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for step in notebook["steps"]:
        if step["kind"] == "note":
            steps.append({
                "id": step["id"], "position": step["position"], "kind": "note",
                "title": step["title"],
            })
        else:
            steps.append({
                "id": step["id"], "position": step["position"], "kind": "tool",
                "title": step["title"],
                "tool_id": step["tool_id"],
                "tool_version": step["tool_version"],
                "tool_version_id": f"{step['tool_id']}@{step['tool_version']}",
                "inputs": step["inputs"],
                "params": step["params"],
                "roi": step["roi"],
            })
    return {"title": notebook["title"], "description": notebook["description"], "steps": steps}


class NotebookRunner:
    def __init__(self, store: ImageLabStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._active: dict[str, _RunToken] = {}

    def _get_notebook(self, notebook_id: str) -> dict[str, Any] | None:
        from pixelforge.image_lab.notebooks import NotebookStore

        return NotebookStore(self.store).get(notebook_id)

    def run_sync(
        self,
        notebook_id: str,
        *,
        idempotency_key: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or uuid.uuid4().hex
        notebook = self._get_notebook(notebook_id)
        if notebook is None:
            raise LookupError("unknown notebook")
        snapshot = _build_snapshot(notebook)
        snapshot_hash = _snapshot_hash(notebook_id, notebook["revision"], snapshot)
        with closing(self.store.connect()) as db:
            if idempotency_key:
                existing = db.execute(
                    """SELECT id, status FROM notebook_runs
                    WHERE notebook_id=? AND idempotency_key=?""",
                    (notebook_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["status"] in ("running", "pending"):
                        raise RunConflictError(f"notebook has an active run {existing['id']}")
                    return self._load_run(existing["id"])
            active = db.execute(
                """SELECT id FROM notebook_runs
                WHERE notebook_id=? AND status IN ('running','pending')""",
                (notebook_id,),
            ).fetchone()
            if active is not None:
                raise RunConflictError(f"notebook has an active run {active['id']}")
            with db:
                db.execute(
                    """INSERT INTO notebook_runs
                    (id, notebook_id, notebook_revision, snapshot_json, snapshot_hash,
                     status, started_at, idempotency_key)
                    VALUES (?, ?, ?, ?, ?, 'running', ?, ?)""",
                    (run_id, notebook_id, notebook["revision"], _json(snapshot), snapshot_hash,
                     _now(), idempotency_key),
                )
        cancel_event = threading.Event()
        token = _RunToken(run_id, cancel_event)
        with self._lock:
            self._active[run_id] = token
        try:
            with closing(self.store.connect()) as db:
                ctx = _RunContext(run_id, self.store, cancel_event, db)
                with db:
                    built: dict[str, dict[str, Any]] = {}
                    error_json: str | None = None
                    try:
                        for position, step in enumerate(snapshot["steps"]):
                            if step["kind"] == "note":
                                continue
                            built[step["id"]] = _execute_step(ctx, position, step, built)
                        db.execute(
                            "UPDATE notebook_runs SET status='completed', finished_at=? WHERE id=?",
                            (_now(), run_id),
                        )
                    except RunCancelledError:
                        db.execute(
                            "UPDATE notebook_runs SET status='cancelled', finished_at=? WHERE id=?",
                            (_now(), run_id),
                        )
                        error_json = _json({"type": "RunCancelledError", "message": "run was cancelled"})
                    except Exception as exc:
                        db.execute(
                            "UPDATE notebook_runs SET status='failed', finished_at=? WHERE id=?",
                            (_now(), run_id),
                        )
                        error_json = _json({"type": type(exc).__name__, "message": str(exc)})
                    if error_json:
                        db.execute(
                            "UPDATE notebook_runs SET error_json=? WHERE id=?",
                            (error_json, run_id),
                        )
        finally:
            with self._lock:
                self._active.pop(run_id, None)
        return self._load_run(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            token = self._active.get(run_id)
        if token is None:
            raise LookupError("run is not active")
        token.cancel_event.set()
        return self._load_run(run_id)

    def list_runs(self, notebook_id: str) -> list[dict[str, Any]]:
        with closing(self.store.connect()) as db:
            rows = db.execute(
                """SELECT id, notebook_revision, status, started_at, finished_at, error_json
                FROM notebook_runs WHERE notebook_id=? ORDER BY started_at DESC""",
                (notebook_id,),
            ).fetchall()
        return [_run_summary(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._load_run(run_id)

    def _load_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self.store.connect()) as db:
            run = db.execute("SELECT * FROM notebook_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                return None
            steps = db.execute(
                """SELECT * FROM step_runs WHERE run_id=? ORDER BY position_snapshot""",
                (run_id,),
            ).fetchall()
        item = dict(run)
        item["snapshot"] = json.loads(run["snapshot_json"])
        item["error"] = json.loads(run["error_json"]) if run["error_json"] else None
        item["steps"] = []
        for step in steps:
            step_item = dict(step)
            step_item["resolved_inputs"] = json.loads(step["resolved_inputs_json"])
            step_item["outputs"] = json.loads(step["outputs_json"])
            item["steps"].append(step_item)
        return item

    def output_path(self, run_id: str, step_run_id: str, port: str) -> Path | None:
        run = self._load_run(run_id)
        if run is None:
            return None
        for step in run["steps"]:
            if step["id"] != step_run_id:
                continue
            output = step["outputs"].get(port)
            if output is None:
                return None
            path = self.store.files / Path(output["storage_path"])
            resolved = path.resolve()
            if not resolved.is_relative_to(self.store.files.resolve()) or not resolved.is_file():
                return None
            return resolved
        return None


@dataclass
class _RunToken:
    run_id: str
    cancel_event: threading.Event


def _run_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "notebook_revision": row["notebook_revision"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
    }
