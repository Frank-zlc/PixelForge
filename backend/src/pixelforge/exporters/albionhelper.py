"""Export to AlbionHelper's ``WorkflowProfiles`` JSON.

The first downstream consumer, and a deliberate demonstration that the Exporter
seam works: AlbionHelper is a *consumer* of PixelForge, never a dependency of it.
PixelForge imports nothing from it -- this module encodes its schema as data, so
the coupling is a format, not an import.

That schema is narrower than PixelForge's on purpose (it validates with
``extra="forbid"`` and a fixed action enum, because the profiles are dispatched to
edge nodes and must not be able to carry arbitrary behaviour). It supports six
actions, OCR-only text waiting, and normalised coordinates -- no swipes, no
assertions, no accessibility selectors, no image matching.

Each of those gaps produces a warning naming the step and the options, which
doubles as the list of what AlbionHelper would have to grow to accept the script
unchanged.
"""

from __future__ import annotations

import json

from pixelforge.exporters.base import ExportResult, ExportWarning
from pixelforge.script.model import (
    OnFail,
    Project,
    Script,
    Step,
    StepAction,
    Strategy,
)

__all__ = ["AlbionHelperExporter"]

# AlbionHelper's WorkflowProfileStep action enum.
_SUPPORTED = {
    StepAction.LAUNCH_APP: "launch",
    StepAction.TAP: "tap",
    StepAction.KEY: "keyevent",
    StepAction.WAIT: "wait",
    StepAction.WAIT_FOR: "wait_text",
    StepAction.INPUT_TEXT: "input_credential",
}
_MAX_STEPS = 30
_MAX_NAME = 100
# Its `workflow` field is a Literal, so an arbitrary name is rejected on load.
_KNOWN_WORKFLOWS = ("login-monitor", "open-market-query")


class AlbionHelperExporter:
    name = "albionhelper"
    description = "AlbionHelper WorkflowProfiles JSON (lossy -- warnings explain what)"

    def export(self, project: Project, script: Script) -> ExportResult:
        warnings: list[ExportWarning] = []
        steps: list[dict[str, object]] = []

        package = project.app_package
        if not package:
            warnings.append(
                ExportWarning(
                    None,
                    "AlbionHelper profiles require package_name and the project has none",
                    "set the project's app_package before exporting",
                )
            )
            package = "com.example.app"

        workflow = script.id if script.id in _KNOWN_WORKFLOWS else _KNOWN_WORKFLOWS[0]
        if script.id not in _KNOWN_WORKFLOWS:
            warnings.append(
                ExportWarning(
                    None,
                    f"workflow name {script.id!r} is not one AlbionHelper accepts "
                    f"(its schema pins it to {list(_KNOWN_WORKFLOWS)})",
                    f"exported as {workflow!r}; rename the script or widen that Literal",
                )
            )

        enabled = [step for step in script.steps if step.enabled]
        if len(enabled) > _MAX_STEPS:
            warnings.append(
                ExportWarning(
                    None,
                    f"{len(enabled)} steps exceeds AlbionHelper's limit of {_MAX_STEPS}",
                    f"only the first {_MAX_STEPS} were exported; split the flow",
                )
            )
            enabled = enabled[:_MAX_STEPS]

        used_names: set[str] = set()
        for step in enabled:
            converted = self._convert(step, warnings, used_names)
            if converted is not None:
                steps.append(converted)

        if not steps:
            warnings.append(
                ExportWarning(
                    None,
                    "no step could be represented, and AlbionHelper requires at least one",
                    "the exported profile will not validate as-is",
                )
            )

        payload = {
            "schema_version": 1,
            "profiles": [
                {
                    "workflow": workflow,
                    "version": "1",
                    "package_name": package,
                    "steps": steps,
                }
            ],
        }
        return ExportResult(
            filename=f"{script.id}.albionhelper.json",
            content=json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            warnings=warnings,
        )

    def _convert(
        self, step: Step, warnings: list[ExportWarning], used_names: set[str]
    ) -> dict[str, object] | None:
        action = _SUPPORTED.get(step.action)
        if action is None:
            warnings.append(
                ExportWarning(
                    step.id,
                    f"action {step.action.value!r} has no AlbionHelper equivalent",
                    "drop the step, replace it with a supported action, or extend "
                    "AlbionHelper's action enum",
                )
            )
            return None

        # Its step names must be unique and <=100 chars.
        name = step.name[:_MAX_NAME]
        if name in used_names:
            suffix = f" ({step.id})"
            name = name[: _MAX_NAME - len(suffix)] + suffix
            warnings.append(
                ExportWarning(
                    step.id,
                    "duplicate step name (AlbionHelper requires unique names)",
                    f"renamed to {name!r}",
                )
            )
        used_names.add(name)

        out: dict[str, object] = {
            "name": name,
            "action": action,
            "timeout_seconds": min(600.0, max(0.001, step.timeout_ms / 1000)),
            "max_attempts": max(1, min(10, step.retry + 1)),
        }

        if step.action is StepAction.KEY:
            if step.keycode is not None and step.keycode > 999:
                warnings.append(
                    ExportWarning(
                        step.id,
                        f"keycode {step.keycode} exceeds AlbionHelper's 0-999 range",
                        "use a keycode within range",
                    )
                )
                return None
            out["key_code"] = step.keycode
            return out

        if step.action is StepAction.WAIT:
            out["wait_seconds"] = min(60.0, max(0.05, (step.delay_ms or 50) / 1000))
            return out

        if step.action is StepAction.LAUNCH_APP:
            return out

        # The remaining actions need a location or a text pattern.
        target = step.target
        if target is None:
            return out

        available = set(target.available_strategies)
        if step.action is StepAction.WAIT_FOR:
            if target.ocr is None:
                warnings.append(
                    ExportWarning(
                        step.id,
                        "wait_text needs an OCR pattern and this target has none "
                        f"(it uses {target.describe()})",
                        "add an OCR pattern to the target, or drop the step",
                    )
                )
                return None
            out["expected_text"] = target.ocr.pattern[:200]
            return out

        if target.coord is None:
            warnings.append(
                ExportWarning(
                    step.id,
                    f"AlbionHelper locates only by coordinate, and this target uses "
                    f"{target.describe()}",
                    "re-record the step so it also captures a coordinate",
                )
            )
            return None

        out["x"] = round(target.coord.x, 6)
        out["y"] = round(target.coord.y, 6)

        # Anything above coordinates in the chain is a robustness feature that
        # simply will not travel.
        stronger = available - {Strategy.COORD}
        if stronger:
            warnings.append(
                ExportWarning(
                    step.id,
                    f"{', '.join(sorted(s.value for s in stronger))} locator(s) dropped; "
                    "the exported step is coordinate-only",
                    "it will work on this device's resolution and may miss on others",
                )
            )

        if step.action is StepAction.INPUT_TEXT:
            # Its input action reads from a keychain alias rather than carrying
            # text, which is a deliberate security choice on its side.
            warnings.append(
                ExportWarning(
                    step.id,
                    "input_credential reads from AlbionHelper's keychain and cannot "
                    "carry literal text",
                    "exported as credential_field='username'; set the profile's "
                    "credential_alias and adjust if this field is the password",
                )
            )
            out["credential_field"] = "username"

        if step.assert_after is not None:
            warnings.append(
                ExportWarning(
                    step.id,
                    "assertion dropped (AlbionHelper steps do not verify outcomes)",
                    "failures will surface at a later step instead of this one",
                )
            )
        if step.wait_before is not None and step.wait_before.kind != "delay":
            warnings.append(
                ExportWarning(
                    step.id,
                    f"wait_before({step.wait_before.kind}) dropped",
                    "insert a preceding wait_text or wait step instead",
                )
            )
        if step.on_fail in (OnFail.HUMAN, OnFail.CONTINUE):
            warnings.append(
                ExportWarning(
                    step.id,
                    f"on_fail={step.on_fail.value!r} dropped; AlbionHelper only retries",
                    "its WorkflowRunner raises HumanActionRequired on its own triggers",
                )
            )
        return out
