"""Live timeline: steps, captures, network events and logs on one axis."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pixelforge.timeline.bus import TimelineBus
from pixelforge.ws.subscription import relay_until_disconnect

logger = logging.getLogger(__name__)
router = APIRouter(tags=["timeline"])


@router.websocket("/ws/timeline")
async def timeline(websocket: WebSocket) -> None:
    await websocket.accept()
    bus: TimelineBus = websocket.app.state.bus
    # Replay recent history first: a client that connects after a run started
    # should still be able to draw it.
    await websocket.send_json(
        {
            "type": "history",
            "events": [event.as_dict(origin=bus.origin) for event in bus.history()[-400:]],
        }
    )
    try:
        async with bus.subscribe() as events:
            async def send(event) -> None:
                await websocket.send_json(
                    {"type": "event", "event": event.as_dict(origin=bus.origin)}
                )
            await relay_until_disconnect(websocket, events, send)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("timeline stream failed")
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
