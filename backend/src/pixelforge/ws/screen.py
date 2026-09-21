"""Live video and control over WebSocket.

Video is forwarded as raw H.264 packets and decoded by the browser's
``VideoDecoder``. The backend never decodes: that keeps per-device CPU near zero,
which is what makes several phones from one process viable.

A joining client is first sent the retained parameter sets and newest keyframe.
Without them a decoder has nothing to start from and the canvas stays black until
the next IDR -- which on a quiet screen can be many seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pixelforge.device.manager import SessionManager
from pixelforge.device.scrcpy.control import (
    Action,
    MotionAction,
    encode_key,
    encode_text,
    encode_touch,
)
from pixelforge.device.lease import LeaseManager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stream"])


@router.websocket("/ws/screen/{serial}")
async def screen(websocket: WebSocket, serial: str) -> None:
    await websocket.accept()
    sessions: SessionManager = websocket.app.state.sessions
    session = sessions.get(serial)
    if session is None:
        await websocket.send_json({"type": "error", "message": f"no session for {serial}"})
        await websocket.close(code=1008)
        return
    if not session.scrcpy.state.started:
        await websocket.send_json(
            {
                "type": "error",
                "message": "video is unavailable on this device",
                "notes": session.notes,
            }
        )
        await websocket.close(code=1011)
        return

    state = session.scrcpy.state
    codec = state.codec
    await websocket.send_json(
        {
            "type": "config",
            "codec": codec.webcodecs_id if codec else None,
            "frame": list(state.frame.as_tuple()) if state.frame else None,
            "display": list(session.display.as_tuple()),
            "rotation": int(session.mapper.rotation),
            "device_name": state.device_name,
        }
    )

    try:
        async with session.scrcpy.subscribe() as packets:
            async for packet in packets:
                # One-byte prefix so the client knows whether this is a parameter
                # set, a keyframe or a delta without parsing NAL headers itself.
                flags = (0x01 if packet.is_config else 0) | (
                    0x02 if packet.is_keyframe else 0
                )
                await websocket.send_bytes(bytes([flags]) + packet.data)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("screen stream failed for %s", serial)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)


@router.websocket("/ws/control/{serial}")
async def control(websocket: WebSocket, serial: str) -> None:
    """Low-latency control channel.

    Separate from the REST tap endpoint because dragging emits a MOVE event every
    few milliseconds, and an HTTP round trip per event would make it feel like
    treacle.
    """
    await websocket.accept()
    sessions: SessionManager = websocket.app.state.sessions
    leases: LeaseManager = websocket.app.state.leases
    session = sessions.get(serial)
    if session is None:
        await websocket.send_json({"type": "error", "message": f"no session for {serial}"})
        await websocket.close(code=1008)
        return

    try:
        first = await websocket.receive_json()
    except (WebSocketDisconnect, json.JSONDecodeError):
        return
    token = str(first.get("token", ""))
    lease = leases.current(serial)
    if lease is None or lease.token != token:
        await websocket.send_json(
            {"type": "error", "message": "a valid device lease is required"}
        )
        await websocket.close(code=1008)
        return
    await websocket.send_json({"type": "ready", "frame": list(session.mapper.frame.as_tuple())})

    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            # Coordinates arrive in frame space; scrcpy rescales on the device,
            # so nothing is converted here.
            if kind in {"down", "move", "up"}:
                action = {
                    "down": MotionAction.DOWN,
                    "move": MotionAction.MOVE,
                    "up": MotionAction.UP,
                }[kind]
                current = session.mapper.frame
                await session.scrcpy.send(
                    encode_touch(
                        action,
                        float(message["x"]),
                        float(message["y"]),
                        current.width,
                        current.height,
                        pointer_id=int(message.get("pointer", -2)),
                    )
                )
            elif kind == "key":
                code = int(message["keycode"])
                await session.scrcpy.send(
                    encode_key(Action.DOWN, code), encode_key(Action.UP, code)
                )
            elif kind == "text":
                messages = encode_text(str(message["text"]))
                if messages:
                    await session.scrcpy.send(*messages)
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})
    except (WebSocketDisconnect, KeyError, ValueError, json.JSONDecodeError):
        return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("control channel failed for %s", serial)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
