"""The four concrete locators."""

from __future__ import annotations

import re
from pathlib import Path

from pixelforge.device.uiautomator import UiNode
from pixelforge.geometry.mapper import Rect, Size
from pixelforge.locators.base import LocateContext, LocateResult
from pixelforge.script.model import Strategy, Target
from pixelforge.vision.matching import DEFAULT_SCALES, match_template
from pixelforge.vision.ocr import TesseractOcr

__all__ = ["A11yLocator", "CoordLocator", "OcrLocator", "TemplateLocator"]


def _roi_to_rect(roi: tuple[float, float, float, float] | None, size: Size) -> Rect | None:
    if roi is None:
        return None
    left, top, right, bottom = roi
    return Rect(
        x=int(left * size.width),
        y=int(top * size.height),
        width=max(1, int((right - left) * size.width)),
        height=max(1, int((bottom - top) * size.height)),
    ).clamped_to(size)


class A11yLocator:
    """Accessibility tree. First choice: survives resolution and layout changes."""

    strategy = Strategy.A11Y

    def applicable(self, target: Target, context: LocateContext) -> bool:
        return target.a11y is not None and context.a11y_available and bool(context.nodes)

    def why_not(self, target: Target, context: LocateContext) -> str:
        if not context.a11y_available:
            return "uiautomator2 unavailable"
        if not context.nodes:
            # Normal for a game or a Canvas UI -- not an error, just a miss.
            return "accessibility tree is empty (canvas or game UI?)"
        return f"no node matched {target.a11y.describe() if target.a11y else ''}"

    async def locate(self, target: Target, context: LocateContext) -> LocateResult | None:
        selector = target.a11y
        if selector is None:
            return None
        matches = [
            node
            for node in context.nodes
            if isinstance(node, UiNode) and self._matches(selector, node)
        ]
        if not matches:
            return None
        if selector.index is not None:
            if selector.index >= len(matches):
                return None
            node = matches[selector.index]
        else:
            # Smallest match: trees nest, and the innermost node is the control.
            node = min(matches, key=lambda item: item.area)
        return LocateResult(
            strategy=Strategy.A11Y,
            box=node.bounds,
            score=1.0,
            detail=node.describe(),
        )

    @staticmethod
    def _matches(selector, node: UiNode) -> bool:
        checks = (
            (selector.resource_id, node.resource_id, "eq"),
            (selector.text, node.text, "eq"),
            (selector.text_contains, node.text, "in"),
            (selector.content_desc, node.content_desc, "eq"),
            (selector.class_name, node.class_name, "eq"),
            (selector.package, node.package, "eq"),
        )
        for wanted, actual, mode in checks:
            if wanted is None:
                continue
            if actual is None:
                return False
            if mode == "eq" and actual != wanted:
                return False
            if mode == "in" and wanted.lower() not in actual.lower():
                return False
        return not (selector.clickable is not None and node.clickable != selector.clickable)


class TemplateLocator:
    """Image matching. The strategy that works where there is no tree at all."""

    strategy = Strategy.TEMPLATE

    def __init__(self) -> None:
        self._last: str = "not attempted"

    def applicable(self, target: Target, context: LocateContext) -> bool:
        return target.template is not None and context.screenshot is not None

    def why_not(self, target: Target, context: LocateContext) -> str:
        if context.screenshot is None:
            return "no screenshot available"
        return self._last

    async def locate(self, target: Target, context: LocateContext) -> LocateResult | None:
        import cv2
        import numpy as np

        spec = target.template
        if spec is None or context.screenshot is None:
            return None
        root = Path(str(context.templates_dir or "."))
        path = root / spec.file
        if not path.is_file():
            self._last = f"template file missing: {path}"
            return None
        needle = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if needle is None:
            self._last = f"template unreadable: {path}"
            return None
        needle = cv2.cvtColor(needle, cv2.COLOR_BGR2RGB)

        mask = None
        if spec.mask:
            mask_path = root / spec.mask
            if mask_path.is_file():
                mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)

        haystack = context.screenshot
        assert isinstance(haystack, np.ndarray)
        result = match_template(
            haystack,
            needle,
            roi=_roi_to_rect(spec.roi, Size(haystack.shape[1], haystack.shape[0])),
            threshold=spec.threshold,
            mask=mask,
            scales=DEFAULT_SCALES if spec.multi_scale else (1.0,),
        )
        self._last = result.explain()
        if not result.found or result.box is None:
            return None
        return LocateResult(
            strategy=Strategy.TEMPLATE,
            box=result.box,
            score=result.score,
            detail=result.explain(),
        )


class OcrLocator:
    """Text recognition. For screens with visible labels but no tree."""

    strategy = Strategy.OCR

    def __init__(self, ocr: TesseractOcr | None = None) -> None:
        self._ocr = ocr or TesseractOcr()
        self._last = "not attempted"

    def applicable(self, target: Target, context: LocateContext) -> bool:
        return (
            target.ocr is not None
            and context.screenshot is not None
            and self._ocr.available
        )

    def why_not(self, target: Target, context: LocateContext) -> str:
        if not self._ocr.available:
            return "Tesseract not installed"
        if context.screenshot is None:
            return "no screenshot available"
        return self._last

    async def locate(self, target: Target, context: LocateContext) -> LocateResult | None:
        import numpy as np

        spec = target.ocr
        if spec is None or context.screenshot is None:
            return None
        haystack = context.screenshot
        assert isinstance(haystack, np.ndarray)
        size = Size(haystack.shape[1], haystack.shape[0])
        result = await self._ocr.recognize(
            haystack,
            crop=_roi_to_rect(spec.roi, size),
            language=spec.language or context.ocr_language,
        )
        pattern = re.compile(spec.pattern)
        for word in result.words:
            if word.confidence >= spec.min_confidence and pattern.search(word.text):
                self._last = f"matched {word.text!r} at {word.confidence:.0f}%"
                return LocateResult(
                    strategy=Strategy.OCR,
                    box=word.box,
                    score=word.confidence / 100.0,
                    detail=self._last,
                )
        seen = ", ".join(w.text for w in result.words[:8]) or "(nothing)"
        self._last = f"no word matched /{spec.pattern}/; saw: {seen}"
        return None


class CoordLocator:
    """Normalised coordinates. Always succeeds, which is exactly the danger.

    Placed last for that reason: it cannot fail, so anything above it must be
    given its chance first, or the chain would silently stop improving.
    """

    strategy = Strategy.COORD

    def applicable(self, target: Target, context: LocateContext) -> bool:
        return target.coord is not None

    def why_not(self, target: Target, context: LocateContext) -> str:
        return "no coordinate recorded"

    async def locate(self, target: Target, context: LocateContext) -> LocateResult | None:
        coord = target.coord
        if coord is None:
            return None
        display = context.display
        x = coord.x * display.width
        y = coord.y * display.height
        detail = f"normalised ({coord.x:.4f}, {coord.y:.4f})"
        profile = target.recorded_on
        if profile is not None and abs(profile.aspect - display.aspect) > 0.02:
            # Stated rather than silently tolerated: a normalised point is only
            # meaningful on a similarly shaped screen, and this is the moment the
            # user can still do something about it.
            detail += (
                f" -- recorded at {profile.width}x{profile.height}, replaying on "
                f"{display.width}x{display.height}; aspect differs, so this point "
                "may not be on the same control"
            )
        return LocateResult(
            strategy=Strategy.COORD,
            box=Rect(x=int(x) - 1, y=int(y) - 1, width=2, height=2),
            score=0.5,
            detail=detail,
        )
