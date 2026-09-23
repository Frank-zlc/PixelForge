"""Draft persistence, graph validation, and optimistic locking."""

from __future__ import annotations

import uuid
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from pixelforge.config import Settings
from pixelforge.main import build_app


def _step(
    tool_id: str, image_input: dict[str, str], *, roi: list[int] | None = None
) -> dict[str, object]:
    return {"id": uuid.uuid4().hex, "kind": "tool", "title": tool_id,
            "tool_id": tool_id, "inputs": {"image": image_input}, "params": {}, "roi": roi}


def test_notebook_graph_persists_and_rejects_invalid_refs(tmp_path: Path) -> None:
    settings = Settings(
        adb_executable="adb-absent", data_dir=tmp_path, asset_dir=tmp_path, log_level="ERROR"
    )
    image = np.full((25, 40, 3), 180, np.uint8)
    ok, png = cv2.imencode(".png", image)
    assert ok

    with TestClient(build_app(settings)) as client:
        asset = client.post(
            "/api/image-lab/imports?relative_name=one/a.png", content=bytes(png)
        ).json()
        created = client.post("/api/image-lab/notebooks", json={"title": "颜色实验"})
        assert created.status_code == 201
        notebook = created.json()
        path = f"/api/image-lab/notebooks/{notebook['id']}"
        crop = _step("crop", {"kind": "asset", "asset_id": asset["id"]}, roi=[2, 3, 20, 15])
        color = _step("color_mask", {"kind": "step", "step_id": crop["id"], "port": "image"})
        threshold = _step("threshold", {"kind": "step", "step_id": color["id"], "port": "image"})
        draft = {"expected_revision": 1, "title": "颜色实验", "description": "验证链式输入",
                 "steps": [crop, color, threshold]}
        saved = client.put(path, json=draft)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 2
        assert saved.json()["steps"][1]["inputs"]["image"]["step_id"] == crop["id"]
        assert client.put(path, json=draft).status_code == 409

        wrong_port = {**threshold, "inputs": {"image": {
            "kind": "step", "step_id": color["id"], "port": "mask"}}}
        invalid = client.put(path, json={**draft, "expected_revision": 2,
                                         "steps": [crop, color, wrong_port]})
        assert invalid.status_code == 422
        assert "incompatible" in invalid.text
        assert client.put(path, json={**draft, "expected_revision": 2,
                                      "steps": [threshold, crop, color]}).status_code == 422
        assert client.put(path, json={**draft, "expected_revision": 2,
                                      "steps": [color, threshold]}).status_code == 422
        bad_roi = {**draft, "expected_revision": 2,
                   "steps": [{**crop, "roi": [35, 0, 20, 15]}]}
        assert client.put(path, json=bad_roi).status_code == 422
        assert client.get(path).json()["revision"] == 2

    with TestClient(build_app(settings)) as client:
        reopened = client.get(path)
        assert reopened.status_code == 200
        assert [step["tool_id"] for step in reopened.json()["steps"]] == [
            "crop", "color_mask", "threshold"
        ]
        assert client.get("/api/image-lab/notebooks").json()["items"][0]["step_count"] == 3


def test_notebook_note_and_missing_assets(tmp_path: Path) -> None:
    """Saving a dangling asset reference is allowed; only its *shape* is
    checked here. Whether the asset actually exists is a run-time concern --
    see test_notebook_runs.py::test_run_rejects_missing_asset_and_conflict,
    which runs this exact scenario and asserts a graceful 'failed' run rather
    than a save-time rejection. A malformed id (not 32 hex chars) is still a
    structural error and stays rejected below.
    """
    settings = Settings(
        adb_executable="adb-absent", data_dir=tmp_path, asset_dir=tmp_path, log_level="ERROR"
    )
    with TestClient(build_app(settings)) as client:
        notebook = client.post("/api/image-lab/notebooks", json={"title": "备注"}).json()
        path = f"/api/image-lab/notebooks/{notebook['id']}"
        note = {"id": uuid.uuid4().hex, "kind": "note", "title": "想法"}
        assert client.put(path, json={"expected_revision": 1, "title": "备注",
                                      "steps": [note]}).status_code == 200
        dangling = _step("grayscale", {"kind": "asset", "asset_id": uuid.uuid4().hex})
        saved = client.put(path, json={"expected_revision": 2, "title": "备注",
                                       "steps": [note, dangling]})
        assert saved.status_code == 200, saved.text
        reopened = client.get(path).json()
        assert [step["kind"] for step in reopened["steps"]] == ["note", "tool"]

        malformed = _step("grayscale", {"kind": "asset", "asset_id": "not-a-real-id"})
        assert client.put(path, json={"expected_revision": 3, "title": "备注",
                                      "steps": [note, malformed]}).status_code == 422
