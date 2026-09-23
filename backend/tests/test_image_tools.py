"""A local image can exercise the catalog without any Android session."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from pixelforge.config import Settings
from pixelforge.main import build_app


def _png(pixels: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR))
    assert ok
    return bytes(encoded)


def _pixels(payload: bytes) -> np.ndarray:
    decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    return cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)


def test_catalog_demo_and_local_crop(tmp_path: Path) -> None:
    settings = Settings(
        adb_executable="adb-absent", data_dir=tmp_path, asset_dir=tmp_path, log_level="ERROR"
    )
    image = np.zeros((40, 60, 3), np.uint8)
    image[10:25, 20:45] = (230, 210, 35)

    with TestClient(build_app(settings)) as client:
        tools = client.get("/api/image-tools").json()
        available_ids = {tool["id"] for tool in tools if tool["availability"] == "available"}
        assert available_ids >= {
            "crop",
            "grayscale",
            "color_mask",
            "edges",
            "threshold",
            "text_enhance",
            "template_match",
            "match_verify",
            "ocr",
            "text_regions",
            "highlight_state",
            "template_match_expand",
            "line_detect",
        }
        # face_detect needs an OpenCV build that still ships CascadeClassifier
        # (dropped in OpenCV 5.x) -- available or not depending on environment,
        # never asserted either way here.
        assert all(tool["effect_image"] for tool in tools if tool["availability"] == "available")
        assert client.get("/api/image-tools/color_mask/demo").headers["content-type"] == "image/png"

        crop = client.post(
            "/api/image-tools/crop/run?x=20&y=10&width=25&height=15", content=_png(image)
        )
        assert crop.status_code == 200
        assert np.array_equal(_pixels(crop.content), image[10:25, 20:45])

        mask = client.post("/api/image-tools/color_mask/run?color=yellow", content=_png(image))
        assert mask.status_code == 200
        assert np.count_nonzero(_pixels(mask.content)[10:25, 20:45]) > 0
        assert np.count_nonzero(_pixels(mask.content)[:10]) == 0

        # screen_diff is still pending_adapter (unlike ocr, now registered and ready)
        assert client.post("/api/image-tools/screen_diff/run", content=_png(image)).status_code == 409
        assert client.post("/api/image-tools/crop/run", content=_png(image)).status_code == 422
        assert (
            client.post(
                "/api/image-tools/crop/run?x=100&y=100&width=10&height=10", content=_png(image)
            ).status_code
            == 422
        )
        assert client.post("/api/image-tools/edges/run", content=b"bad").status_code == 422

    assert (tmp_path / "image_tools.sqlite3").is_file()
