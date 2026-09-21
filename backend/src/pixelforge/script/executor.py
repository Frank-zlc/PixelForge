"""Runs a script step by step, emitting everything the timeline needs.

Each step is: wait for a precondition, locate the target, act, then assert the
result. Splitting locate from assert is what makes a failure diagnosable -- the
two failures need opposite fixes:

    locate failed   the element was never found -> stale template, wrong page
    assert failed   it was found and tapped, and nothing happened -> tap landed
                    on the wrong thing, or was intercepted

Reporting both as "step 4 failed" throws away the distinction and leaves the user
guessing. Every step therefore records a before and after capture plus the
locator's own score, so the timeline can show *which* of the two occurred.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pixelforge.geometry.mapper import Rect, Size
from pixelforge.locators.base import (
    LocateContext,
    LocateFailure,
    LocateResult,
    LocatorChain,
)
from pixelforge.script.backend import DeviceBackend
from pixelforge.script.debugger import DebugMode, Debugger
from pixelforge.script.model import (
    Assertion,
    OnFail,
    Script,
    Step,
    StepAction,
    Target,
    WaitCondition,
)
from pixelforge.timeline.bus import EventKind, TimelineBus
from pixelforge.vision.matching import diff_ratio

__all__ = ["HumanInterventionRequired", "RunResult", "RunStatus", "ScriptRunner", "StepResult"]

_VARIABLE_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_POLL_INTERVAL_S = 0.15


class RunStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    STOPPED = "stopped"
    NEEDS_HUMAN = "needs_human"


class HumanInterventionRequired(RuntimeError):
    """A step hit something automation must not attempt to defeat.

    CAPTCHAs, MFA prompts and unexpected dialogs land here. The run pauses and
    hands the device over rather than trying to get past them.
    """

    def __init__(self, step_id: str, reason: str) -> None:
        super().__init__(f"step {step_id} needs a human: {reason}")
        self.step_id = step_id
        self.reason = reason


@dataclass(slots=True)
class StepResult:
    step_id: str
    name: str
    status: RunStatus
    attempts: int = 1
    duration_ms: float = 0.0
    located: LocateResult | None = None
    error: str | None = None
    diagnosis: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "status": self.status.value,
            "attempts": self.attempts,
            "duration_ms": round(self.duration_ms, 1),
            "strategy": self.located.strategy.value if self.located else None,
            "score": round(self.located.score, 4) if self.located else None,
            "error": self.error,
            "diagnosis": self.diagnosis,
        }


@dataclass(slots=True)
class RunResult:
    run_id: str
    status: RunStatus
    steps: list[StepResult] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def failed_step(self) -> StepResult | None:
        return next(
            (s for s in self.steps if s.status in (RunStatus.FAILED, RunStatus.NEEDS_HUMAN)),
            None,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 1),
            "steps": [step.as_dict() for step in self.steps],
        }


class ScriptRunner:
    """Executes one script against one device."""

    def __init__(
        self,
        backend: DeviceBackend,
        chain: LocatorChain,
        bus: TimelineBus,
        *,
        templates_dir: Path | None = None,
        ocr_language: str = "eng",
    ) -> None:
        self._backend = backend
        self._chain = chain
        self._bus = bus
        self._templates_dir = templates_dir
        self._ocr_language = ocr_language

    async def run(
        self,
        script: Script,
        *,
        debugger: Debugger | None = None,
        variables: dict[str, str] | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        debugger = debugger or Debugger()
        run_id = run_id or uuid.uuid4().hex[:12]
        scope = {**script.variables, **(variables or {})}
        started = time.monotonic()

        self._bus.reset_origin()
        self._bus.emit(
            EventKind.RUN_START,
            run_id=run_id,
            message=script.name,
            steps=len(script.steps),
            mode=debugger.mode.value,
        )
        result = RunResult(run_id=run_id, status=RunStatus.OK)

        start_index = (
            script.index_of(debugger.start_from) if debugger.start_from else 0
        )
        for index, step in enumerate(script.steps):
            if debugger.stopping:
                result.status = RunStatus.STOPPED
                break
            if not step.enabled or not debugger.should_run(step.id, index, start_index):
                continue

            if debugger.should_break(step.id):
                debugger.pause(step.id)
                self._bus.emit(
                    EventKind.STEP_PAUSED,
                    run_id=run_id,
                    step_id=step.id,
                    message="paused -- the device is yours; resume when ready",
                )
                await debugger.wait_if_paused()
                if debugger.stopping:
                    result.status = RunStatus.STOPPED
                    break
                self._bus.emit(EventKind.STEP_RESUMED, run_id=run_id, step_id=step.id)

            step_result = await self._run_step(step, scope, run_id)
            result.steps.append(step_result)

            if step_result.status is RunStatus.NEEDS_HUMAN:
                result.status = RunStatus.NEEDS_HUMAN
                break
            if step_result.status is RunStatus.FAILED and step.on_fail is not OnFail.CONTINUE:
                result.status = RunStatus.FAILED
                break
            if debugger.mode is DebugMode.STEP_OVER:
                debugger.pause(step.id)

        result.duration_ms = (time.monotonic() - started) * 1000
        self._bus.emit(
            EventKind.RUN_END,
            run_id=run_id,
            message=result.status.value,
            duration_ms=round(result.duration_ms, 1),
            failed_step=result.failed_step.step_id if result.failed_step else None,
        )
        return result

    # ------------------------------------------------------------ one step

    async def _run_step(
        self, step: Step, scope: dict[str, str], run_id: str
    ) -> StepResult:
        outcome = StepResult(step_id=step.id, name=step.name, status=RunStatus.OK)
        started = time.monotonic()
        attempts = step.retry + 1 if step.on_fail is OnFail.RETRY else 1

        for attempt in range(1, attempts + 1):
            outcome.attempts = attempt
            self._bus.emit(
                EventKind.STEP_START,
                run_id=run_id,
                step_id=step.id,
                message=step.name,
                action=step.action.value,
                attempt=attempt,
            )
            try:
                await self._execute(step, scope, run_id, outcome)
                outcome.status = RunStatus.OK
                outcome.error = None
                break
            except HumanInterventionRequired as exc:
                outcome.status = RunStatus.NEEDS_HUMAN
                outcome.error = exc.reason
                break
            except LocateFailure as exc:
                outcome.status = RunStatus.FAILED
                outcome.error = "element not found"
                # The per-strategy report is the actionable part, so it is kept
                # separate from the one-line error.
                outcome.diagnosis = str(exc)
            except (AssertionError, TimeoutError, asyncio.TimeoutError) as exc:
                outcome.status = RunStatus.FAILED
                outcome.error = str(exc) or type(exc).__name__
            except Exception as exc:  # noqa: BLE001
                outcome.status = RunStatus.FAILED
                outcome.error = f"{type(exc).__name__}: {exc}"

            if outcome.status is RunStatus.OK or attempt >= attempts:
                break

        if outcome.status is RunStatus.FAILED and step.on_fail is OnFail.HUMAN:
            outcome.status = RunStatus.NEEDS_HUMAN

        outcome.duration_ms = (time.monotonic() - started) * 1000
        # as_dict() already carries step_id; spreading it wholesale would collide
        # with the explicit keyword.
        payload = {k: v for k, v in outcome.as_dict().items() if k != "step_id"}
        self._bus.emit(
            EventKind.STEP_END,
            run_id=run_id,
            step_id=step.id,
            message=outcome.status.value,
            **payload,
        )
        return outcome

    async def _execute(
        self, step: Step, scope: dict[str, str], run_id: str, outcome: StepResult
    ) -> None:
        if step.wait_before is not None:
            await self._wait_for(step.wait_before, run_id, step.id)

        if step.action is StepAction.WAIT:
            await asyncio.sleep((step.delay_ms or 0) / 1000)
            return
        if step.action is StepAction.LAUNCH_APP:
            await self._backend.launch_app(step.package or "")
            return
        if step.action is StepAction.STOP_APP:
            await self._backend.stop_app(step.package or "")
            return
        if step.action is StepAction.KEY:
            await self._backend.key(step.keycode or 0)
            return
        if step.action is StepAction.SCREENSHOT:
            await self._capture(run_id, step.id, "screenshot")
            return
        if step.action is StepAction.SCRIPT:
            raise HumanInterventionRequired(
                step.id,
                "inline Python steps run in the authoring SDK, not in a dispatched "
                "run; export the script or run it from the IDE",
            )

        located = await self._locate(step.target, run_id, step.id)
        outcome.located = located

        if step.action is StepAction.TAP:
            await self._backend.tap(located.box)
        elif step.action is StepAction.LONG_PRESS:
            await self._backend.long_press(located.box, step.duration_ms or 600)
        elif step.action is StepAction.SWIPE:
            display = self._backend.display
            to = step.to
            assert to is not None
            end = Rect(
                x=int(to.x * display.width) - 1,
                y=int(to.y * display.height) - 1,
                width=2,
                height=2,
            )
            await self._backend.swipe(located.box, end, step.duration_ms or 300)
        elif step.action is StepAction.INPUT_TEXT:
            await self._backend.tap(located.box)
            await self._backend.input_text(_expand(step.text or "", scope))
        elif step.action in (StepAction.WAIT_FOR, StepAction.ASSERT):
            pass  # locating it was the whole job

        self._bus.emit(
            EventKind.STEP_ACTION,
            run_id=run_id,
            step_id=step.id,
            message=f"{step.action.value} via {located.strategy.value}",
            box=[located.box.x, located.box.y, located.box.width, located.box.height],
            score=round(located.score, 4),
        )

        if step.assert_after is not None:
            await self._check(step.assert_after, run_id, step.id)

    # -------------------------------------------------------------- helpers

    async def _context(self, *, need_image: bool, need_nodes: bool) -> LocateContext:
        """Gather only what the chain will actually consult.

        A target that resolves on the accessibility tree should not pay for a
        300ms screenshot, and vice versa.
        """
        context = LocateContext(
            display=self._backend.display,
            templates_dir=self._templates_dir,
            ocr_language=self._ocr_language,
            a11y_available=self._backend.a11y_available,
        )
        if need_image:
            context.screenshot = await self._backend.screenshot()
        if need_nodes and self._backend.a11y_available:
            context.nodes = list(await self._backend.hierarchy())
        return context

    async def _locate(
        self, target: Target | None, run_id: str, step_id: str
    ) -> LocateResult:
        if target is None:
            raise AssertionError("step has no target")
        strategies = {s.value for s in target.available_strategies}
        context = await self._context(
            need_image=bool(strategies & {"template", "ocr"}),
            need_nodes="a11y" in strategies,
        )
        result = await self._chain.locate(target, context)
        self._bus.emit(
            EventKind.STEP_LOCATE,
            run_id=run_id,
            step_id=step_id,
            message=f"{result.strategy.value}: {result.detail}",
            strategy=result.strategy.value,
            score=round(result.score, 4),
            box=[result.box.x, result.box.y, result.box.width, result.box.height],
        )
        return result

    async def _wait_for(
        self, condition: WaitCondition, run_id: str, step_id: str
    ) -> None:
        deadline = time.monotonic() + condition.timeout_ms / 1000
        if condition.kind == "delay":
            await asyncio.sleep((condition.delay_ms or 0) / 1000)
            return
        if condition.kind == "screen_stable":
            await self._wait_stable(condition, deadline)
            return

        target = condition.target
        assert target is not None
        while time.monotonic() < deadline:
            strategies = {s.value for s in target.available_strategies}
            context = await self._context(
                need_image=bool(strategies & {"template", "ocr"}),
                need_nodes="a11y" in strategies,
            )
            if await self._chain.try_locate(target, context) is not None:
                return
            await asyncio.sleep(_POLL_INTERVAL_S)
        raise TimeoutError(
            f"wait_before({condition.kind}) timed out after {condition.timeout_ms}ms"
        )

    async def _wait_stable(self, condition: WaitCondition, deadline: float) -> None:
        """Wait until consecutive captures stop differing.

        Matching mid-animation finds a control halfway through a slide and scores
        badly; retrying immediately just hits the next frame of the same
        animation. Waiting for stillness first removes that whole failure mode.
        """
        previous = await self._backend.screenshot()
        stable_since: float | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL_S)
            current = await self._backend.screenshot()
            if diff_ratio(previous, current) <= condition.max_diff:
                stable_since = stable_since or time.monotonic()
                if (time.monotonic() - stable_since) * 1000 >= condition.stable_ms:
                    return
            else:
                stable_since = None
            previous = current
        raise TimeoutError(
            f"screen did not settle within {condition.timeout_ms}ms "
            f"(needed {condition.stable_ms}ms below {condition.max_diff})"
        )

    async def _check(self, assertion: Assertion, run_id: str, step_id: str) -> None:
        deadline = time.monotonic() + assertion.timeout_ms / 1000
        if assertion.kind == "screen_changed":
            first = await self._backend.screenshot()
            while time.monotonic() < deadline:
                await asyncio.sleep(_POLL_INTERVAL_S)
                if diff_ratio(first, await self._backend.screenshot()) > 0.01:
                    return
            raise AssertionError(
                assertion.message or "screen did not change after the action"
            )

        target = assertion.target
        assert target is not None
        want_present = assertion.kind != "a11y_absent"
        while time.monotonic() < deadline:
            strategies = {s.value for s in target.available_strategies}
            context = await self._context(
                need_image=bool(strategies & {"template", "ocr"}),
                need_nodes="a11y" in strategies,
            )
            found = await self._chain.try_locate(target, context) is not None
            if found == want_present:
                self._bus.emit(
                    EventKind.STEP_ASSERT,
                    run_id=run_id,
                    step_id=step_id,
                    message=f"{assertion.kind} ok",
                )
                return
            await asyncio.sleep(_POLL_INTERVAL_S)
        raise AssertionError(
            assertion.message
            or f"assertion {assertion.kind} failed after {assertion.timeout_ms}ms"
        )

    async def _capture(self, run_id: str, step_id: str, label: str) -> None:
        image = await self._backend.screenshot()
        self._bus.emit(
            EventKind.CAPTURE,
            run_id=run_id,
            step_id=step_id,
            message=label,
            width=int(image.shape[1]),
            height=int(image.shape[0]),
        )


def _expand(text: str, scope: dict[str, str]) -> str:
    """Substitute ``${name}`` from the run scope.

    An unknown name is left as written rather than replaced with an empty string:
    typing a literal '${code}' into a field is an obvious bug, whereas silently
    submitting an empty field looks like the app rejected valid input.
    """
    return _VARIABLE_RE.sub(lambda m: scope.get(m.group(1), m.group(0)), text)
