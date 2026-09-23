"""End-to-end notebook execution via the API."""

from __future__ import annotations

import uuid
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from pixelforge.config import Settings
from pixelforge.main import build_app


def _step(tool_id: str, image_input: dict[str, str], *, params: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "id": uuid.uuid4().hex,
        "kind": "tool",
        "title": tool_id,
        "tool_id": tool_id,
        "tool_version": "1.0.0",
        "inputs": {"image": image_input},
        "params": params or {},
        "roi": None,
    }


def test_run_chain_and_outputs(tmp_path: Path) -> None:
    settings = Settings(
        adb_executable="adb-absent", data_dir=tmp_path, asset_dir=tmp_path, log_level="ERROR"
    )
    image = np.full((40, 80, 3), (200, 150, 100), np.uint8)
    ok, png = cv2.imencode(".png", image)
    assert ok

    with TestClient(build_app(settings)) as client:
        asset = client.post(
            "/api/image-lab/imports?relative_name=chain/a.png", content=bytes(png)
        ).json()
        notebook = client.post("/api/image-lab/notebooks", json={"title": "链式运行"}).json()
        nb_path = f"/api/image-lab/notebooks/{notebook['id']}"
        gray = _step("grayscale", {"kind": "asset", "asset_id": asset["id"]})
        color = _step("color_mask", {"kind": "step", "step_id": gray["id"], "port": "image"},
                       params={"color": "yellow", "s_min": 0, "v_min": 0})
        draft = {"expected_revision": 1, "title": "链式运行", "description": "", "steps": [gray, color]}
        assert client.put(nb_path, json=draft).status_code == 200

        run_resp = client.post(f"{nb_path}/runs", json={})
        assert run_resp.status_code == 202, run_resp.text
        run = client.get(f"{nb_path}/runs").json()["items"][0]
        assert run["status"] == "completed"

        detail = client.get(f"{nb_path}/runs/{run['id']}").json()
        assert detail["notebook_id"] == notebook["id"]
        tool_steps = [s for s in detail["steps"] if s["step_id_snapshot"] in (gray["id"], color["id"])]
        assert len(tool_steps) == 2
        for step in tool_steps:
            assert step["status"] == "completed"
            assert step["outputs"]["image"]["url"]
            assert step["outputs"]["image"]["sha256"]


def test_run_rejects_missing_asset_and_conflict(tmp_path: Path) -> None:
    settings = Settings(
        adb_executable="adb-absent", data_dir=tmp_path, asset_dir=tmp_path, log_level="ERROR"
    )
    image = np.full((20, 20, 3), 128, np.uint8)
    ok, png = cv2.imencode(".png", image)
    assert ok

    with TestClient(build_app(settings)) as client:
        asset = client.post(
            "/api/image-lab/imports?relative_name=bad/a.png", content=bytes(png)
        ).json()
        notebook = client.post("/api/image-lab/notebooks", json={"title": "失败"}).json()
        nb_path = f"/api/image-lab/notebooks/{notebook['id']}"
        bad = _step("grayscale", {"kind": "asset", "asset_id": uuid.uuid4().hex})
        draft = {"expected_revision": 1, "title": "失败", "steps": [bad]}
        assert client.put(nb_path, json=draft).status_code == 200

        run_resp = client.post(f"{nb_path}/runs", json={})
        assert run_resp.status_code == 202
        run = client.get(f"{nb_path}/runs").json()["items"][0]
        assert run["status"] == "failed"

        # concurrent run conflicts
        #
        # start_run() blocks (via run_in_threadpool) until the whole run has
        # finished, so two *sequential* client.post() calls can never overlap
        # in status='running' -- the conflict branch would never actually be
        # exercised. Force genuine overlap: patch _execute_step to pause on a
        # threading.Event once it has been entered (i.e. once the run row is
        # provably 'running' in the DB), fire the run on a background thread,
        # wait for that signal, then issue the conflicting request for real.
        import threading
        from unittest.mock import patch

        from pixelforge.image_lab import runs as runs_module

        good = _step("grayscale", {"kind": "asset", "asset_id": asset["id"]})
        client.put(nb_path, json={"expected_revision": 2, "title": "失败", "steps": [good]})

        entered = threading.Event()
        release = threading.Event()
        original_execute_step = runs_module._execute_step

        def blocking_execute_step(*args: object, **kwargs: object) -> object:
            entered.set()
            release.wait(timeout=5)
            return original_execute_step(*args, **kwargs)

        results: dict[str, object] = {}

        def run_first() -> None:
            with patch.object(runs_module, "_execute_step", blocking_execute_step):
                results["first"] = client.post(f"{nb_path}/runs", json={})

        thread = threading.Thread(target=run_first)
        thread.start()
        try:
            assert entered.wait(timeout=5), "run did not reach 'running' in time"
            conflict = client.post(f"{nb_path}/runs", json={})
            assert conflict.status_code == 409
        finally:
            release.set()
            thread.join(timeout=5)
        assert results["first"].status_code == 202


def test_run_idempotency_and_output_content(tmp_path: Path) -> None:
    settings = Settings(
        adb_executable="adb-absent", data_dir=tmp_path, asset_dir=tmp_path, log_level="ERROR"
    )
    image = np.full((30, 60, 3), (50, 100, 200), np.uint8)
    ok, png = cv2.imencode(".png", image)
    assert ok

    with TestClient(build_app(settings)) as client:
        asset = client.post(
            "/api/image-lab/imports?relative_name=thumb/b.png", content=bytes(png)
        ).json()
        notebook = client.post("/api/image-lab/notebooks", json={"title": "幂等"}).json()
        nb_path = f"/api/image-lab/notebooks/{notebook['id']}"
        step = _step("grayscale", {"kind": "asset", "asset_id": asset["id"]})
        client.put(nb_path, json={"expected_revision": 1, "title": "幂等", "steps": [step]})

        key = "run-twice"
        r1 = client.post(f"{nb_path}/runs", json={"idempotency_key": key}).json()
        r2 = client.post(f"{nb_path}/runs", json={"idempotency_key": key}).json()
        assert r1["notebook_id"] == r2["notebook_id"]

        detail = client.get(f"{nb_path}/runs/{r1['run_id']}").json()
        out = detail["steps"][0]["outputs"]["image"]
        content = client.get(out["url"])
        assert content.status_code == 200
        assert content.headers["content-type"] == "image/png"
