"""Helpers for send-only WebSockets that must still notice client disconnects."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

from fastapi import WebSocket

T = TypeVar("T")


async def relay_until_disconnect(
    websocket: WebSocket,
    source: AsyncIterator[T],
    send: Callable[[T], Awaitable[None]],
) -> None:
    """Relay ``source`` while concurrently consuming the disconnect frame.

    Waiting only on ``source`` leaves an idle socket handler alive forever when
    a browser tab closes. Uvicorn then cannot finish a graceful shutdown until
    another device/timeline event happens to wake the iterator.
    """
    next_item = asyncio.create_task(anext(source))
    incoming = asyncio.create_task(websocket.receive())
    try:
        while True:
            done, _ = await asyncio.wait(
                {next_item, incoming}, return_when=asyncio.FIRST_COMPLETED
            )
            if incoming in done:
                message = incoming.result()
                if message["type"] == "websocket.disconnect":
                    return
                # These feeds are server-to-client. Ignore unexpected client
                # messages, but keep consuming so disconnect remains observable.
                incoming = asyncio.create_task(websocket.receive())
            if next_item in done:
                try:
                    item = next_item.result()
                except StopAsyncIteration:
                    return
                await send(item)
                next_item = asyncio.create_task(anext(source))
    finally:
        for task in (next_item, incoming):
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await task
