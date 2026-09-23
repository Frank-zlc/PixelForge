"""Validated, revisioned notebook drafts. Execution is a separate stage."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from pixelforge.image_lab.operators import IMAGE_INPUT_TYPE, OPERATORS, validate_params
from pixelforge.image_lab.storage import ImageLabStore

_ID = re.compile(r"[0-9a-f]{32}\Z")


class NotebookConflictError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: object, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{field} must be a non-empty string under {limit} characters" if required
                         else f"{field} must be a string under {limit} characters")
    return value.strip()


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _roi(value: object, *, required: bool) -> list[int] | None:
    if value is None:
        if required:
            raise ValueError("this tool requires an ROI")
        return None
    if (not isinstance(value, list) or len(value) != 4
            or any(type(part) is not int for part in value)):
        raise ValueError("ROI must be [x, y, width, height] in input pixels")
    x, y, width, height = value
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError("ROI must have non-negative origin and positive size")
    return value


def _validate_steps(db: sqlite3.Connection, steps: object) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or len(steps) > 100:
        raise ValueError("steps must be a list of at most 100 items")
    validated: list[dict[str, Any]] = []
    outputs: dict[str, dict[str, str]] = {}
    for position, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise ValueError(f"step {position + 1} must be an object")
        step_id = _id(raw.get("id"), "step id")
        if step_id in outputs:
            raise ValueError("duplicate step id")
        kind = raw.get("kind")
        title = _text(raw.get("title", ""), "step title", 120, required=True)
        if kind == "note":
            if raw.get("inputs") or raw.get("roi") or raw.get("params"):
                raise ValueError("note steps cannot have inputs, ROI or parameters")
            validated.append({"id": step_id, "position": position, "kind": "note",
                              "title": title, "tool_version_id": None, "inputs": {},
                              "params": {}, "roi": None})
            outputs[step_id] = {}
            continue
        if kind != "tool":
            raise ValueError("step kind must be tool or note")
        tool_id = raw.get("tool_id")
        spec = OPERATORS.get(tool_id) if isinstance(tool_id, str) else None
        if spec is None:
            raise ValueError(f"step {position + 1} has no registered tool")
        version = raw.get("tool_version", spec.version)
        if version != spec.version:
            raise ValueError(f"step {position + 1} uses an unavailable tool version")
        version_id = f"{spec.id}@{spec.version}"
        ready = db.execute(
            "SELECT catalog_state FROM image_tools WHERE id=?", (spec.id,)
        ).fetchone()
        if ready is None or ready["catalog_state"] != "ready":
            raise ValueError(f"step {position + 1} tool is not ready")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("step parameters must be an object")
        parsed = validate_params(spec, params)
        inputs = raw.get("inputs")
        if not isinstance(inputs, dict) or set(inputs) != {"image"}:
            raise ValueError("tool step requires one named image input")
        image_ref = inputs["image"]
        if not isinstance(image_ref, dict):
            raise ValueError("image input must be a reference")
        if image_ref.get("kind") == "asset" and set(image_ref) == {"kind", "asset_id"}:
            # Asset existence is checked at RUN time, not here. A draft is allowed
            # to reference an asset that was since deleted, or one the user is
            # about to (re-)import -- exactly like a Jupyter cell can reference a
            # name that doesn't exist yet. The run engine (image_lab/runs.py)
            # re-resolves the reference and records a graceful 'failed' step
            # rather than a crash; see test_notebook_runs.py. Only the ID's shape
            # is a structural error worth rejecting at save time.
            asset_id = _id(image_ref["asset_id"], "asset id")
            asset = db.execute(
                """SELECT width, height FROM image_assets
                WHERE id=? AND kind='original' AND deleted_at IS NULL""", (asset_id,)
            ).fetchone()
            bounds = (asset["width"], asset["height"]) if asset is not None else None
        elif image_ref.get("kind") == "step" and set(image_ref) == {"kind", "step_id", "port"}:
            source_id = _id(image_ref["step_id"], "source step id")
            port = image_ref["port"]
            if source_id not in outputs:
                raise ValueError("step input must reference an earlier step")
            if not isinstance(port, str):
                raise ValueError("step output port name must be a string")
            # Every operator's "image" input needs IMAGE_RGB8 -- a MASK8 port
            # (color_mask's "mask", say) is a single 0/255 channel and is not
            # interchangeable with a real image, even though both are valid
            # *outputs*. Check against the one true value (see IMAGE_INPUT_TYPE)
            # rather than a locally-maintained set that can drift out of sync
            # with what operators.py actually requires.
            if outputs[source_id].get(port) != IMAGE_INPUT_TYPE:
                raise ValueError("step output port is incompatible with image input")
            bounds = None
        else:
            raise ValueError("image input must reference an asset or earlier named output")
        roi = _roi(raw.get("roi"), required=spec.needs_roi)
        if (roi is not None and bounds is not None
                and (roi[0] + roi[2] > bounds[0] or roi[1] + roi[3] > bounds[1])):
            raise ValueError("ROI exceeds source image")
        validated.append({"id": step_id, "position": position, "kind": "tool",
                          "title": title, "tool_id": spec.id, "tool_version": spec.version,
                          "tool_version_id": version_id, "inputs": inputs,
                          "params": parsed, "roi": roi})
        outputs[step_id] = dict(spec.outputs)
    return validated


class NotebookStore:
    def __init__(self, images: ImageLabStore) -> None:
        self.images = images

    def create(self, title: object, description: object = "") -> dict[str, Any]:
        name = _text(title, "title", 120, required=True)
        detail = _text(description, "description", 2000)
        notebook_id, now = uuid.uuid4().hex, _now()
        with closing(self.images.connect()) as db, db:
            db.execute(
                """INSERT INTO notebooks
                (id, title, description, revision, status, created_at, updated_at)
                VALUES (?, ?, ?, 1, 'active', ?, ?)""",
                (notebook_id, name, detail, now, now),
            )
        item = self.get(notebook_id)
        assert item is not None
        return item

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with closing(self.images.connect()) as db:
            rows = db.execute(
                """SELECT n.id, n.title, n.description, n.revision, n.status,
                   n.created_at, n.updated_at, COUNT(s.id) AS step_count
                   FROM notebooks n LEFT JOIN notebook_steps s ON s.notebook_id=n.id
                   GROUP BY n.id ORDER BY n.updated_at DESC, n.id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, notebook_id: str) -> dict[str, Any] | None:
        if _ID.fullmatch(notebook_id) is None:
            return None
        with closing(self.images.connect()) as db:
            row = db.execute("SELECT * FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
            if row is None:
                return None
            steps = db.execute(
                "SELECT * FROM notebook_steps WHERE notebook_id=? ORDER BY position",
                (notebook_id,),
            ).fetchall()
        item = dict(row)
        item["steps"] = []
        for step in steps:
            version_id = step["tool_version_id"]
            item["steps"].append({
                "id": step["id"], "position": step["position"], "kind": step["kind"],
                "title": step["title"],
                "tool_id": version_id.split("@", 1)[0] if version_id else None,
                "tool_version": version_id.split("@", 1)[1] if version_id else None,
                "inputs": json.loads(step["inputs_json"]),
                "params": json.loads(step["params_json"]),
                "roi": json.loads(step["roi_json"]) if step["roi_json"] else None,
            })
        return item

    def save(self, notebook_id: str, draft: dict[str, Any]) -> dict[str, Any]:
        if _ID.fullmatch(notebook_id) is None:
            raise LookupError("unknown notebook")
        revision = draft.get("expected_revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        title = _text(draft.get("title"), "title", 120, required=True)
        description = _text(draft.get("description", ""), "description", 2000)
        now = _now()
        with closing(self.images.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                current = db.execute(
                    "SELECT revision FROM notebooks WHERE id=?", (notebook_id,)
                ).fetchone()
                if current is None:
                    raise LookupError("unknown notebook")
                if current["revision"] != revision:
                    raise NotebookConflictError("notebook revision changed; reload before saving")
                steps = _validate_steps(db, draft.get("steps"))
                db.execute("DELETE FROM notebook_steps WHERE notebook_id=?", (notebook_id,))
                for step in steps:
                    db.execute(
                        """INSERT INTO notebook_steps
                        (id, notebook_id, position, kind, title, tool_version_id,
                         inputs_json, params_json, roi_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (step["id"], notebook_id, step["position"], step["kind"], step["title"],
                         step["tool_version_id"], json.dumps(step["inputs"]),
                         json.dumps(step["params"]), json.dumps(step["roi"])
                         if step["roi"] is not None else None, now, now),
                    )
                db.execute(
                    """UPDATE notebooks SET title=?, description=?, revision=revision+1,
                    updated_at=? WHERE id=?""", (title, description, now, notebook_id)
                )
        item = self.get(notebook_id)
        assert item is not None
        return item
