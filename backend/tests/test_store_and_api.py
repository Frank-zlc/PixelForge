"""Project persistence and end-to-end API behaviour."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from pixelforge.api.capture import CropRequest, _asset_output_path
from pixelforge.config import Settings
from pixelforge.main import build_app
from pixelforge.store.projects import ProjectStore
from pixelforge.script.model import CoordTarget, Project, Script, Step, StepAction, Target


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(tmp_path)


class TestProjectStore:
    def test_round_trip(self, store: ProjectStore) -> None:
        store.save(Project(id="shop", name="Shop", app_package="com.shop"))
        assert store.get("shop").app_package == "com.shop"
        assert [p.id for p in store.list_projects()] == ["shop"]

    def test_unknown_project(self, store: ProjectStore) -> None:
        with pytest.raises(KeyError, match="unknown project"):
            store.get("nope")

    def test_scripts_are_replaced_not_duplicated(self, store: ProjectStore) -> None:
        store.save(Project(id="shop", name="Shop"))
        script = Script(id="flow", name="One")
        store.save_script("shop", script)
        store.save_script("shop", script.model_copy(update={"name": "Two"}))
        scripts = store.get("shop").scripts
        assert len(scripts) == 1 and scripts[0].name == "Two"

    def test_delete_script(self, store: ProjectStore) -> None:
        store.save(Project(id="shop", name="Shop"))
        store.save_script("shop", Script(id="flow", name="One"))
        assert store.delete_script("shop", "flow").scripts == []

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "", "with space", "x\x00y"])
    def test_path_traversal_is_refused(self, store: ProjectStore, bad: str) -> None:
        # These identifiers become path segments, so they are rejected rather
        # than sanitised.
        with pytest.raises(ValueError, match="invalid identifier"):
            store.project_dir(bad)

    def test_templates_are_stored_beside_the_project(self, store: ProjectStore) -> None:
        store.save(Project(id="shop", name="Shop"))
        path = store.save_template("shop", "pay", b"\x89PNG\r\n\x1a\nfake")
        assert path.name == "pay.png"
        assert [t["name"] for t in store.list_templates("shop")] == ["pay"]

    def test_template_cannot_create_an_orphan_project(self, store: ProjectStore) -> None:
        with pytest.raises(KeyError, match="unknown project"):
            store.save_template("missing", "pay", b"png")

    def test_standalone_asset_folder_stays_below_configured_root(self, tmp_path: Path) -> None:
        path = _asset_output_path(tmp_path / "assets", "albion/items", "hide")
        assert path == (tmp_path / "assets/albion/items/hide.png").resolve()
        with pytest.raises(HTTPException, match="relative path"):
            _asset_output_path(tmp_path / "assets", "../outside", "hide")

    def test_crop_filename_accepts_unicode_and_strips_png_suffix(self) -> None:
        crop = CropRequest(
            token="t",
            name="物品列表.png",
            x=0,
            y=0,
            width=10,
            height=10,
        )
        assert crop.name == "物品列表"
        with pytest.raises(ValueError, match="unsafe"):
            CropRequest(
                token="t",
                name="../outside",
                x=0,
                y=0,
                width=10,
                height=10,
            )

    def test_writes_are_atomic(self, store: ProjectStore) -> None:
        # A temp file plus rename means an interrupted save cannot leave a
        # truncated script behind.
        store.save(Project(id="shop", name="Shop"))
        assert not list(store.project_dir("shop").glob("*.tmp"))

    def test_one_corrupt_file_does_not_hide_the_rest(self, store: ProjectStore) -> None:
        store.save(Project(id="good", name="Good"))
        broken = store.root / "broken"
        broken.mkdir()
        (broken / "project.json").write_text("{ not json")
        assert [p.id for p in store.list_projects()] == ["good"]


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        adb_executable="adb-absent",
        data_dir=tmp_path,
        asset_dir=tmp_path,
        log_level="ERROR",
    )
    with TestClient(build_app(settings)) as test_client:
        yield test_client


class TestApi:
    def test_health_reports_capabilities_rather_than_failing(self, client) -> None:
        # A missing adb disables device features; it must not stop the server,
        # because a UI that loads and explains itself beats a dead process.
        payload = client.get("/api/health").json()
        assert payload["status"] == "ok"
        assert payload["adb_available"] is False
        assert "scrcpy_jar" in payload["capabilities"]

    def test_device_list_is_empty_not_an_error(self, client) -> None:
        assert client.get("/api/devices").json() == []

    def test_storage_reports_the_real_asset_directory(self, client, tmp_path: Path) -> None:
        payload = client.get("/api/storage").json()
        assert payload == {
            "asset_root": str(tmp_path.resolve()),
            "project_templates": None,
        }

    def test_unknown_device(self, client) -> None:
        assert client.get("/api/devices/NOPE").status_code == 404

    def test_project_and_script_lifecycle(self, client) -> None:
        assert client.post("/api/projects", json={"id": "shop", "name": "Shop"}).status_code == 201
        script = {
            "id": "flow", "name": "Flow",
            "steps": [{
                "id": "s1", "name": "Tap", "action": "tap",
                "target": {"strategy": ["coord"], "coord": {"x": 0.5, "y": 0.5}},
            }],
        }
        response = client.put("/api/projects/shop/scripts/flow", json=script)
        assert response.status_code == 200
        assert [s["id"] for s in response.json()["scripts"]] == ["flow"]
        assert client.delete("/api/projects/shop/scripts/flow").json()["scripts"] == []

    def test_script_id_mismatch_is_rejected(self, client) -> None:
        client.post("/api/projects", json={"id": "shop", "name": "Shop"})
        response = client.put(
            "/api/projects/shop/scripts/flow", json={"id": "other", "name": "X", "steps": []}
        )
        assert response.status_code == 400

    def test_invalid_step_is_rejected_with_a_reason(self, client) -> None:
        client.post("/api/projects", json={"id": "shop", "name": "Shop"})
        response = client.put("/api/projects/shop/scripts/flow", json={
            "id": "flow", "name": "Flow",
            "steps": [{"id": "s1", "name": "Tap", "action": "tap"}],  # no target
        })
        assert response.status_code == 422
        assert "target" in response.text

    def test_export_returns_content_and_a_warning_list(self, client) -> None:
        client.post("/api/projects", json={"id": "shop", "name": "Shop", "app_package": "com.shop"})
        client.put("/api/projects/shop/scripts/flow", json={
            "id": "flow", "name": "Flow",
            "steps": [{
                "id": "s1", "name": "Tap", "action": "tap",
                "target": {
                    "strategy": ["a11y", "coord"],
                    "a11y": {"resource_id": "com.shop:id/pay"},
                    "coord": {"x": 0.5, "y": 0.9},
                },
            }],
        })
        payload = client.post(
            "/api/projects/shop/scripts/flow/export", json={"exporter": "pixelforge"}
        ).json()
        assert payload["lossless"] is True
        assert payload["warnings"] == []
        assert "com.shop:id/pay" in payload["content"]

    def test_unknown_exporter_is_a_client_error(self, client) -> None:
        client.post("/api/projects", json={"id": "shop", "name": "Shop"})
        client.put("/api/projects/shop/scripts/flow",
                   json={"id": "flow", "name": "Flow", "steps": []})
        response = client.post(
            "/api/projects/shop/scripts/flow/export", json={"exporter": "nope"}
        )
        assert response.status_code == 400
        assert "available" in response.text

    def test_device_actions_require_a_session(self, client) -> None:
        # Exclusivity is not advisory: without it two operators interleave taps.
        for path, body in [
            ("/api/devices/X/tap", {"token": "t", "x": 1, "y": 1}),
            ("/api/devices/X/swipe", {
                "token": "t", "x1": 1, "y1": 1, "x2": 2, "y2": 2,
            }),
            ("/api/devices/X/crop-image", {
                "token": "t", "x": 1, "y": 1, "width": 2, "height": 2,
            }),
            ("/api/devices/X/key", {"token": "t", "keycode": 4}),
            ("/api/devices/X/text", {"token": "t", "text": "hi"}),
        ]:
            assert client.post(path, json=body).status_code == 403

    def test_exporters_and_listeners_are_listed(self, client) -> None:
        assert {e["name"] for e in client.get("/api/exporters").json()} == {"pixelforge", "pytest"}
        listeners = {item["name"]: item["status"] for item in client.get("/api/listeners").json()}
        assert listeners["logcat"] == "built"
        # Declared as planned rather than omitted, so the choice is visible.
        assert listeners["mitmproxy"] == "planned"

    def test_events_websocket_sends_a_snapshot_first(self, client) -> None:
        with client.websocket_connect("/ws/events") as socket:
            message = socket.receive_json()
        assert message["type"] == "snapshot"

    def test_timeline_websocket_replays_history(self, client) -> None:
        with client.websocket_connect("/ws/timeline") as socket:
            message = socket.receive_json()
        assert message["type"] == "history"

    def test_frontend_is_served(self, client) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "PixelForge" in response.text
