"""Typed, registered image operations for the offline workbench."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from pixelforge.geometry.mapper import Rect
from pixelforge.vision.tool_catalog import _color_mask, demo_image, process_image

ParamValue = str | int | float | bool
ParamKind = Literal["choice", "integer", "number", "boolean"]
Handler = Callable[[np.ndarray, Rect | None, dict[str, ParamValue]], "OpResult"]

# The one type every operator's single "image" input accepts today. A MASK8
# output (color_mask's "mask" port, say) is a single channel of 0/255 -- feeding
# it to a step that expects a full image is a type error, not a style choice.
# Named once here so the notebook-graph validator checks against this exact
# value instead of keeping its own copy that can drift out of sync.
IMAGE_INPUT_TYPE = "IMAGE_RGB8"


@dataclass(frozen=True, slots=True)
class ParamSpec:
    name: str
    label: str
    kind: ParamKind
    default: ParamValue
    minimum: int | float | None = None
    maximum: int | float | None = None
    options: tuple[str, ...] = ()
    when: Mapping[str, ParamValue] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "options": list(self.options),
            "when": dict(self.when),
        }

    def parse(self, value: object) -> ParamValue:
        if self.kind == "boolean":
            if isinstance(value, bool):
                return value
            if value in ("true", "false"):
                return value == "true"
            raise ValueError(f"{self.name} must be true or false")
        if self.kind == "choice":
            if not isinstance(value, str) or value not in self.options:
                raise ValueError(f"{self.name} must be one of {', '.join(self.options)}")
            return value
        try:
            if self.kind == "integer":
                if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                    raise ValueError
                if not isinstance(value, (int, float, str)):
                    raise ValueError
                parsed: int | float = int(value)
            else:
                if not isinstance(value, (int, float, str)):
                    raise ValueError
                parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{self.name} must be a {self.kind}") from exc
        if not np.isfinite(parsed):
            raise ValueError(f"{self.name} must be finite")
        if self.minimum is not None and parsed < self.minimum:
            raise ValueError(f"{self.name} must be at least {self.minimum}")
        if self.maximum is not None and parsed > self.maximum:
            raise ValueError(f"{self.name} must be at most {self.maximum}")
        return parsed


@dataclass(frozen=True, slots=True)
class OpResult:
    """Named outputs; debug images are results rather than implicit file writes."""

    images: Mapping[str, np.ndarray] = field(default_factory=dict)
    regions: tuple[dict[str, object], ...] = ()
    points: tuple[dict[str, object], ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    text: str = ""
    artifacts: Mapping[str, np.ndarray] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OperatorSpec:
    id: str
    category: str
    name: str
    description: str
    source: str
    version: str
    params: tuple[ParamSpec, ...]
    outputs: Mapping[str, str]
    handler: Handler
    acceptance_ref: str
    needs_roi: bool = False

    @property
    def function_name(self) -> str:
        return f"{self.handler.__module__}.{self.handler.__qualname__}"

    def as_dict(self) -> dict[str, object]:
        state, reason = availability(self)
        return {
            "id": self.id,
            "category": self.category,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "version": self.version,
            "function_name": self.function_name,
            "status": state,
            "availability": "available" if state == "ready" else state,
            "availability_reason": reason,
            "params_schema": [param.as_dict() for param in self.params],
            "parameters": {param.name: param.default for param in self.params},
            "inputs": {"image": "IMAGE_RGB8"},
            "outputs": dict(self.outputs),
            "effect_image": f"/api/image-tools/{self.id}/demo" if state == "ready" else None,
            "acceptance_ref": self.acceptance_ref,
        }


OPERATORS: dict[str, OperatorSpec] = {}


def operator(
    *,
    id: str,
    category: str,
    name: str,
    description: str,
    source: str,
    params: tuple[ParamSpec, ...] = (),
    outputs: Mapping[str, str] | None = None,
    acceptance_ref: str,
    needs_roi: bool = False,
) -> Callable[[Handler], Handler]:
    """Register built-in operators without a parallel editable function-name list."""

    def register(handler: Handler) -> Handler:
        if id in OPERATORS:
            raise ValueError(f"duplicate operator id: {id}")
        OPERATORS[id] = OperatorSpec(
            id=id,
            category=category,
            name=name,
            description=description,
            source=source,
            version="1.0.0",
            params=params,
            outputs=outputs or {"image": "IMAGE_RGB8"},
            handler=handler,
            acceptance_ref=acceptance_ref,
            needs_roi=needs_roi,
        )
        return handler

    return register


COLORS = ("red", "yellow", "green", "blue", "white")
COLOR_PARAMS = (
    ParamSpec("color", "目标颜色", "choice", "yellow", options=COLORS),
    ParamSpec("s_min", "饱和度下限", "integer", 43, 0, 255),
    ParamSpec("v_min", "亮度下限", "integer", 46, 0, 255),
)
ACCEPTANCE = "backend/tests/test_operator_registry.py::test_registered_operator_contract"


def _legacy_params(params: Mapping[str, ParamValue]) -> dict[str, str]:
    return {key: str(value) for key, value in params.items()}


@operator(
    id="crop", category="基础处理", name="区域裁剪", description="按原图像素精确截取选区。",
    source="PixelForge", needs_roi=True, acceptance_ref=ACCEPTANCE,
)
def crop(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    return OpResult(images={"image": process_image("crop", image, roi=roi)})


@operator(
    id="grayscale", category="基础处理", name="灰度图",
    description="去除色彩, 观察亮度和结构。", source="PixelForge", acceptance_ref=ACCEPTANCE,
)
def grayscale(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    return OpResult(images={"image": process_image("grayscale", image, roi=roi)})


@operator(
    id="color_mask", category="颜色分析", name="指定颜色提取",
    description="按颜色筛选图像, 同时输出可复用的单通道掩码。", source="MHXY 思路重构",
    params=COLOR_PARAMS, outputs={"image": "IMAGE_RGB8", "mask": "MASK8"},
    acceptance_ref=ACCEPTANCE,
)
def color_mask(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    selected = _selected(image, roi)
    legacy = _legacy_params(params)
    mask = _color_mask(selected, legacy)
    filtered = process_image("color_mask", image, roi=roi, params=legacy)
    return OpResult(images={"image": filtered, "mask": mask})


@operator(
    id="edges", category="轮廓分析", name="边缘检测",
    description="使用 Canny 检查图像轮廓。", source="MHXY 思路重构",
    params=(ParamSpec("low", "低阈值", "integer", 50, 0, 255),
            ParamSpec("high", "高阈值", "integer", 150, 0, 255)),
    acceptance_ref=ACCEPTANCE,
)
def edges(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    output = process_image("edges", image, roi=roi, params=_legacy_params(params))
    return OpResult(images={"image": output})


@operator(
    id="threshold", category="文字预处理", name="二值化",
    description="按阈值将亮度转换成黑白图。", source="MHXY 思路重构",
    params=(ParamSpec("threshold", "二值阈值", "integer", 180, 0, 255),),
    acceptance_ref=ACCEPTANCE,
)
def threshold(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    output = process_image("threshold", image, roi=roi, params=_legacy_params(params))
    return OpResult(images={"image": output})


@operator(
    id="text_enhance", category="文字预处理", name="彩色文字增强",
    description="颜色筛选后合成两档亮度阈值。", source="MHXY 思路重构",
    params=COLOR_PARAMS, acceptance_ref=ACCEPTANCE,
)
def text_enhance(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    output = process_image("text_enhance", image, roi=roi, params=_legacy_params(params))
    return OpResult(images={"image": output})


def _selected(image: np.ndarray, roi: Rect | None) -> np.ndarray:
    if roi is None:
        return image
    return image[roi.y : roi.bottom, roi.x : roi.right]


def validate_params(spec: OperatorSpec, supplied: Mapping[str, object]) -> dict[str, ParamValue]:
    declared = {param.name: param for param in spec.params}
    unknown = set(supplied) - set(declared)
    if unknown:
        raise ValueError(f"unknown parameters: {', '.join(sorted(unknown))}")
    values = {
        param.name: param.parse(supplied.get(param.name, param.default)) for param in spec.params
    }
    if spec.id == "edges" and int(values["low"]) >= int(values["high"]):
        raise ValueError("low must be less than high")
    return values


def run_operator(
    tool_id: str, image: np.ndarray, *, roi: Rect | None = None,
    params: Mapping[str, object] | None = None,
) -> OpResult:
    spec = OPERATORS[tool_id]
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8 or image.size == 0:
        raise ValueError("expected a non-empty RGB8 image")
    if roi is not None and (
        roi.x < 0 or roi.y < 0 or roi.width <= 0 or roi.height <= 0
        or roi.right > image.shape[1] or roi.bottom > image.shape[0]
    ):
        raise ValueError("ROI must be inside the input image")
    if spec.needs_roi and roi is None:
        raise ValueError("this tool requires an ROI")
    result = spec.handler(image, roi, validate_params(spec, params or {}))
    for name, kind in spec.outputs.items():
        output = result.images.get(name)
        if output is None:
            raise ValueError(f"tool did not produce output {name}")
        if output.dtype != np.uint8 or output.size == 0:
            raise ValueError(f"invalid output {name}")
        if kind == "MASK8" and (output.ndim != 2 or not np.isin(output, [0, 255]).all()):
            raise ValueError(f"invalid MASK8 output {name}")
        if kind == "IMAGE_RGB8" and (output.ndim != 3 or output.shape[2] != 3):
            raise ValueError(f"invalid RGB8 output {name}")
    return result


def availability(spec: OperatorSpec) -> tuple[str, str]:
    if not spec.acceptance_ref:
        return "pending_validation", "缺少验收用例"
    try:
        roi = Rect(18, 20, 142, 65) if spec.needs_roi else None
        run_operator(spec.id, demo_image(), roi=roi)
    except (ValueError, KeyError) as exc:
        return "pending_adapter", f"示例运行失败: {exc}"
    return "ready", "已注册并可运行示例; 效果验收以测试记录为准"


def list_operators() -> list[dict[str, object]]:
    return [spec.as_dict() for spec in OPERATORS.values()]


def manifest(spec: OperatorSpec) -> dict[str, object]:
    """Immutable code-owned fields for a published tool version."""
    return {
        "version": spec.version,
        "implementation_key": spec.function_name,
        "params_schema": [param.as_dict() for param in spec.params],
        "inputs_schema": {"image": IMAGE_INPUT_TYPE, "roi_required": spec.needs_roi},
        "outputs_schema": dict(spec.outputs),
        "acceptance_ref": spec.acceptance_ref,
    }
