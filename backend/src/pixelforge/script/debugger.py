"""Step-level debugger.

Made cheap by a deliberate modelling choice: steps are *data*, so a breakpoint is
a set membership test and stepping is an index. Implementing the same features
over free-form Python would mean ``sys.settrace`` or a debug-adapter integration.

The feature that matters most in practice is :meth:`pause`: stop mid-run, take the
device by hand, nudge the flow past whatever the script cannot handle -- a CAPTCHA,
an unexpected dialog, an SMS code -- then resume. Without it, every such
interruption means restarting the whole run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["DebugMode", "Debugger", "RunControl"]


class DebugMode(StrEnum):
    RUN = "run"
    STEP_OVER = "step_over"
    RUN_TO = "run_to"
    RUN_ONLY = "run_only"


class RunControl(StrEnum):
    CONTINUE = "continue"
    PAUSE = "pause"
    STOP = "stop"


@dataclass
class Debugger:
    """Breakpoints and pause/resume for one run."""

    mode: DebugMode = DebugMode.RUN
    breakpoints: set[str] = field(default_factory=set)
    run_to: str | None = None
    run_only: set[str] = field(default_factory=set)
    start_from: str | None = None

    _resume: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _paused_at: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._resume.set()

    # ----------------------------------------------------------- inspection

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    @property
    def paused_at(self) -> str | None:
        return self._paused_at

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def should_run(self, step_id: str, index: int, start_index: int) -> bool:
        """Whether this step executes at all under the current mode."""
        if index < start_index:
            return False
        if self.mode is DebugMode.RUN_ONLY:
            return step_id in self.run_only
        return True

    def should_break(self, step_id: str) -> bool:
        """Whether to stop *before* running this step."""
        if self.mode is DebugMode.STEP_OVER:
            return True
        if self.mode is DebugMode.RUN_TO and step_id == self.run_to:
            return True
        return step_id in self.breakpoints

    # -------------------------------------------------------------- control

    def pause(self, step_id: str | None = None) -> None:
        self._paused_at = step_id
        self._resume.clear()

    def resume(self) -> None:
        self._paused_at = None
        self._resume.set()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()  # unblock anything waiting, so the run can unwind

    async def wait_if_paused(self) -> None:
        """Block while paused. The device is free for manual use meanwhile."""
        await self._resume.wait()

    def toggle_breakpoint(self, step_id: str) -> bool:
        if step_id in self.breakpoints:
            self.breakpoints.discard(step_id)
            return False
        self.breakpoints.add(step_id)
        return True
