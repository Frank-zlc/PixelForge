"""Database backed inventory of image tools and their executable implementations."""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from pixelforge.geometry.mapper import Rect, Size

if TYPE_CHECKING:
    from pixelforge.image_lab.storage import ImageLabStore

# Only pending entries live here. Runnable metadata is projected from the
# decorated code registry in image_lab/operators.py.
PENDING_TOOLS: tuple[tuple[str, str, str, str, str, str, str, dict[str, str | int]], ...] = (
    (
        "ocr",
        "文字识别",
        "OCR 文字定位",
        "TesseractOcr.recognize",
        "设备脚本中已有文字框与置信度; 图片工作台尚未接入结果标注。",
        "device_only",
        "PixelForge",
        {},
    ),
    (
        "screen_diff",
        "画面分析",
        "画面变化检测",
        "diff_ratio",
        "比较连续设备截图, 判断画面是否稳定。",
        "device_only",
        "PixelForge",
        {},
    ),
    (
        "text_regions",
        "文字识别",
        "彩色文字区域定位",
        "TBD",
        "计划移植 MHXY 的颜色筛选与轮廓定位, 输出可检查的候选文字框。",
        "planned",
        "MHXY 候选",
        {},
    ),
    (
        "batch_process",
        "批量处理",
        "目录批量处理",
        "TBD",
        "计划将同一处理参数应用于目录内多张图片并导出结果。",
        "planned",
        "PixelForge 规划",
        {},
    ),
)

COLOR_RANGES = {
    "yellow": (26, 34),
    "green": (35, 77),
    "blue": (100, 124),
    "white": (0, 180),
}


class ImageToolCatalog:
    def __init__(self, database: Path) -> None:
        from pixelforge.image_lab.storage import ImageLabStore

        self.database = database
        self.store: ImageLabStore = ImageLabStore(database)

    def _connect(self) -> sqlite3.Connection:
        return self.store.connect()

    def list(self) -> list[dict[str, object]]:
        return self.store.list_tools()

    def get(self, tool_id: str) -> dict[str, object] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM image_tools WHERE id = ?", (tool_id,)).fetchone()
        return {**dict(row), "parameters": json.loads(row["parameters_json"])} if row else None


def _number(params: dict[str, str], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(params.get(key, str(default)))
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _color_mask(image: np.ndarray, params: dict[str, str]) -> np.ndarray:
    color = params.get("color", "yellow")
    if color not in COLOR_RANGES and color != "red":
        raise ValueError("color must be red, yellow, green, blue or white")
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    saturation = _number(params, "s_min", 43, 0, 255)
    value = _number(params, "v_min", 46, 0, 255)
    if color == "white":
        lower, upper = (0, 0, max(value, 180)), (180, 40, 255)
        return cv2.inRange(hsv, np.array(lower), np.array(upper))
    if color == "red":
        first = cv2.inRange(hsv, np.array((0, saturation, value)), np.array((10, 255, 255)))
        second = cv2.inRange(hsv, np.array((170, saturation, value)), np.array((179, 255, 255)))
        return cv2.bitwise_or(first, second)
    hue_low, hue_high = COLOR_RANGES[color]
    return cv2.inRange(hsv, np.array((hue_low, saturation, value)), np.array((hue_high, 255, 255)))


def decode_data_url(value: str) -> np.ndarray:
    """Decode a base64 PNG/JPEG (optionally as a ``data:...;base64,`` URL) into RGB."""
    if not value:
        raise ValueError("template image is required")
    raw_b64 = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        raw = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("template image must be valid base64") from exc
    decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValueError("template image could not be decoded")
    return cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)


def encode_data_url(image: np.ndarray) -> str:
    """Encode an RGB image as a ``data:image/png;base64,...`` string."""
    bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR) if image.ndim == 3 else image
    ok, encoded = cv2.imencode(".png", bgr)
    if not ok:
        raise ValueError("could not encode image")
    return "data:image/png;base64," + base64.b64encode(encoded).decode("ascii")


def draw_marker(image: np.ndarray, box: Rect, color: tuple[int, int, int] = (64, 200, 120)) -> np.ndarray:
    """Copy of ``image`` with a rectangle drawn around ``box``. Never mutates the input."""
    marked = image.copy()
    cv2.rectangle(marked, (box.x, box.y), (box.right, box.bottom), color, 2)
    return marked


def similarity_ratio(
    area: np.ndarray, template: np.ndarray, *, mode: str, color_params: dict[str, str]
) -> tuple[float, np.ndarray]:
    """MHXY 移植 (``template_similarity_check``): 颜色掩码或边缘轮廓的交集比例.

    ``mode == "color"`` masks both images by ``color_params`` (see ``_color_mask``);
    otherwise both are compared by their Canny edges. Either way the ratio is
    intersection-pixels / template-mask-pixels, so it answers "how much of the
    template's signature shows up in this region" rather than raw pixel diff.
    """
    if mode == "color":
        area_mask = _color_mask(area, color_params)
        tem_mask = _color_mask(template, color_params)
    else:
        area_mask = cv2.Canny(cv2.cvtColor(area, cv2.COLOR_RGB2GRAY), 50, 150)
        tem_mask = cv2.Canny(cv2.cvtColor(template, cv2.COLOR_RGB2GRAY), 50, 150)
    if area_mask.shape != tem_mask.shape:
        area_mask = cv2.resize(
            area_mask, (tem_mask.shape[1], tem_mask.shape[0]), interpolation=cv2.INTER_NEAREST
        )
    intersection = cv2.bitwise_and(area_mask, tem_mask, mask=tem_mask)
    template_total = float(np.sum(tem_mask))
    if template_total <= 0:
        return 0.0, intersection
    return float(np.sum(intersection)) / template_total, intersection


def process_image(
    tool_id: str,
    image: np.ndarray,
    *,
    roi: Rect | None = None,
    params: dict[str, str] | None = None,
) -> np.ndarray:
    """Run a registered operation on an RGB image; never mutate the caller's pixels."""
    if tool_id not in IMPLEMENTATIONS:
        raise KeyError(tool_id)
    if image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
        raise ValueError("expected a non-empty RGB image")
    if roi is not None:
        if roi.right <= 0 or roi.bottom <= 0 or roi.x >= image.shape[1] or roi.y >= image.shape[0]:
            raise ValueError("selection is outside the image")
        box = roi.clamped_to(Size(image.shape[1], image.shape[0]))
        image = image[box.y : box.bottom, box.x : box.right]
    return IMPLEMENTATIONS[tool_id](image, params or {})


def _grayscale(image: np.ndarray, params: dict[str, str]) -> np.ndarray:
    return cv2.cvtColor(cv2.cvtColor(image, cv2.COLOR_RGB2GRAY), cv2.COLOR_GRAY2RGB)


def _crop(image: np.ndarray, params: dict[str, str]) -> np.ndarray:
    return image.copy()


def _extract_color(image: np.ndarray, params: dict[str, str]) -> np.ndarray:
    return cv2.bitwise_and(image, image, mask=_color_mask(image, params))


def _edges(image: np.ndarray, params: dict[str, str]) -> np.ndarray:
    low = _number(params, "low", 50, 0, 255)
    high = _number(params, "high", 150, 0, 255)
    if low >= high:
        raise ValueError("low must be less than high")
    edges = cv2.Canny(cv2.cvtColor(image, cv2.COLOR_RGB2GRAY), low, high)
    return cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)


def _threshold(image: np.ndarray, params: dict[str, str]) -> np.ndarray:
    level = _number(params, "threshold", 180, 0, 255)
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    _, binary = cv2.threshold(gray, level, 255, cv2.THRESH_BINARY)
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)


def _enhance_text(image: np.ndarray, params: dict[str, str]) -> np.ndarray:
    mask = _color_mask(image, params)
    brightness = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    first = cv2.bitwise_and(cv2.inRange(brightness, 180, 255), mask)
    second = cv2.bitwise_and(cv2.inRange(brightness, 150, 255), mask)
    enhanced = cv2.addWeighted(first, 0.7, second, 0.3, 0)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2RGB)


IMPLEMENTATIONS = {
    "crop": _crop,
    "grayscale": _grayscale,
    "color_mask": _extract_color,
    "edges": _edges,
    "threshold": _threshold,
    "text_enhance": _enhance_text,
}


def demo_image() -> np.ndarray:
    image = np.full((200, 360, 3), (25, 30, 42), np.uint8)
    cv2.rectangle(image, (18, 20), (160, 85), (235, 220, 45), -1)
    cv2.rectangle(image, (188, 20), (340, 85), (210, 65, 65), -1)
    cv2.rectangle(image, (18, 112), (340, 180), (55, 85, 125), -1)
    cv2.putText(image, "START", (35, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (22, 24, 30), 2)
    cv2.putText(image, "1280", (213, 61), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.putText(image, "PIXEL FORGE", (46, 155), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (245, 245, 245), 2)
    return image
