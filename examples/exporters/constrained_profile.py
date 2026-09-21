"""Example plugin: downgrade to a constrained step profile.

Copy this, adjust the constants to your engine's conventions, and drop it in a
directory named by ``PIXELFORGE_EXPORTER_PLUGINS``.

The target format here is a shape that turns up a lot in practice, for good
reasons: a profile dispatched to edge nodes should not be able to carry arbitrary
behaviour, so it is locked down to a fixed action enum with ``extra="forbid"``
validation. Six actions, normalised coordinates only, OCR-only text waiting,
unique step names, at most 30 steps.

What that costs is expressiveness, and the whole point of this file is what it
does about that: **every downgrade produces a warning naming the step, what could
not be represented, and the options**. An exporter that silently dropped an
assertion would produce a script that passes in the IDE and quietly does less in
production, which is the worst available failure -- nothing looks wrong.

The warnings are useful in the other direction too: they are a list of what the
target engine would have to grow to accept the flow unchanged.
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

# --- adjust these to your engine -------------------------------------------

_ACTION_MAP = {
    StepAction.LAUNCH_APP: "launch",
    StepAction.TAP: "tap",
    StepAction.KEY: "keyevent",
    StepAction.WAIT: "wait",
    StepAction.WAIT_FOR: "wait_text",
    StepAction.INPUT_TEXT: "input_credential",
}
_MAX_STEPS = 30
_MAX_NAME = 100
_MAX_KEYCODE = 999
# Some engines pin the profile name to a Literal, so an arbitrary script id is
# rejected at load time. Set to () if yours accepts any name.
_WORKFLOW_NAMES: tuple[str, ...] = ("login-monitor", "open-market-query")

# ---------------------------------------------------------------------------


class ConstrainedProfileExporter:
    name = "constrained-profile"
    description = "Constrained step profile (lossy -- warnings explain what)"

    def export(self, project: Project, script: Script) -> ExportResult:
        warnings: list[ExportWarning] = []
        steps: list[dict[str, object]] = []

        package = project.app_package
        if not package:
            warnings.append(
                ExportWarning(
                    None,
                    "the profile requires a package name and the project has none",
                    "set the project's app_package before exporting",
                )
            )
            package = "com.example.app"

        workflow = script.id
        if _WORKFLOW_NAMES and script.id not in _WORKFLOW_NAMES:
            workflow = _WORKFLOW_NAMES[0]
            warnings.append(
                ExportWarning(
                    None,
                    f"workflow name {script.id!r} is not one the engine accepts "
                    f"(it pins the field to {list(_WORKFLOW_NAMES)})",
                    f"exported as {workflow!r}; rename the script or widen that Literal",
                )
            )

        enabled = [step for step in script.steps if step.enabled]
        if len(enabled) > _MAX_STEPS:
            warnings.append(
                ExportWarning(
                    None,
                    f"{len(enabled)} steps exceeds the engine's limit of {_MAX_STEPS}",
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
                    "no step could be represented, and the engine requires at least one",
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
            filename=f"{script.id}.profile.json",
            content=json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            warnings=warnings,
        )

    def _convert(
        self, step: Step, warnings: list[ExportWarning], used_names: set[str]
    ) -> dict[str, object] | None:
        action = _ACTION_MAP.get(step.action)
        if action is None:
            warnings.append(
                ExportWarning(
                    step.id,
                    f"action {step.action.value!r} has no equivalent in the engine",
                    "drop the step, replace it with a supported action, or extend "
                    "the engine's action enum",
                )
            )
            return None

        name = step.name[:_MAX_NAME]
        if name in used_names:
            suffix = f" ({step.id})"
            name = name[: _MAX_NAME - len(suffix)] + suffix
            warnings.append(
                ExportWarning(
                    step.id,
                    "duplicate step name (the engine requires unique names)",
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
            if step.keycode is not None and step.keycode > _MAX_KEYCODE:
                warnings.append(
                    ExportWarning(
                        step.id,
                        f"keycode {step.keycode} exceeds the engine's 0-{_MAX_KEYCODE} range",
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

        target = step.target
        if target is None:
            return out

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
                    "the engine locates only by coordinate, and this target uses "
                    f"{target.describe()}",
                    "re-record the step so it also captures a coordinate",
                )
            )
            return None

        out["x"] = round(target.coord.x, 6)
        out["y"] = round(target.coord.y, 6)

        # Everything above coordinates in the chain is a robustness feature that
        # simply will not travel to a coordinate-only engine.
        stronger = set(target.available_strategies) - {Strategy.COORD}
        if stronger:
            warnings.append(
                ExportWarning(
                    step.id,
                    f"{', '.join(sorted(s.value for s in stronger))} locator(s) dropped; "
                    "the exported step is coordinate-only",
                    "it will work at this device's resolution and may miss on others",
                )
            )

        if step.action is StepAction.INPUT_TEXT:
            warnings.append(
                ExportWarning(
                    step.id,
                    "input_credential reads from the engine's own credential store "
                    "and cannot carry literal text",
                    "exported as credential_field='username'; set the profile's "
                    "credential alias and adjust if this field is the password",
                )
            )
            out["credential_field"] = "username"

        if step.assert_after is not None:
            warnings.append(
                ExportWarning(
                    step.id,
                    "assertion dropped (the engine's steps do not verify outcomes)",
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
                    f"on_fail={step.on_fail.value!r} dropped; the engine only retries",
                    "handle the case in the engine's own interruption mechanism",
                )
            )
        return out


EXPORTER = ConstrainedProfileExporter()
