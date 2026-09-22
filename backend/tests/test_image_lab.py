"""N1 catalog migration, normalized imports, and typed quick preview."""

from __future__ import annotations

import base64
import sqlite3
from contextlib import closing
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from pixelforge.config import Settings
from pixelforge.main import build_app


def _png(image: np.ndarray) -> bytes:
    ok, result = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    assert ok
    return bytes(result)


def test_import_persists_same_name_images_and_typed_tool_output(tmp_path: Path) -> None:
    settings = Settings(
        adb_executable="adb-absent", data_dir=tmp_path, asset_dir=tmp_path, log_level="ERROR"
    )
    image = np.zeros((20, 30, 3), np.uint8)
    image[5:15, 5:20] = (250, 220, 20)
    payload = _png(image)

    with TestClient(build_app(settings)) as client:
        first = client.post(
            "/api/image-lab/imports?relative_name=one/a.png&batch_id=sample", content=payload
        )
        second = client.post(
            "/api/image-lab/imports?relative_name=two/a.png&batch_id=sample", content=payload
        )
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] != second.json()["id"]
        assert first.json()["sha256"] == second.json()["sha256"]
        assert client.get("/api/image-lab/assets?batch_id=sample").json()["items"][0][
            "relative_name"
        ] == "one/a.png"

        tools = client.get("/api/image-lab/tools").json()
        assert tools["counts"] == {
            "ready_algorithms": 6,
            "pending_adapter": 3,
            "planned_algorithms": 2,
            "planned_workflows": 1,
        }
        mask = next(tool for tool in tools["items"] if tool["id"] == "color_mask")
        assert mask["outputs"]["mask"] == "MASK8"
        assert mask["params_schema"][0]["name"] == "color"
        assert ".color_mask" in mask["function_name"]
        examples = client.get("/api/image-lab/tools/color_mask/examples").json()
        assert examples[0]["result_refs"]["mask"]["asset_id"]
        assert client.get(examples[0]["result_refs"]["mask"]["url"]).headers[
            "content-type"
        ] == "image/png"

        response = client.post("/api/image-lab/tools/color_mask/run", content=payload)
        assert response.status_code == 200
        result = response.json()
        assert set(result["outputs"]) == {"image", "mask"}
        encoded = result["outputs"]["mask"]["data_url"].split(",", 1)[1]
        decoded = cv2.imdecode(np.frombuffer(base64.b64decode(encoded), np.uint8), 0)
        assert np.count_nonzero(decoded) == 15 * 10

        invalid_path = client.post(
            "/api/image-lab/imports?relative_name=../x.png", content=payload
        )
        assert invalid_path.status_code == 422
        invalid_params = client.post(
            "/api/image-lab/tools/threshold/run?params=%7B%22bad%22%3A1%7D", content=payload
        )
        assert invalid_params.status_code == 422

    with TestClient(build_app(settings)) as client:
        items = client.get("/api/image-lab/assets?batch_id=sample").json()["items"]
        assert len(items) == 2
        assert client.get(items[0]["preview_url"]).headers["content-type"] == "image/png"
        thumb = client.get(items[0]["thumbnail_url"])
        assert thumb.headers["content-type"] == "image/png"
        thumb_pixels = cv2.imdecode(np.frombuffer(thumb.content, np.uint8), cv2.IMREAD_COLOR)
        assert max(thumb_pixels.shape[:2]) <= 112
        assert client.get(f"/api/image-lab/assets/{items[0]['id']}").json()["width"] == 30


def test_existing_catalog_is_backed_up_before_schema_migration(tmp_path: Path) -> None:
    database = tmp_path / "image_tools.sqlite3"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("""CREATE TABLE image_tools (
            id TEXT PRIMARY KEY, category TEXT NOT NULL, name TEXT NOT NULL,
            function_name TEXT NOT NULL, description TEXT NOT NULL, effect_image TEXT,
            availability TEXT NOT NULL, source TEXT NOT NULL,
            parameters_json TEXT NOT NULL, sort_order INTEGER NOT NULL)""")
        db.execute("""INSERT INTO image_tools VALUES (
            'legacy', '旧分类', '保留条目', 'none', '历史记录', NULL,
            'planned', 'user', '{}', 99)""")

    settings = Settings(
        adb_executable="adb-absent", data_dir=tmp_path, asset_dir=tmp_path, log_level="ERROR"
    )
    with TestClient(build_app(settings)) as client:
        items = client.get("/api/image-lab/tools").json()["items"]
        assert any(item["id"] == "legacy" for item in items)
    backups = list(tmp_path.glob("image_tools.before-image-lab-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as db:
        row = db.execute("SELECT name FROM image_tools WHERE id='legacy'").fetchone()
        assert row is not None and row[0] == "保留条目"


def test_import_applies_exif_orientation_and_alpha_policy(tmp_path: Path) -> None:
    settings = Settings(
        adb_executable="adb-absent", data_dir=tmp_path, asset_dir=tmp_path, log_level="ERROR"
    )
    image = np.zeros((10, 20, 3), np.uint8)
    image[:, :6] = (255, 0, 0)
    ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    assert ok
    # EXIF orientation 6 (rotate 90 degrees clockwise), TIFF little endian.
    tiff = bytes.fromhex("49492a0008000000010012010300010000000600000000000000")
    exif = b"Exif\x00\x00" + tiff
    payload = (
        bytes(jpeg[:2]) + b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big")
        + exif + bytes(jpeg[2:])
    )

    rgba = np.zeros((5, 6, 4), np.uint8)
    rgba[:, :, :3] = (0, 0, 255)
    rgba[:, :, 3] = 0
    rgba[0, 0, 3] = 255
    ok, png = cv2.imencode(".png", rgba)
    assert ok

    with TestClient(build_app(settings)) as client:
        rotated = client.post("/api/image-lab/imports?relative_name=rotated.jpg", content=payload)
        assert rotated.status_code == 201
        assert (rotated.json()["width"], rotated.json()["height"]) == (10, 20)
        alpha = client.post("/api/image-lab/imports?relative_name=alpha.png", content=bytes(png))
        assert alpha.status_code == 201
        preview = client.get(alpha.json()["preview_url"])
        decoded = cv2.imdecode(np.frombuffer(preview.content, np.uint8), cv2.IMREAD_COLOR)
        assert tuple(decoded[1, 1]) == (255, 255, 255)
        assert tuple(decoded[0, 0]) == (0, 0, 255)
