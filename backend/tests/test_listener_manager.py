from __future__ import annotations

import pytest

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
