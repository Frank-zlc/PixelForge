from __future__ import annotations

from types import SimpleNamespace

import pytest

from pixelforge.api import devices as devices_api
from pixelforge.listeners import manager as manager_module
from pixelforge.listeners.manager import ListenerManager
from pixelforge.script.model import ListenerConfig, Project
from pixelforge.timeline.bus import TimelineBus


class FakeListener:
    name = "fake"
    description = "fake listener"

    def __init__(self) -> None:
        self.running = False
        self.serial: str | None = None

    async def start(self, context) -> None:
        self.serial = context.serial
        self.running = True

    async def stop(self) -> None:
        self.running = False


@pytest.mark.asyncio
async def test_listener_follows_session_lifecycle(monkeypatch) -> None:
    listener = FakeListener()
    monkeypatch.setattr(manager_module, "build_listener", lambda *args, **kwargs: listener)
    manager = ListenerManager(object(), TimelineBus())  # type: ignore[arg-type]

    statuses = await manager.start(
        "logical-serial",
        adb_serial="10.0.0.5:7400",
        configs=[ListenerConfig(name="fake")],
        app_package="com.example",
    )
    assert statuses[0].running is True
    assert listener.serial == "10.0.0.5:7400"

    await manager.stop("logical-serial")
    assert listener.running is False
    assert manager.statuses("logical-serial") == []


def test_projects_enable_logcat_by_default() -> None:
    project = Project(id="demo", name="Demo")
    assert [listener.name for listener in project.listeners] == ["logcat"]


@pytest.mark.asyncio
async def test_logcat_can_start_without_a_project(monkeypatch) -> None:
    calls = []

    async def fake_start(serial, *, adb_serial, configs, app_package):
        calls.append((serial, adb_serial, configs, app_package))
        return [SimpleNamespace(as_dict=lambda: {"name": "logcat", "running": True, "error": None})]

    monkeypatch.setattr(devices_api, "require_lease", lambda *_args: None)
    sessions = SimpleNamespace(get=lambda serial: SimpleNamespace(serial="device-serial"))
    listeners = SimpleNamespace(start=fake_start)
    result = await devices_api.start_listeners(
        "logical-serial",
        devices_api.ListenerStartRequest(token="token"),
        object(), sessions, listeners, object(),
    )
    assert result == [{"name": "logcat", "running": True, "error": None}]
    assert calls[0][0:2] == ("logical-serial", "device-serial")
    assert [item.name for item in calls[0][2]] == ["logcat"]
    assert calls[0][3] is None
