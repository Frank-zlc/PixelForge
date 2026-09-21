"""WebSocket feed of registry events.

The frontend device list is driven from here rather than polling ``GET
/api/devices``: plug/unplug arrives from ``host:track-devices`` in milliseconds,
and polling would throw that away.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pixelforge.device.registry import DeviceRegistry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])


@router.websocket("/ws/events")
async def device_events(websocket: WebSocket) -> None:
    await websocket.accept()
    registry: DeviceRegistry = websocket.app.state.registry

    # Send a full snapshot first so the client has state without a second
    # request, then stream deltas. Without this the UI would be empty until
    # something changes, which on a stable bench is never.
    await websocket.send_json(
        {
            "type": "snapshot",
            "devices": [device.model_dump(mode="json") for device in registry.list()],
        }
    )

    try:
        async with registry.subscribe() as events:
            async for event in events:
                await websocket.send_json(
                    {
                        "type": event.kind,
                        "device": event.device.model_dump(mode="json"),
                    }
                )
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("device event stream failed")
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
