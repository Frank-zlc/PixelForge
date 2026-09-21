"""Export downgrades must be announced, never silent.

An exporter that quietly drops an assertion produces a script that passes in the
IDE and does less in production -- the worst failure mode available, because
nothing looks wrong. Every test here is really checking that the warning exists.
"""

from __future__ import annotations

import json

import pytest

from pixelforge.exporters.registry import EXPORTERS, export, list_exporters
from pixelforge.script.model import (
    A11ySelector,
    Assertion,
    CoordTarget,
    OcrTarget,
    OnFail,
    Project,
    Script,
    Step,
    StepAction,
    Strategy,
    Target,
    TemplateTarget,
)


@pytest.fixture
def project() -> Project:
    return Project(id="shop", name="Shop", app_package="com.shop")


def script(*steps: Step, script_id: str = "checkout") -> Script:
    return Script(id=script_id, name="Checkout", steps=list(steps))


TAP_RICH = Step(
    id="s2",
    name="Pay",
    action=StepAction.TAP,
    target=Target(
        a11y=A11ySelector(resource_id="com.shop:id/pay"),
        template=TemplateTarget(file="pay.png"),
        coord=CoordTarget(x=0.5, y=0.9),
    ),
)


class TestRegistry:
    def test_all_three_are_registered(self) -> None:
        assert {e["name"] for e in list_exporters()} == {"pixelforge", "albionhelper", "pytest"}

    def test_unknown_exporter_lists_the_alternatives(self, project: Project) -> None:
        with pytest.raises(KeyError, match="available"):
            export("nope", project, script())


class TestNativeExport:
    def test_round_trips_without_loss(self, project: Project) -> None:
        original = script(TAP_RICH)
        result = export("pixelforge", project, original)
        assert result.lossless
        payload = json.loads(result.content)
        restored = Script.model_validate(payload["script"])
        assert restored.steps[0].target.available_strategies == (
            Strategy.A11Y, Strategy.TEMPLATE, Strategy.COORD
        )


class TestAlbionHelperExport:
    def test_produces_its_schema_shape(self, project: Project) -> None:
        result = export("albionhelper", project, script(
            Step(id="s1", name="Launch", action=StepAction.LAUNCH_APP, package="com.shop")
        ))
        payload = json.loads(result.content)
        assert payload["schema_version"] == 1
        profile = payload["profiles"][0]
        assert profile["package_name"] == "com.shop"
        assert profile["steps"][0]["action"] == "launch"

    def test_coordinates_survive(self, project: Project) -> None:
        result = export("albionhelper", project, script(TAP_RICH))
        step = json.loads(result.content)["profiles"][0]["steps"][0]
        assert (step["x"], step["y"]) == (0.5, 0.9)

    def test_stronger_locators_are_reported_as_dropped(self, project: Project) -> None:
        result = export("albionhelper", project, script(TAP_RICH))
        lost = " ".join(w.lost for w in result.warnings)
        assert "a11y" in lost and "template" in lost
        assert "coordinate-only" in lost

    def test_unsupported_action_is_named(self, project: Project) -> None:
        result = export("albionhelper", project, script(
            Step(id="s1", name="Swipe", action=StepAction.SWIPE,
                 target=Target(coord=CoordTarget(x=0.5, y=0.8)),
                 to=CoordTarget(x=0.5, y=0.2))
        ))
        assert any("swipe" in w.lost for w in result.warnings)
        assert json.loads(result.content)["profiles"][0]["steps"] == []

    def test_assertion_loss_is_reported(self, project: Project) -> None:
        step = TAP_RICH.model_copy(update={
            "assert_after": Assertion(
                kind="a11y_exists", target=Target(a11y=A11ySelector(text="Done"))
            )
        })
        result = export("albionhelper", project, script(step))
        assert any("assertion dropped" in w.lost for w in result.warnings)

    def test_on_fail_human_is_reported(self, project: Project) -> None:
        step = TAP_RICH.model_copy(update={"on_fail": OnFail.HUMAN})
        result = export("albionhelper", project, script(step))
        assert any("on_fail" in w.lost for w in result.warnings)

    def test_workflow_name_outside_its_literal_is_reported(self, project: Project) -> None:
        result = export("albionhelper", project, script(TAP_RICH, script_id="checkout"))
        assert any("workflow name" in w.lost for w in result.warnings)
        assert json.loads(result.content)["profiles"][0]["workflow"] == "login-monitor"

    def test_known_workflow_name_passes_quietly(self, project: Project) -> None:
        result = export("albionhelper", project, script(
            Step(id="s1", name="Launch", action=StepAction.LAUNCH_APP, package="com.shop"),
            script_id="login-monitor",
        ))
        assert not any("workflow name" in w.lost for w in result.warnings)

    def test_wait_for_needs_an_ocr_pattern(self, project: Project) -> None:
        ok = export("albionhelper", project, script(
            Step(id="s1", name="Wait", action=StepAction.WAIT_FOR,
                 target=Target(strategy=(Strategy.OCR,), ocr=OcrTarget(pattern="Total")))
        ))
        assert json.loads(ok.content)["profiles"][0]["steps"][0]["expected_text"] == "Total"

        bad = export("albionhelper", project, script(
            Step(id="s1", name="Wait", action=StepAction.WAIT_FOR,
                 target=Target(a11y=A11ySelector(text="Total")))
        ))
        assert any("wait_text needs an OCR pattern" in w.lost for w in bad.warnings)

    def test_step_limit_is_enforced_and_reported(self, project: Project) -> None:
        steps = [
            Step(id=f"s{i}", name=f"Step {i}", action=StepAction.KEY, keycode=4)
            for i in range(40)
        ]
        result = export("albionhelper", project, script(*steps))
        assert len(json.loads(result.content)["profiles"][0]["steps"]) == 30
        assert any("exceeds AlbionHelper's limit" in w.lost for w in result.warnings)

    def test_duplicate_names_are_made_unique(self, project: Project) -> None:
        result = export("albionhelper", project, script(
            Step(id="a", name="Same", action=StepAction.KEY, keycode=4),
            Step(id="b", name="Same", action=StepAction.KEY, keycode=4),
        ))
        names = [s["name"] for s in json.loads(result.content)["profiles"][0]["steps"]]
        assert len(set(names)) == 2
        assert any("duplicate step name" in w.lost for w in result.warnings)

    def test_missing_package_is_reported(self) -> None:
        bare = Project(id="x", name="X")
        result = export("albionhelper", bare, script(TAP_RICH))
        assert any("package_name" in w.lost for w in result.warnings)

    def test_report_is_human_readable(self, project: Project) -> None:
        report = export("albionhelper", project, script(TAP_RICH)).report()
        assert "could not be represented" in report
        assert "step s2" in report


class TestPytestExport:
    def test_emits_a_runnable_module(self, project: Project) -> None:
        result = export("pytest", project, script(
            Step(id="s1", name="Launch", action=StepAction.LAUNCH_APP, package="com.shop"),
            TAP_RICH,
        ))
        assert result.filename == "test_checkout.py"
        compile(result.content, result.filename, "exec")  # must be valid Python

    def test_includes_every_enabled_step(self, project: Project) -> None:
        result = export("pytest", project, script(
            Step(id="s1", name="Key", action=StepAction.KEY, keycode=4),
            Step(id="s2", name="Off", action=StepAction.KEY, keycode=3, enabled=False),
        ))
        assert "pf_device.key(4)" in result.content
        assert "skipped (disabled)" in result.content
