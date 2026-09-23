"""Typed, registered image operations for the offline workbench."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

import cv2
import numpy as np

from pixelforge.geometry.mapper import Point, Rect
from pixelforge.vision.matching import match_template, match_template_expanding
from pixelforge.vision.ocr import OcrError, TesseractOcr, text_similarity
from pixelforge.vision.tool_catalog import (
    _color_mask,
    decode_data_url,
    demo_image,
    draw_boxes,
    draw_marker,
    encode_data_url,
    find_text_blobs,
    process_image,
    similarity_ratio,
)

ParamValue = str | int | float | bool
ParamKind = Literal["choice", "integer", "number", "boolean", "image", "text"]
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
        if self.kind == "image":
            if not isinstance(value, str) or not value:
                raise ValueError(f"{self.name} must be a base64-encoded image")
            return value
        if self.kind == "text":
            if not isinstance(value, str):
                raise ValueError(f"{self.name} must be text")
            return value
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


def _demo_template() -> str:
    """A crop of ``demo_image()``'s START button -- a template that's guaranteed to be
    findable in the demo image, so ``template_match``/``match_verify`` can self-test
    (``availability()``) without a real device screenshot to work from."""
    return encode_data_url(demo_image()[20:85, 18:160])


TEMPLATE_MATCH_PARAMS = (
    ParamSpec("template", "模板图 (base64 PNG)", "image", _demo_template()),
    ParamSpec("threshold", "匹配阈值", "number", 0.90, 0.5, 1.0),
)


@operator(
    id="template_match", category="元素定位", name="模板匹配",
    description="多尺度模板匹配, 在图像(或选区)中定位模板位置; 复用设备脚本已有的 ROI/多尺度实现。",
    source="PixelForge 设备脚本复用", params=TEMPLATE_MATCH_PARAMS,
    outputs={"image": "IMAGE_RGB8"}, acceptance_ref=ACCEPTANCE,
)
def template_match_op(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    template = decode_data_url(str(params["template"]))
    result = match_template(image, template, roi=roi, threshold=float(params["threshold"]))
    regions: tuple[dict[str, object], ...] = ()
    points: tuple[dict[str, object], ...] = ()
    preview = image
    if result.box is not None:
        color = (64, 200, 120) if result.found else (235, 90, 90)
        preview = draw_marker(image, result.box, color)
        regions = (
            {
                "x": result.box.x, "y": result.box.y,
                "width": result.box.width, "height": result.box.height,
                "matched": result.found,
            },
        )
        center = result.box.center
        points = ({"x": center.x, "y": center.y},)
    return OpResult(
        images={"image": preview},
        regions=regions,
        points=points,
        metrics={"score": result.score, "threshold": result.threshold, "scale": result.scale},
        text=result.explain(),
        notes=(f"metric={result.metric}",),
    )


MATCH_VERIFY_PARAMS = (
    ParamSpec("template", "参考模板图 (base64 PNG)", "image", _demo_template()),
    ParamSpec("mode", "比对方式", "choice", "edge", options=("edge", "color")),
    ParamSpec("color", "目标颜色", "choice", "yellow", options=COLORS, when={"mode": "color"}),
    ParamSpec("similarity", "相似度阈值", "number", 0.5, 0.0, 1.0),
)


@operator(
    id="match_verify", category="元素定位", name="模板二次校验",
    description="按颜色掩码或边缘轮廓比对选区与模板的相似度, 为模板匹配结果提供二次校验分数。",
    source="MHXY 移植 (template_similarity_check)", needs_roi=True,
    params=MATCH_VERIFY_PARAMS, outputs={"image": "IMAGE_RGB8"}, acceptance_ref=ACCEPTANCE,
)
def match_verify(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    area = _selected(image, roi)
    template = decode_data_url(str(params["template"]))
    mode = str(params["mode"])
    color_params = _legacy_params({"color": params["color"], "s_min": 43, "v_min": 46})
    ratio, _mask = similarity_ratio(area, template, mode=mode, color_params=color_params)
    passed = ratio >= float(params["similarity"])
    return OpResult(
        images={"image": area},
        metrics={"similarity": ratio},
        text="相似" if passed else "不相似",
        notes=(f"similarity={ratio:.3f} threshold={params['similarity']} mode={mode}",),
    )


OCR_LANGUAGES = ("eng", "chi_sim", "chi_sim+eng")
OCR_PARAMS = (
    ParamSpec("language", "识别语言", "choice", "eng", options=OCR_LANGUAGES),
    ParamSpec("psm", "页面分割模式 (PSM)", "integer", 11, 0, 13),
    ParamSpec("min_confidence", "最低置信度", "number", 60, 0, 100),
    ParamSpec("target_words", "目标文字 (可选, 用于相似度校验)", "text", ""),
)


@operator(
    id="ocr", category="文字识别", name="OCR 文字定位",
    description=(
        "调用 Tesseract 识别选区(或整图)文字, 返回逐词候选框、置信度; "
        "填写目标文字可额外给出与识别结果的相似度。默认语言包 eng 已随环境自带, "
        "识别中文需要在系统上另外安装 chi_sim 语言包。"
    ),
    source="PixelForge 设备脚本复用 (TesseractOcr)",
    params=OCR_PARAMS, outputs={"image": "IMAGE_RGB8"}, acceptance_ref=ACCEPTANCE,
)
def ocr(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    engine = TesseractOcr(language=str(params["language"]))
    if not engine.available:
        raise ValueError("未找到 Tesseract 可执行文件, 无法运行 OCR (设置 TESSERACT_CMD 或安装 tesseract)")
    try:
        result = engine.recognize_sync(image, crop=roi, psm=int(params["psm"]))
    except OcrError as exc:
        # A missing language pack, a segfault on odd input, a timeout -- all of
        # these should downgrade this operator to "pending_adapter" rather than
        # crash the app at startup, where availability() runs this same demo.
        raise ValueError(str(exc)) from exc
    min_confidence = float(params["min_confidence"])
    kept = tuple(word for word in result.words if word.confidence >= min_confidence)
    preview = draw_boxes(image, [word.box for word in kept]) if kept else image
    regions = tuple(
        {
            "x": word.box.x, "y": word.box.y, "width": word.box.width, "height": word.box.height,
            "text": word.text, "confidence": word.confidence,
        }
        for word in kept
    )
    points = tuple({"x": word.center.x, "y": word.center.y} for word in kept)
    metrics: dict[str, float] = {"word_count": float(len(kept))}
    if result.mean_confidence is not None:
        metrics["mean_confidence"] = result.mean_confidence
    target = str(params["target_words"])
    if target:
        metrics["text_similarity"] = text_similarity(target, result.text)
    return OpResult(images={"image": preview}, regions=regions, points=points, metrics=metrics, text=result.text)


TEXT_REGIONS_PARAMS = COLOR_PARAMS + (
    ParamSpec("min_height", "最小文字块高度(像素)", "integer", 30, 5, 500),
    ParamSpec("language", "识别语言", "choice", "eng", options=OCR_LANGUAGES),
    ParamSpec("target_words", "目标文字 (可选, 用于相似度校验)", "text", ""),
)


@operator(
    id="text_regions", category="文字识别", name="彩色文字区域定位",
    description=(
        "按颜色筛选并用形态学操作把相邻字符合并成候选文字块, 逐块运行 OCR 校验并给出坐标, "
        "适合颜色统一的 UI 文案(公告栏金色/白色文字等)在没有固定模板时的定位。"
    ),
    source="MHXY 移植并优化 (dynamic_capture.get_words_xy)",
    params=TEXT_REGIONS_PARAMS, outputs={"image": "IMAGE_RGB8"}, acceptance_ref=ACCEPTANCE,
)
def text_regions(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    selected = _selected(image, roi)
    legacy = _legacy_params(params)
    blobs = find_text_blobs(selected, legacy, min_height=int(params["min_height"]))
    engine = TesseractOcr(language=str(params["language"]))
    target = str(params["target_words"])
    offset_x, offset_y = (roi.x, roi.y) if roi is not None else (0, 0)
    regions: list[dict[str, object]] = []
    preview_boxes: list[Rect] = []
    best_similarity = 0.0
    for blob in blobs:
        x, y, w, h = blob["x"], blob["y"], blob["width"], blob["height"]
        crop = selected[y : y + h, x : x + w]
        text = ""
        if engine.available and crop.size:
            try:
                text = engine.recognize_sync(crop, psm=7).text.strip()
            except OcrError:
                text = ""  # a single unreadable blob should not fail the whole scan
        entry: dict[str, object] = {
            "x": x + offset_x, "y": y + offset_y, "width": w, "height": h,
            "gravity_x": blob["gravity_x"] + offset_x, "gravity_y": blob["gravity_y"] + offset_y,
            "text": text,
        }
        if target and text:
            score = text_similarity(target, text)
            entry["text_similarity"] = score
            best_similarity = max(best_similarity, score)
        regions.append(entry)
        preview_boxes.append(Rect(entry["x"], entry["y"], w, h))
    preview = draw_boxes(image, preview_boxes) if preview_boxes else image
    metrics: dict[str, float] = {"blob_count": float(len(regions))}
    if target:
        metrics["best_text_similarity"] = best_similarity
    return OpResult(images={"image": preview}, regions=tuple(regions), metrics=metrics)


HIGHLIGHT_STATE_PARAMS = COLOR_PARAMS + (
    ParamSpec("coverage_threshold", "点亮占比阈值", "number", 0.5, 0.0, 1.0),
)


@operator(
    id="highlight_state", category="颜色分析", name="高亮状态检测",
    description=(
        "统计选区内目标颜色像素占比, 用于判断按钮/图标/选项是否处于点亮(高亮/选中)状态; "
        "占比达到阈值视为已点亮。"
    ),
    source="MHXY 移植并优化 (click_function.get_highlight_mask)", needs_roi=True,
    params=HIGHLIGHT_STATE_PARAMS, outputs={"image": "IMAGE_RGB8", "mask": "MASK8"},
    acceptance_ref=ACCEPTANCE,
)
def highlight_state(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    area = _selected(image, roi)
    mask = _color_mask(area, _legacy_params(params))
    coverage = float(np.count_nonzero(mask)) / float(mask.size) if mask.size else 0.0
    lit = coverage >= float(params["coverage_threshold"])
    preview = cv2.bitwise_and(area, area, mask=mask)
    return OpResult(
        images={"image": preview, "mask": mask},
        metrics={"coverage": coverage, "threshold": float(params["coverage_threshold"])},
        text="点亮" if lit else "未点亮",
        notes=(f"coverage={coverage:.3f} threshold={params['coverage_threshold']}",),
    )


TEMPLATE_MATCH_EXPAND_PARAMS = (
    ParamSpec("template", "模板图 (base64 PNG)", "image", _demo_template()),
    ParamSpec("anchor_x", "锚点 X", "integer", 89, 0, 100000),
    ParamSpec("anchor_y", "锚点 Y", "integer", 52, 0, 100000),
    ParamSpec("initial_radius", "初始搜索半径(像素)", "integer", 50, 1, 2000),
    ParamSpec("radius_step", "每次扩展像素", "integer", 25, 1, 500),
    ParamSpec("max_expansions", "最大扩展次数", "integer", 3, 0, 10),
    ParamSpec("threshold", "匹配阈值", "number", 0.90, 0.5, 1.0),
)


@operator(
    id="template_match_expand", category="元素定位", name="锚点扩展模板匹配",
    description=(
        "以指定锚点为中心, 在逐步扩大的搜索半径内重试模板匹配; 适合目标位置有小幅漂移"
        "(滚动、轻微布局变化)的场景, 比固定 ROI 更省搜索量, 比全图搜索更抗干扰。"
    ),
    source="MHXY 移植并优化 (dynamic_capture.accurate_recognition)",
    params=TEMPLATE_MATCH_EXPAND_PARAMS, outputs={"image": "IMAGE_RGB8"}, acceptance_ref=ACCEPTANCE,
)
def template_match_expand(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    template = decode_data_url(str(params["template"]))
    anchor = Point(float(params["anchor_x"]), float(params["anchor_y"]))
    result = match_template_expanding(
        image, template, anchor=anchor,
        initial_radius=int(params["initial_radius"]),
        radius_step=int(params["radius_step"]),
        max_expansions=int(params["max_expansions"]),
        threshold=float(params["threshold"]),
    )
    regions: tuple[dict[str, object], ...] = ()
    points: tuple[dict[str, object], ...] = ()
    preview = image
    if result.box is not None:
        color = (64, 200, 120) if result.found else (235, 90, 90)
        preview = draw_marker(image, result.box, color)
        regions = (
            {
                "x": result.box.x, "y": result.box.y,
                "width": result.box.width, "height": result.box.height,
                "matched": result.found,
            },
        )
        center = result.box.center
        points = ({"x": center.x, "y": center.y},)
    return OpResult(
        images={"image": preview},
        regions=regions,
        points=points,
        metrics={"score": result.score, "threshold": result.threshold, "scale": result.scale},
        text=result.explain(),
        notes=(f"metric={result.metric}",),
    )


FACE_DETECT_PARAMS = (
    ParamSpec("scale_factor", "缩放步长", "number", 1.1, 1.01, 2.0),
    ParamSpec("min_neighbors", "最小相邻数", "integer", 3, 1, 20),
)


@operator(
    id="face_detect", category="轮廓分析", name="人脸检测",
    description="使用 OpenCV 内置 Haar 级联检测人脸区域, 用于定位人物头像/角色立绘中的面部位置。",
    source="MHXY 移植并优化 (reserve_function.face_detect)",
    params=FACE_DETECT_PARAMS, outputs={"image": "IMAGE_RGB8"}, acceptance_ref=ACCEPTANCE,
)
def face_detect(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    area = _selected(image, roi)
    gray = cv2.cvtColor(area, cv2.COLOR_RGB2GRAY)
    # MHXY pointed at a hardcoded project-relative XML path (path_config.FACE_DETECT_TEST).
    # cv2.data.haarcascades ships the same model with the desktop opencv-python wheel,
    # with no extra file for the person to keep track of -- but a stripped-down build
    # (some headless wheels) can omit the classifier or its data entirely, so this is
    # treated as an environment gap (-> pending_adapter via ValueError) rather than a bug.
    cascade_dir = getattr(getattr(cv2, "data", None), "haarcascades", None)
    detector = None
    if cascade_dir:
        cascade_path = os.path.join(cascade_dir, "haarcascade_frontalface_default.xml")
        if hasattr(cv2, "CascadeClassifier") and os.path.isfile(cascade_path):
            detector = cv2.CascadeClassifier(cascade_path)
    if detector is None or detector.empty():
        raise ValueError(
            "当前 OpenCV 构建缺少人脸检测所需的 Haar 级联分类器/数据文件 "
            "(常见于精简版 opencv-python-headless); 安装标准 opencv-python 或 "
            "opencv-contrib-python 后该工具即可运行。"
        )
    faces = detector.detectMultiScale(
        gray, scaleFactor=float(params["scale_factor"]), minNeighbors=int(params["min_neighbors"])
    )
    boxes = [Rect(int(x), int(y), int(w), int(h)) for x, y, w, h in faces]
    preview = draw_boxes(area, boxes) if boxes else area
    regions = tuple({"x": b.x, "y": b.y, "width": b.width, "height": b.height} for b in boxes)
    points = tuple({"x": b.center.x, "y": b.center.y} for b in boxes)
    return OpResult(
        images={"image": preview}, regions=regions, points=points,
        metrics={"face_count": float(len(boxes))},
    )


LINE_DETECT_PARAMS = (
    ParamSpec("low", "Canny 低阈值", "integer", 50, 0, 255),
    ParamSpec("high", "Canny 高阈值", "integer", 150, 0, 255),
    ParamSpec("min_line_length", "最小线段长度", "integer", 50, 1, 2000),
    ParamSpec("max_line_gap", "最大线段间隙", "integer", 10, 0, 200),
)


@operator(
    id="line_detect", category="轮廓分析", name="直线检测",
    description="基于 Canny 边缘与霍夫变换检测画面中的直线段, 用于定位分割线、进度条边框等结构。",
    source="MHXY 移植并优化 (reserve_function.line_detect_possible_demo)",
    params=LINE_DETECT_PARAMS, outputs={"image": "IMAGE_RGB8"}, acceptance_ref=ACCEPTANCE,
)
def line_detect(image: np.ndarray, roi: Rect | None, params: dict[str, ParamValue]) -> OpResult:
    area = _selected(image, roi)
    low, high = int(params["low"]), int(params["high"])
    if low >= high:
        raise ValueError("low must be less than high")
    gray = cv2.cvtColor(area, cv2.COLOR_RGB2GRAY)
    edges_img = cv2.Canny(gray, low, high, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges_img, 1, np.pi / 180, 100,
        minLineLength=int(params["min_line_length"]), maxLineGap=int(params["max_line_gap"]),
    )
    preview = area.copy()
    segments: list[dict[str, object]] = []
    if lines is not None:
        # HoughLinesP's output shape has changed across OpenCV versions --
        # (N, 1, 4) historically, (N, 4) on newer builds. reshape(-1, 4)
        # normalises either into one (x1, y1, x2, y2) row per line.
        for x1, y1, x2, y2 in lines.reshape(-1, 4).tolist():
            cv2.line(preview, (x1, y1), (x2, y2), (235, 90, 90), 2)
            segments.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2})
    return OpResult(
        images={"image": preview}, regions=tuple(segments),
        metrics={"line_count": float(len(segments))},
    )


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
