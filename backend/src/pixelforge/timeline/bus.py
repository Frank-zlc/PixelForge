"""One timeline for steps, captures, network events and logs.

The question that costs the most time when debugging automation is "I tapped it,
but did the tap land, and did the app react?". A step log alone cannot answer it;
neither can a packet capture alone. Put both on one axis and the answer is
immediate:

    t=0.00  step_start  tap ok_button
    t=0.31  capture     before.png
    t=0.42  network     POST /api/market/query
    t=0.68  network     200 (1.2KB)
    t=0.95  step_end    ok

Ordering uses a monotonic clock. Wall time is recorded separately for display,
but never for ordering: an NTP correction mid-run would otherwise reorder events
or produce negative durations.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["EventKind", "TimelineBus", "TimelineEvent"]

_SUBSCRIBER_QUEUE = 512
# Enough to reconstruct a run in the UI without growing without bound on a
# long-running session.
_HISTORY_LIMIT = 4000


class EventKind(StrEnum):
    RUN_START = "run_start"
    RUN_END = "run_end"
    STEP_START = "step_start"
    STEP_LOCATE = "step_locate"
    STEP_ACTION = "step_action"
    STEP_ASSERT = "step_assert"
    STEP_END = "step_end"
    STEP_PAUSED = "step_paused"
    STEP_RESUMED = "step_resumed"
    CAPTURE = "capture"
    NETWORK = "network"
    LOG = "log"
    DEVICE = "device"


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    kind: EventKind
    monotonic: float
    wall_clock: float
    run_id: str | None = None
    step_id: str | None = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, *, origin: float = 0.0) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "t": round(self.monotonic - origin, 4),
            "wall_clock": self.wall_clock,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "message": self.message,
            "data": self.data,
        }


class TimelineBus:
    """Fan-out event bus with a bounded replay buffer."""

    def __init__(self, *, history_limit: int = _HISTORY_LIMIT) -> None:
        self._subscribers: set[asyncio.Queue[TimelineEvent]] = set()
        self._history: list[TimelineEvent] = []
        self._history_limit = history_limit
        self._origin = time.monotonic()

    @property
    def origin(self) -> float:
        return self._origin

    def reset_origin(self) -> None:
        """Re-zero the axis, so a new run's timestamps start near zero."""
        self._origin = time.monotonic()

    def emit(
        self,
        kind: EventKind,
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        message: str = "",
        **data: Any,
    ) -> TimelineEvent:
        event = TimelineEvent(
            kind=kind,
            monotonic=time.monotonic(),
            wall_clock=time.time(),
            run_id=run_id,
            step_id=step_id,
            message=message,
            data=data,
        )
        self._history.append(event)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]
        for queue in self._subscribers:
            if queue.full():
                # A stalled UI must not back-pressure step execution.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        return event

    def history(
        self, *, run_id: str | None = None, kinds: set[EventKind] | None = None
    ) -> list[TimelineEvent]:
        return [
            event
            for event in self._history
            if (run_id is None or event.run_id == run_id)
            and (kinds is None or event.kind in kinds)
        ]

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[TimelineEvent]]:
        queue: asyncio.Queue[TimelineEvent] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE)
        self._subscribers.add(queue)

        async def drain() -> AsyncIterator[TimelineEvent]:
            while True:
                yield await queue.get()

        try:
            yield drain()
        finally:
            self._subscribers.discard(queue)
