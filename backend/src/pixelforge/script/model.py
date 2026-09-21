"""Step, Target and Project: what a recorded flow actually is.

The central idea is :class:`Target`. A step does not say "tap (0.48, 0.67)" -- it
says "tap this thing", and carries *four* independent descriptions of the thing,
captured in one pass at record time:

    a11y      resource-id / text / description   stable across devices
    template  a lossless crop + search region     works where a11y is empty
    ocr       a text pattern and where to look    works where both fail
    coord     normalised x,y                      last resort

At replay the strategies are tried in order and the first hit wins. That ordering
is the whole mechanism by which one tool covers both a normal app (rich
accessibility tree) and a game (canvas, no tree at all) without asking the user
to pick a mode. Recording all four costs one extra screenshot; recording only
coordinates is what makes scripts break on the next device.

``Project`` is the isolation unit. Different businesses share the engine and share
nothing else -- own device filter, own template directory, own variables. Adding
a second business must never mean touching the first one's steps, and must never
mean a branch in the engine.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

__all__ = [
    "A11ySelector",
    "Assertion",
    "CoordTarget",
    "DeviceProfile",
    "ListenerConfig",
    "OcrTarget",
    "OnFail",
    "Project",
    "Script",
    "Step",
    "StepAction",
    "Strategy",
    "Target",
    "TemplateTarget",
    "WaitCondition",
]

Normalised = Annotated[float, Field(ge=0.0, le=1.0)]


class Strategy(StrEnum):
    A11Y = "a11y"
    TEMPLATE = "template"
    OCR = "ocr"
    COORD = "coord"


class StepAction(StrEnum):
    TAP = "tap"
    LONG_PRESS = "long_press"
    SWIPE = "swipe"
    GESTURE = "gesture"
    INPUT_TEXT = "input_text"
    KEY = "key"
    LAUNCH_APP = "launch_app"
    STOP_APP = "stop_app"
    WAIT = "wait"
    WAIT_FOR = "wait_for"
    ASSERT = "assert"
    SCREENSHOT = "screenshot"
    SCRIPT = "script"


class OnFail(StrEnum):
    ABORT = "abort"
    RETRY = "retry"
    CONTINUE = "continue"
    # Pause and hand the device to the operator. The honest answer to a CAPTCHA
    # or an SMS code: PixelForge does not try to defeat either.
    HUMAN = "human"


class DeviceProfile(BaseModel):
    """The device a step was recorded on.

    Kept with every target so a replay can say *why* it is unreliable -- "recorded
    at 1080x2400, running on 1440x3200" is a diagnosis; "template not found" is
    not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    density: int | None = Field(default=None, gt=0)
    rotation: int = Field(default=0, ge=0, le=3)
    sdk_int: int | None = Field(default=None, gt=0)
    model: str | None = None

    @property
    def aspect(self) -> float:
        return self.width / self.height


class A11ySelector(BaseModel):
    """Accessibility-tree selector. The most portable strategy when available."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_id: str | None = None
    text: str | None = None
    text_contains: str | None = None
    content_desc: str | None = None
    class_name: str | None = None
    package: str | None = None
    index: int | None = Field(default=None, ge=0)
    clickable: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> A11ySelector:
        if not any(
            getattr(self, field) is not None
            for field in (
                "resource_id",
                "text",
                "text_contains",
                "content_desc",
                "class_name",
            )
        ):
            raise ValueError("an a11y selector needs at least one identifying field")
        return self

    def describe(self) -> str:
        parts = [
            f"{name}={value!r}"
            for name, value in self.model_dump(exclude_none=True).items()
        ]
        return " ".join(parts)


class TemplateTarget(BaseModel):
    """A lossless image crop plus how to search for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(min_length=1, description="path relative to the project's templates dir")
    threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    roi: tuple[Normalised, Normalised, Normalised, Normalised] | None = Field(
        default=None,
        description="normalised (left, top, right, bottom) search region",
    )
    mask: str | None = Field(
        default=None,
        description="optional mask image; black pixels are excluded from scoring, "
        "which is how a button with a changing badge inside it stays matchable",
    )
    multi_scale: bool = Field(
        default=True,
        description="search a +/-20% scale pyramid, for cross-resolution replay",
    )

    @model_validator(mode="after")
    def _roi_ordered(self) -> TemplateTarget:
        if self.roi is not None:
            left, top, right, bottom = self.roi
            if right <= left or bottom <= top:
                raise ValueError(f"roi must have positive area, got {self.roi}")
        return self


class OcrTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern: str = Field(min_length=1, description="regex matched against recognised words")
    language: str = "eng"
    roi: tuple[Normalised, Normalised, Normalised, Normalised] | None = None
    min_confidence: float = Field(default=60.0, ge=0.0, le=100.0)


class CoordTarget(BaseModel):
    """Normalised coordinates. Works only on a device shaped like the recorder's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: Normalised
    y: Normalised


class Target(BaseModel):
    """A thing on screen, described four ways.

    ``strategy`` is the order to try, not a single choice. Any listed strategy
    whose data is missing is skipped rather than failing the step, so a target can
    be recorded on a screen with no accessibility tree and still declare the
    preferred order for the day it gains one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: tuple[Strategy, ...] = (
        Strategy.A11Y,
        Strategy.TEMPLATE,
        Strategy.OCR,
        Strategy.COORD,
    )
    a11y: A11ySelector | None = None
    template: TemplateTarget | None = None
    ocr: OcrTarget | None = None
    coord: CoordTarget | None = None
    recorded_on: DeviceProfile | None = None

    @model_validator(mode="after")
    def _has_usable_strategy(self) -> Target:
        if not self.strategy:
            raise ValueError("a target needs at least one strategy")
        if not self.available_strategies:
            raise ValueError(
                "none of the listed strategies has data: "
                f"strategy={[s.value for s in self.strategy]} but every "
                "corresponding field is empty"
            )
        return self

    @property
    def available_strategies(self) -> tuple[Strategy, ...]:
        """Listed strategies that actually carry data, in the listed order."""
        return tuple(
            strategy
            for strategy in self.strategy
            if getattr(self, strategy.value) is not None
        )

    @property
    def coord_only(self) -> bool:
        """True when only coordinates are available.

        Surfaced in the UI: a coordinate-only step is the one that will break on
        the next device, and the user deserves to be told at record time rather
        than at 3am in CI.
        """
        return self.available_strategies == (Strategy.COORD,)

    def describe(self) -> str:
        return "->".join(s.value for s in self.available_strategies)


class WaitCondition(BaseModel):
    """Something to be true before the step acts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["screen_stable", "a11y_exists", "ocr_text", "template", "delay"]
    timeout_ms: int = Field(default=5000, gt=0, le=600_000)
    # screen_stable
    stable_ms: int = Field(default=400, gt=0, le=60_000)
    max_diff: float = Field(default=0.002, ge=0.0, le=1.0)
    # a11y_exists / ocr_text / template
    target: Target | None = None
    # delay
    delay_ms: int | None = Field(default=None, gt=0, le=600_000)

    @model_validator(mode="after")
    def _kind_fields(self) -> WaitCondition:
        if self.kind in {"a11y_exists", "ocr_text", "template"} and self.target is None:
            raise ValueError(f"wait kind {self.kind!r} requires a target")
        if self.kind == "delay" and self.delay_ms is None:
            raise ValueError("wait kind 'delay' requires delay_ms")
        return self


class Assertion(BaseModel):
    """A check after the step, so a failure is attributed to the right step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["a11y_exists", "a11y_absent", "ocr_text", "template", "screen_changed"]
    target: Target | None = None
    timeout_ms: int = Field(default=5000, gt=0, le=600_000)
    message: str | None = None

    @model_validator(mode="after")
    def _kind_fields(self) -> Assertion:
        if self.kind != "screen_changed" and self.target is None:
            raise ValueError(f"assertion {self.kind!r} requires a target")
        return self


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    action: StepAction
    target: Target | None = None
    wait_before: WaitCondition | None = None
    assert_after: Assertion | None = None
    on_fail: OnFail = OnFail.ABORT
    retry: int = Field(default=0, ge=0, le=10)
    timeout_ms: int = Field(default=10_000, gt=0, le=600_000)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=2000)

    # action-specific
    text: str | None = None
    keycode: int | None = Field(default=None, ge=0, le=0xFFFF)
    package: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.]{0,254}$")
    to: CoordTarget | None = Field(default=None, description="swipe/gesture endpoint")
    duration_ms: int | None = Field(default=None, ge=10, le=60_000)
    delay_ms: int | None = Field(default=None, ge=0, le=600_000)
    code: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def _action_requirements(self) -> Step:
        needs_target = {
            StepAction.TAP,
            StepAction.LONG_PRESS,
            StepAction.SWIPE,
            StepAction.INPUT_TEXT,
            StepAction.WAIT_FOR,
            StepAction.ASSERT,
        }
        if self.action in needs_target and self.target is None:
            raise ValueError(f"action {self.action.value!r} requires a target")
        if self.action is StepAction.INPUT_TEXT and self.text is None:
            raise ValueError("input_text requires text")
        if self.action is StepAction.KEY and self.keycode is None:
            raise ValueError("key requires keycode")
        if self.action in {StepAction.LAUNCH_APP, StepAction.STOP_APP} and not self.package:
            raise ValueError(f"{self.action.value} requires package")
        if self.action is StepAction.SWIPE and self.to is None:
            raise ValueError("swipe requires a 'to' endpoint")
        if self.action is StepAction.WAIT and self.delay_ms is None:
            raise ValueError("wait requires delay_ms")
        if self.action is StepAction.SCRIPT and not self.code:
            raise ValueError("script requires code")
        if self.on_fail is OnFail.RETRY and self.retry == 0:
            raise ValueError("on_fail='retry' needs retry >= 1")
        return self


class Script(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    steps: list[Step] = Field(default_factory=list, max_length=500)
    variables: dict[str, str] = Field(default_factory=dict)
    recorded_on: DeviceProfile | None = None

    @model_validator(mode="after")
    def _unique_step_ids(self) -> Script:
        ids = [step.id for step in self.steps]
        duplicates = {value for value in ids if ids.count(value) > 1}
        if duplicates:
            raise ValueError(f"duplicate step ids: {sorted(duplicates)}")
        return self

    @property
    def coord_only_steps(self) -> list[str]:
        """Steps that will break on a differently shaped device."""
        return [
            step.id
            for step in self.steps
            if step.target is not None and step.target.coord_only
        ]

    def index_of(self, step_id: str) -> int:
        for index, step in enumerate(self.steps):
            if step.id == step_id:
                return index
        raise KeyError(f"unknown step id: {step_id}")


class DeviceFilter(BaseModel):
    """Which devices a project may drive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    serials: tuple[str, ...] = ()
    model_contains: str | None = None
    min_sdk: int | None = Field(default=None, gt=0)

    def matches(self, *, serial: str, model: str | None, sdk_int: int | None) -> bool:
        if self.serials and serial not in self.serials:
            return False
        if self.model_contains and (
            model is None or self.model_contains.lower() not in model.lower()
        ):
            return False
        return not (self.min_sdk is not None and (sdk_int is None or sdk_int < self.min_sdk))


class ListenerConfig(BaseModel):
    """One passive data source attached to a project session.

    Listener options stay JSON-compatible so third-party listeners can add
    configuration without changing the core project schema.  The concrete
    listener remains responsible for validating the options it understands.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    enabled: bool = True
    options: dict[str, JsonValue] = Field(default_factory=dict)


class Project(BaseModel):
    """One business. Shares the engine with every other project and nothing else."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    app_package: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.]{0,254}$")
    device_filter: DeviceFilter = DeviceFilter()
    scripts: list[Script] = Field(default_factory=list)
    variables: dict[str, str] = Field(default_factory=dict)
    exporters: list[str] = Field(default_factory=lambda: ["pixelforge"])
    listeners: list[ListenerConfig] = Field(
        default_factory=lambda: [ListenerConfig(name="logcat")]
    )
    ocr_language: str = "eng"

    @model_validator(mode="after")
    def _unique_listener_names(self) -> Project:
        names = [listener.name for listener in self.listeners]
        duplicates = {value for value in names if names.count(value) > 1}
        if duplicates:
            raise ValueError(f"duplicate listeners: {sorted(duplicates)}")
        return self

    def templates_dir(self, root: Path) -> Path:
        return root / self.id / "templates"

    def resolve_variables(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """Project variables, then script/run overrides."""
        merged = dict(self.variables)
        merged.update(overrides or {})
        return merged
