"""Export behaviour, and the plugin seam that keeps business formats out of core.

Two things are being checked. That core ships only business-agnostic exporters,
and that a downstream format can be added without touching PixelForge -- because
if the plugin path did not work, the pressure to merge one company's schema into
the registry would be irresistible, and the second company's would follow.

The rest is about warnings. An exporter that silently drops an assertion produces
a script that passes in the IDE and quietly does less in production, which is the
worst failure available: nothing looks wrong.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from pixelforge.exporters.base import ExportResult, ExportWarning
from pixelforge.exporters.registry import (
    EXPORTERS,
    export,
    list_exporters,
    load_plugin_dir,
    register,
)
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

EXAMPLE_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "examples" / "exporters"


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


@pytest.fixture
def clean_registry():
    """Restore the registry, so a plugin test cannot leak into the next one."""
    snapshot = dict(EXPORTERS)
    yield
    EXPORTERS.clear()
    EXPORTERS.update(snapshot)


class TestCoreStaysBusinessAgnostic:
    def test_only_generic_exporters_ship(self) -> None:
        # A format that targets one company's engine encodes that engine's schema.
        # Shipping one here would make the tool carry business knowledge it claims
        # not to have, and the second engine would want equal treatment.
        assert {item["name"] for item in list_exporters()} == {"pixelforge", "pytest"}

    def test_unknown_exporter_lists_the_alternatives(self, project: Project) -> None:
        with pytest.raises(KeyError, match="available"):
            export("nope", project, script())


class TestPluginSeam:
    def test_register_adds_an_exporter(self, clean_registry) -> None:
        class Dummy:
            name = "dummy"
            description = "test"

            def export(self, project, script) -> ExportResult:
                return ExportResult(filename="x.txt", content="hi")

        register(Dummy())
        assert "dummy" in EXPORTERS

    def test_register_refuses_to_shadow_silently(self, clean_registry) -> None:
        """A plugin overriding 'pixelforge' would emit the wrong file under a
        familiar name -- worse than a startup error."""

        class Impostor:
            name = "pixelforge"
            description = "not the real one"

            def export(self, project, script) -> ExportResult:
                return ExportResult(filename="x", content="")

        with pytest.raises(ValueError, match="already registered"):
            register(Impostor())
        register(Impostor(), replace=True)  # explicit override is allowed

    def test_register_rejects_a_non_exporter(self, clean_registry) -> None:
        class NoExport:
            name = "broken"

        with pytest.raises(TypeError, match="no export"):
            register(NoExport())

    def test_loads_a_plugin_from_a_directory(self, tmp_path: Path, clean_registry) -> None:
        (tmp_path / "mine.py").write_text(
            textwrap.dedent('''
                from pixelforge.exporters.base import ExportResult

                class MyEngineExporter:
                    name = "my-engine"
                    description = "my engine"

                    def export(self, project, script):
                        return ExportResult(filename="out.txt", content=script.name)

                EXPORTER = MyEngineExporter()
            ''')
        )
        assert load_plugin_dir(tmp_path) == ["my-engine"]
        assert export("my-engine", Project(id="p", name="P"), script()).content == "Checkout"

    def test_get_exporter_factory_is_also_accepted(
        self, tmp_path: Path, clean_registry
    ) -> None:
        (tmp_path / "factory.py").write_text(
            textwrap.dedent('''
                from pixelforge.exporters.base import ExportResult

                class E:
                    name = "factory-made"
                    description = ""
                    def export(self, project, script):
                        return ExportResult(filename="f", content="")

                def get_exporter():
                    return E()
            ''')
        )
        assert load_plugin_dir(tmp_path) == ["factory-made"]

    def test_a_broken_plugin_is_skipped_not_fatal(
        self, tmp_path: Path, clean_registry
    ) -> None:
        # A typo in an optional exporter must not take the whole server down.
        (tmp_path / "broken.py").write_text("this is not valid python (")
        (tmp_path / "fine.py").write_text(
            textwrap.dedent('''
                from pixelforge.exporters.base import ExportResult
                class E:
                    name = "fine"
                    description = ""
                    def export(self, project, script):
                        return ExportResult(filename="f", content="")
                EXPORTER = E()
            ''')
        )
        assert load_plugin_dir(tmp_path) == ["fine"]

    def test_module_without_an_exporter_is_skipped(
        self, tmp_path: Path, clean_registry
    ) -> None:
        (tmp_path / "nothing.py").write_text("VALUE = 1\n")
        assert load_plugin_dir(tmp_path) == []

    def test_underscore_prefixed_modules_are_ignored(
        self, tmp_path: Path, clean_registry
    ) -> None:
        (tmp_path / "_helper.py").write_text("raise RuntimeError('should not import')")
        assert load_plugin_dir(tmp_path) == []

    def test_missing_directory_is_not_fatal(self, tmp_path: Path, clean_registry) -> None:
        assert load_plugin_dir(tmp_path / "nope") == []


class TestNativeExport:
    def test_round_trips_without_loss(self, project: Project) -> None:
        result = export("pixelforge", project, script(TAP_RICH))
        assert result.lossless
        restored = Script.model_validate(json.loads(result.content)["script"])
        assert restored.steps[0].target.available_strategies == (
            Strategy.A11Y, Strategy.TEMPLATE, Strategy.COORD
        )


class TestPytestExport:
    def test_module_and_conftest_are_valid_python(self, project: Project) -> None:
        result = export("pytest", project, script(
            Step(id="s1", name="Launch", action=StepAction.LAUNCH_APP, package="com.shop"),
            TAP_RICH,
        ))
        compile(result.content, result.filename, "exec")
        assert "conftest.py" in result.media
        compile(result.media["conftest.py"].decode(), "conftest.py", "exec")

    def test_emits_no_undefined_helper(self, project: Project) -> None:
        """Regression: an earlier version called a `pf.target(...)` helper that no
        generated file imported, so the module compiled and died with NameError
        the moment CI ran it."""
        content = export("pytest", project, script(TAP_RICH)).content
        assert "pf.target" not in content
        # The target travels as a plain dict literal instead.
        assert '"resource_id": "com.shop:id/pay"' in content

    def test_target_carries_the_whole_strategy_chain(self, project: Project) -> None:
        # Honouring the order is what lets one flow run both on a device with an
        # accessibility tree and on a game canvas without one.
        content = export("pytest", project, script(TAP_RICH)).content
        assert '"strategy":' in content and '"a11y"' in content and '"coord"' in content

    def test_disabled_steps_are_marked_not_dropped(self, project: Project) -> None:
        content = export("pytest", project, script(
            Step(id="s1", name="Key", action=StepAction.KEY, keycode=4),
            Step(id="s2", name="Off", action=StepAction.KEY, keycode=3, enabled=False),
        )).content
        assert "pf_device.key(4)" in content
        assert "skipped (disabled)" in content


class TestExampleConstrainedPlugin:
    """The shipped example, loaded the way a real plugin would be.

    Doubles as the proof that lossy downgrade still reports what it lost.
    """

    @pytest.fixture(autouse=True)
    def _load(self, clean_registry):
        loaded = load_plugin_dir(EXAMPLE_PLUGIN_DIR)
        if "constrained-profile" not in loaded:
            pytest.skip("example plugin directory not present")

    def test_produces_the_constrained_shape(self, project: Project) -> None:
        result = export("constrained-profile", project, script(
            Step(id="s1", name="Launch", action=StepAction.LAUNCH_APP, package="com.shop")
        ))
        payload = json.loads(result.content)
        assert payload["schema_version"] == 1
        assert payload["profiles"][0]["steps"][0]["action"] == "launch"

    def test_coordinates_survive(self, project: Project) -> None:
        result = export("constrained-profile", project, script(TAP_RICH))
        step = json.loads(result.content)["profiles"][0]["steps"][0]
        assert (step["x"], step["y"]) == (0.5, 0.9)

    def test_dropped_locators_are_reported(self, project: Project) -> None:
        result = export("constrained-profile", project, script(TAP_RICH))
        lost = " ".join(w.lost for w in result.warnings)
        assert "a11y" in lost and "template" in lost and "coordinate-only" in lost

    def test_unsupported_action_is_named(self, project: Project) -> None:
        result = export("constrained-profile", project, script(
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
        result = export("constrained-profile", project, script(step))
        assert any("assertion dropped" in w.lost for w in result.warnings)

    def test_on_fail_human_is_reported(self, project: Project) -> None:
        step = TAP_RICH.model_copy(update={"on_fail": OnFail.HUMAN})
        result = export("constrained-profile", project, script(step))
        assert any("on_fail" in w.lost for w in result.warnings)

    def test_wait_for_needs_an_ocr_pattern(self, project: Project) -> None:
        ok = export("constrained-profile", project, script(
            Step(id="s1", name="Wait", action=StepAction.WAIT_FOR,
                 target=Target(strategy=(Strategy.OCR,), ocr=OcrTarget(pattern="Total")))
        ))
        assert json.loads(ok.content)["profiles"][0]["steps"][0]["expected_text"] == "Total"

        bad = export("constrained-profile", project, script(
            Step(id="s1", name="Wait", action=StepAction.WAIT_FOR,
                 target=Target(a11y=A11ySelector(text="Total")))
        ))
        assert any("needs an OCR pattern" in w.lost for w in bad.warnings)

    def test_step_limit_is_enforced_and_reported(self, project: Project) -> None:
        steps = [
            Step(id=f"s{i}", name=f"Step {i}", action=StepAction.KEY, keycode=4)
            for i in range(40)
        ]
        result = export("constrained-profile", project, script(*steps))
        assert len(json.loads(result.content)["profiles"][0]["steps"]) == 30
        assert any("exceeds the engine's limit" in w.lost for w in result.warnings)

    def test_duplicate_names_are_made_unique(self, project: Project) -> None:
        result = export("constrained-profile", project, script(
            Step(id="a", name="Same", action=StepAction.KEY, keycode=4),
            Step(id="b", name="Same", action=StepAction.KEY, keycode=4),
        ))
        names = [s["name"] for s in json.loads(result.content)["profiles"][0]["steps"]]
        assert len(set(names)) == 2
        assert any("duplicate step name" in w.lost for w in result.warnings)

    def test_missing_package_is_reported(self) -> None:
        result = export("constrained-profile", Project(id="x", name="X"), script(TAP_RICH))
        assert any("package name" in w.lost for w in result.warnings)

    def test_report_is_human_readable(self, project: Project) -> None:
        report = export("constrained-profile", project, script(TAP_RICH)).report()
        assert "could not be represented" in report and "step s2" in report


class TestWarningShape:
    def test_warning_names_the_step_and_the_option(self) -> None:
        warning = ExportWarning("s4", "assertion dropped", "add a following wait step")
        assert "step s4" in str(warning)
        assert "add a following wait step" in str(warning)

    def test_lossless_result_reports_so(self) -> None:
        assert ExportResult(filename="x", content="y").lossless
        assert "nothing lost" in ExportResult(filename="x", content="y").report()
