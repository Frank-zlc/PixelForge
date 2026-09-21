"""Step engine: sequencing, degradation, retry, assertions, and the debugger.

All of it against a fake device. The value of that is not speed -- it is that
these behaviours can be pinned down exactly, where on a real phone they would be
timing-dependent and only reproducible by hand.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from pixelforge.script.debugger import DebugMode, Debugger
from pixelforge.script.executor import RunStatus, ScriptRunner
from pixelforge.script.model import (
    A11ySelector,
    Assertion,
    CoordTarget,
    DeviceProfile,
    OnFail,
    Script,
    Step,
    StepAction,
    Strategy,
    Target,
    WaitCondition,
)
from pixelforge.timeline.bus import EventKind

LOGIN = Target(a11y=A11ySelector(resource_id="com.example:id/login"))
ACCOUNT = Target(a11y=A11ySelector(resource_id="com.example:id/account"))
MISSING = Target(strategy=(Strategy.A11Y,), a11y=A11ySelector(resource_id="nope"))


def runner(device, chain, bus, **kwargs) -> ScriptRunner:
    return ScriptRunner(device, chain, bus, **kwargs)


def script(*steps: Step, name: str = "flow") -> Script:
    return Script(id="s", name=name, steps=list(steps))


class TestBasicExecution:
    async def test_tap_uses_the_located_box(self, device, chain, bus) -> None:
        result = await runner(device, chain, bus).run(
            script(Step(id="a", name="log in", action=StepAction.TAP, target=LOGIN))
        )
        assert result.status is RunStatus.OK
        assert len(device.taps) == 1
        assert device.taps[0].center.rounded() == (540, 1965)

    async def test_input_text_taps_the_field_first(self, device, chain, bus) -> None:
        await runner(device, chain, bus).run(
            script(
                Step(
                    id="a",
                    name="type",
                    action=StepAction.INPUT_TEXT,
                    target=ACCOUNT,
                    text="alice@example.com",
                )
            )
        )
        assert device.taps, "a text field must be focused before typing"
        assert device.texts == ["alice@example.com"]

    async def test_variables_are_expanded(self, device, chain, bus) -> None:
        await runner(device, chain, bus).run(
            script(
                Step(
                    id="a",
                    name="type",
                    action=StepAction.INPUT_TEXT,
                    target=ACCOUNT,
                    text="${account}",
                )
            ),
            variables={"account": "bob@example.com"},
        )
        assert device.texts == ["bob@example.com"]

    async def test_unknown_variable_is_left_visible(self, device, chain, bus) -> None:
        # An empty field looks like the app rejected valid input; a literal
        # '${code}' on screen is unmistakably a script bug.
        await runner(device, chain, bus).run(
            script(
                Step(id="a", name="type", action=StepAction.INPUT_TEXT,
                     target=ACCOUNT, text="${nope}")
            )
        )
        assert device.texts == ["${nope}"]

    async def test_non_target_actions(self, device, chain, bus) -> None:
        await runner(device, chain, bus).run(
            script(
                Step(id="a", name="launch", action=StepAction.LAUNCH_APP,
                     package="com.example"),
                Step(id="b", name="back", action=StepAction.KEY, keycode=4),
                Step(id="c", name="pause", action=StepAction.WAIT, delay_ms=10),
                Step(id="d", name="stop", action=StepAction.STOP_APP,
                     package="com.example"),
            )
        )
        assert device.launched == ["com.example"]
        assert device.keys == [4]
        assert device.stopped == ["com.example"]

    async def test_disabled_steps_are_skipped(self, device, chain, bus) -> None:
        result = await runner(device, chain, bus).run(
            script(
                Step(id="a", name="on", action=StepAction.TAP, target=LOGIN),
                Step(id="b", name="off", action=StepAction.TAP, target=LOGIN,
                     enabled=False),
            )
        )
        assert [s.step_id for s in result.steps] == ["a"]

    async def test_swipe_endpoint_comes_from_normalised_to(self, device, chain, bus) -> None:
        await runner(device, chain, bus).run(
            script(
                Step(id="a", name="swipe", action=StepAction.SWIPE, target=LOGIN,
                     to=CoordTarget(x=0.5, y=0.1), duration_ms=250)
            )
        )
        (start, end, duration) = device.swipes[0]
        assert duration == 250
        assert end.center.y < start.center.y, "swiping up"


class TestCostAvoidance:
    async def test_a11y_target_does_not_take_a_screenshot(self, device, chain, bus) -> None:
        # A 300ms capture per step adds up fast; the chain should only pay for
        # what it will actually consult.
        await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN))
        )
        assert device.screenshots == 0
        assert device.hierarchy_calls == 1

    async def test_coord_only_target_needs_neither(self, device, chain, bus) -> None:
        target = Target(strategy=(Strategy.COORD,), coord=CoordTarget(x=0.5, y=0.5))
        await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=target))
        )
        assert device.screenshots == 0 and device.hierarchy_calls == 0


class TestDegradation:
    async def test_falls_through_to_coordinates(self, device, chain, bus) -> None:
        target = Target(
            a11y=A11ySelector(resource_id="not-there"),
            coord=CoordTarget(x=0.5, y=0.25),
        )
        result = await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=target))
        )
        assert result.status is RunStatus.OK
        assert result.steps[0].located.strategy is Strategy.COORD
        # The 2x2 probe box is centred exactly on the normalised point.
        assert device.taps[0].center.rounded() == (540, 600)

    async def test_prefers_a11y_when_both_are_available(self, device, chain, bus) -> None:
        target = Target(
            a11y=A11ySelector(resource_id="com.example:id/login"),
            coord=CoordTarget(x=0.1, y=0.1),
        )
        result = await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=target))
        )
        assert result.steps[0].located.strategy is Strategy.A11Y

    async def test_empty_tree_is_reported_as_a_canvas_ui(self, device, chain, bus) -> None:
        # The normal situation for a game, and the message should say so rather
        # than implying something is broken.
        device.nodes_xml = "<hierarchy/>"
        result = await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=MISSING))
        )
        assert result.status is RunStatus.FAILED
        assert "canvas or game UI" in (result.steps[0].diagnosis or "")

    async def test_aspect_mismatch_is_stated_not_swallowed(self, device, chain, bus) -> None:
        target = Target(
            strategy=(Strategy.COORD,),
            coord=CoordTarget(x=0.5, y=0.5),
            recorded_on=DeviceProfile(width=1080, height=1920),  # 16:9 vs 20:9
        )
        result = await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=target))
        )
        assert "aspect differs" in result.steps[0].located.detail


class TestFailureHandling:
    async def test_diagnosis_names_every_strategy_tried(self, device, chain, bus) -> None:
        result = await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=MISSING))
        )
        assert result.status is RunStatus.FAILED
        assert "a11y" in (result.steps[0].diagnosis or "")
        assert result.steps[0].error == "element not found"

    async def test_abort_stops_the_run(self, device, chain, bus) -> None:
        result = await runner(device, chain, bus).run(
            script(
                Step(id="a", name="bad", action=StepAction.TAP, target=MISSING),
                Step(id="b", name="never", action=StepAction.TAP, target=LOGIN),
            )
        )
        assert [s.step_id for s in result.steps] == ["a"]
        assert device.taps == []

    async def test_continue_keeps_going(self, device, chain, bus) -> None:
        result = await runner(device, chain, bus).run(
            script(
                Step(id="a", name="bad", action=StepAction.TAP, target=MISSING,
                     on_fail=OnFail.CONTINUE),
                Step(id="b", name="good", action=StepAction.TAP, target=LOGIN),
            )
        )
        assert [s.status for s in result.steps] == [RunStatus.FAILED, RunStatus.OK]
        assert len(device.taps) == 1

    async def test_retry_counts_attempts(self, device, chain, bus) -> None:
        result = await runner(device, chain, bus).run(
            script(
                Step(id="a", name="bad", action=StepAction.TAP, target=MISSING,
                     on_fail=OnFail.RETRY, retry=2)
            )
        )
        assert result.steps[0].attempts == 3

    async def test_retry_succeeds_when_the_screen_catches_up(
        self, device, chain, bus
    ) -> None:
        device.nodes_xml = "<hierarchy/>"
        device.nodes_after = {2: __import__(
            "tests.conftest", fromlist=["HIERARCHY"]).HIERARCHY}
        result = await runner(device, chain, bus).run(
            script(
                Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN,
                     on_fail=OnFail.RETRY, retry=2)
            )
        )
        assert result.status is RunStatus.OK
        assert result.steps[0].attempts == 2

    async def test_on_fail_human_pauses_rather_than_failing(
        self, device, chain, bus
    ) -> None:
        # The honest answer to a CAPTCHA: hand it over, do not try to defeat it.
        result = await runner(device, chain, bus).run(
            script(
                Step(id="a", name="captcha", action=StepAction.TAP, target=MISSING,
                     on_fail=OnFail.HUMAN)
            )
        )
        assert result.status is RunStatus.NEEDS_HUMAN

    async def test_backend_error_is_captured_not_raised(self, device, chain, bus) -> None:
        device.fail_tap_with = RuntimeError("USB fell out")
        result = await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN))
        )
        assert result.status is RunStatus.FAILED
        assert "USB fell out" in (result.steps[0].error or "")


class TestAssertions:
    async def test_assert_after_passes(self, device, chain, bus) -> None:
        result = await runner(device, chain, bus).run(
            script(
                Step(
                    id="a", name="tap", action=StepAction.TAP, target=LOGIN,
                    assert_after=Assertion(
                        kind="a11y_exists",
                        target=Target(a11y=A11ySelector(resource_id="com.example:id/home")),
                        timeout_ms=300,
                    ),
                )
            )
        )
        assert result.status is RunStatus.OK

    async def test_assert_failure_is_distinct_from_locate_failure(
        self, device, chain, bus
    ) -> None:
        """The two need opposite fixes, so they must not look the same."""
        result = await runner(device, chain, bus).run(
            script(
                Step(
                    id="a", name="tap", action=StepAction.TAP, target=LOGIN,
                    assert_after=Assertion(kind="a11y_exists", target=MISSING,
                                           timeout_ms=200,
                                           message="home screen never appeared"),
                )
            )
        )
        assert result.status is RunStatus.FAILED
        assert result.steps[0].error == "home screen never appeared"
        # The element *was* found and tapped -- that is the distinguishing fact.
        assert result.steps[0].located is not None
        assert device.taps, "the action ran; only the outcome check failed"

    async def test_a11y_absent(self, device, chain, bus) -> None:
        result = await runner(device, chain, bus).run(
            script(
                Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN,
                     assert_after=Assertion(kind="a11y_absent", target=MISSING,
                                            timeout_ms=200))
            )
        )
        assert result.status is RunStatus.OK


class TestWaitConditions:
    async def test_wait_for_element(self, device, chain, bus) -> None:
        result = await runner(device, chain, bus).run(
            script(
                Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN,
                     wait_before=WaitCondition(kind="a11y_exists", target=LOGIN,
                                               timeout_ms=500))
            )
        )
        assert result.status is RunStatus.OK

    async def test_wait_timeout_explains_itself(self, device, chain, bus) -> None:
        result = await runner(device, chain, bus).run(
            script(
                Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN,
                     wait_before=WaitCondition(kind="a11y_exists", target=MISSING,
                                               timeout_ms=200))
            )
        )
        assert result.status is RunStatus.FAILED
        assert "timed out" in (result.steps[0].error or "")

    async def test_screen_stable_waits_out_an_animation(self, device, chain, bus) -> None:
        # Matching mid-animation is a real and confusing failure mode; the engine
        # should settle first.
        rng = np.random.default_rng(3)
        moving = [rng.integers(0, 255, (200, 100, 3), dtype=np.uint8) for _ in range(3)]
        still = np.zeros((200, 100, 3), np.uint8)
        still[::7] = 200  # textured but constant
        device.frames = [*moving, still, still, still, still, still, still]
        result = await runner(device, chain, bus).run(
            script(
                Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN,
                     wait_before=WaitCondition(kind="screen_stable", timeout_ms=4000,
                                               stable_ms=200))
            )
        )
        assert result.status is RunStatus.OK
        assert device.screenshots >= 4


class TestDebugger:
    async def test_breakpoint_pauses_until_resumed(self, device, chain, bus) -> None:
        debugger = Debugger(breakpoints={"b"})
        flow = script(
            Step(id="a", name="one", action=StepAction.TAP, target=LOGIN),
            Step(id="b", name="two", action=StepAction.TAP, target=LOGIN),
        )
        task = asyncio.create_task(
            runner(device, chain, bus).run(flow, debugger=debugger)
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            if debugger.paused:
                break
        assert debugger.paused and debugger.paused_at == "b"
        assert len(device.taps) == 1, "paused before running step b"

        debugger.resume()
        result = await asyncio.wait_for(task, timeout=5)
        assert result.status is RunStatus.OK
        assert len(device.taps) == 2

    async def test_manual_takeover_while_paused(self, device, chain, bus) -> None:
        """The killer feature: drive the device by hand, then continue.

        Without it, every CAPTCHA or stray dialog means restarting the whole run.
        """
        debugger = Debugger(breakpoints={"b"})
        flow = script(
            Step(id="a", name="one", action=StepAction.TAP, target=LOGIN),
            Step(id="b", name="two", action=StepAction.TAP, target=LOGIN),
        )
        task = asyncio.create_task(
            runner(device, chain, bus).run(flow, debugger=debugger)
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            if debugger.paused:
                break
        await device.key(4)  # operator presses Back by hand
        await device.tap(__import__(
            "pixelforge.geometry.mapper", fromlist=["Rect"]).Rect(1, 1, 2, 2))
        debugger.resume()
        result = await asyncio.wait_for(task, timeout=5)
        assert result.status is RunStatus.OK
        assert device.keys == [4], "the manual key press happened"
        assert len(device.taps) == 3, "one manual tap plus two scripted"

    async def test_stop_unwinds_the_run(self, device, chain, bus) -> None:
        debugger = Debugger(breakpoints={"b"})
        flow = script(
            Step(id="a", name="one", action=StepAction.TAP, target=LOGIN),
            Step(id="b", name="two", action=StepAction.TAP, target=LOGIN),
            Step(id="c", name="three", action=StepAction.TAP, target=LOGIN),
        )
        task = asyncio.create_task(
            runner(device, chain, bus).run(flow, debugger=debugger)
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            if debugger.paused:
                break
        debugger.stop()
        result = await asyncio.wait_for(task, timeout=5)
        assert result.status is RunStatus.STOPPED
        assert len(device.taps) == 1

    async def test_run_only_executes_one_step(self, device, chain, bus) -> None:
        debugger = Debugger(mode=DebugMode.RUN_ONLY, run_only={"b"})
        flow = script(
            Step(id="a", name="one", action=StepAction.TAP, target=LOGIN),
            Step(id="b", name="two", action=StepAction.TAP, target=ACCOUNT),
            Step(id="c", name="three", action=StepAction.TAP, target=LOGIN),
        )
        result = await runner(device, chain, bus).run(flow, debugger=debugger)
        assert [s.step_id for s in result.steps] == ["b"]

    async def test_start_from_skips_earlier_steps(self, device, chain, bus) -> None:
        debugger = Debugger(start_from="c")
        flow = script(
            Step(id="a", name="one", action=StepAction.TAP, target=LOGIN),
            Step(id="b", name="two", action=StepAction.TAP, target=LOGIN),
            Step(id="c", name="three", action=StepAction.TAP, target=ACCOUNT),
        )
        result = await runner(device, chain, bus).run(flow, debugger=debugger)
        assert [s.step_id for s in result.steps] == ["c"]

    async def test_run_to_stops_before_the_named_step(self, device, chain, bus) -> None:
        debugger = Debugger(mode=DebugMode.RUN_TO, run_to="c")
        flow = script(
            Step(id="a", name="one", action=StepAction.TAP, target=LOGIN),
            Step(id="b", name="two", action=StepAction.TAP, target=LOGIN),
            Step(id="c", name="three", action=StepAction.TAP, target=LOGIN),
        )
        task = asyncio.create_task(
            runner(device, chain, bus).run(flow, debugger=debugger)
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            if debugger.paused:
                break
        assert debugger.paused_at == "c"
        assert len(device.taps) == 2
        debugger.stop()
        await asyncio.wait_for(task, timeout=5)

    def test_toggle_breakpoint(self) -> None:
        debugger = Debugger()
        assert debugger.toggle_breakpoint("a") is True
        assert debugger.toggle_breakpoint("a") is False
        assert debugger.breakpoints == set()


class TestTimeline:
    async def test_events_are_emitted_in_order(self, device, chain, bus) -> None:
        await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN))
        )
        kinds = [event.kind for event in bus.history()]
        assert kinds[0] is EventKind.RUN_START
        assert kinds[-1] is EventKind.RUN_END
        assert EventKind.STEP_LOCATE in kinds
        assert EventKind.STEP_ACTION in kinds

    async def test_locate_event_carries_the_score_and_box(self, device, chain, bus) -> None:
        await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN))
        )
        (locate,) = bus.history(kinds={EventKind.STEP_LOCATE})
        assert locate.data["strategy"] == "a11y"
        assert locate.data["box"] == [340, 1900, 400, 130]

    async def test_timestamps_are_monotonic(self, device, chain, bus) -> None:
        await runner(device, chain, bus).run(
            script(
                Step(id="a", name="one", action=StepAction.TAP, target=LOGIN),
                Step(id="b", name="two", action=StepAction.TAP, target=LOGIN),
            )
        )
        stamps = [event.monotonic for event in bus.history()]
        assert stamps == sorted(stamps)

    async def test_pause_is_visible_on_the_timeline(self, device, chain, bus) -> None:
        debugger = Debugger(breakpoints={"a"})
        task = asyncio.create_task(
            runner(device, chain, bus).run(
                script(Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN)),
                debugger=debugger,
            )
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            if debugger.paused:
                break
        debugger.resume()
        await asyncio.wait_for(task, timeout=5)
        kinds = [event.kind for event in bus.history()]
        assert EventKind.STEP_PAUSED in kinds and EventKind.STEP_RESUMED in kinds

    async def test_subscriber_receives_live_events(self, device, chain, bus) -> None:
        received = []

        async def listen() -> None:
            async with bus.subscribe() as events:
                async for event in events:
                    received.append(event.kind)
                    if event.kind is EventKind.RUN_END:
                        return

        listener = asyncio.create_task(listen())
        await asyncio.sleep(0.02)
        await runner(device, chain, bus).run(
            script(Step(id="a", name="tap", action=StepAction.TAP, target=LOGIN))
        )
        await asyncio.wait_for(listener, timeout=5)
        assert EventKind.RUN_START in received and EventKind.RUN_END in received
