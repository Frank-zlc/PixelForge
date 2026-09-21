"""Template matching, hardened against the four things that break it in practice.

Plain ``cv2.matchTemplate`` works beautifully on a screenshot of the same device
in the same state, and fails constantly in real use. The four causes, and what
this module does about each:

1. **Dynamic content.** An avatar, a timestamp, an ad inside the template region
   changes every run and drags the correlation down. -> ``mask``: pixels marked
   0 are excluded from the score, so a button can be matched by its frame and
   label while a changing badge inside it is ignored.

2. **Resolution differences.** The same UI on a 1080p and a 1440p device differs
   by a scale factor the template does not have. -> ``scales``: a small pyramid
   around 1.0, keeping the best score across it.

3. **Animation.** A match attempted mid-transition finds the control halfway
   through a slide and scores badly, and retrying immediately hits the next
   frame of the same animation. -> :func:`wait_until_stable`, which compares
   consecutive captures and only proceeds once the screen has settled.

4. **Searching the whole screen.** A button template often matches some unrelated
   patch of background better than the real control. -> ``roi``: restrict the
   search to where the element can actually be, which also makes it faster.

On metric choice: masked matching uses ``TM_CCORR_NORMED`` because OpenCV only
supports masks for that and ``TM_SQDIFF``. Unmasked uses ``TM_CCOEFF_NORMED``,
which subtracts the mean and so tolerates brightness shifts -- exactly what a
theme change or an overlay dimming produces. The two metrics are not on the same
scale, which is why :class:`MatchResult` records which one produced the score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pixelforge.geometry.mapper import Point, Rect, Size

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

__all__ = [
    "DEFAULT_SCALES",
    "DEFAULT_THRESHOLD",
    "MatchResult",
    "diff_ratio",
    "match_template",
]

DEFAULT_THRESHOLD = 0.90
# +/-20% in 5% steps. Wider than this starts matching unrelated things; the
# scales are tried best-first so the common exact match costs one pass.
DEFAULT_SCALES: tuple[float, ...] = (1.0, 0.95, 1.05, 0.9, 1.1, 0.85, 1.15, 0.8, 1.2)


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Outcome of one template search.

    ``score`` is only comparable against results from the same ``metric``: the
    masked and unmasked metrics are differently normalised.
    """

    found: bool
    score: float
    box: Rect | None
    scale: float
    threshold: float
    metric: str
    scores_by_scale: tuple[tuple[float, float], ...] = field(default=())

    @property
    def center(self) -> Point | None:
        return self.box.center if self.box else None

    def explain(self) -> str:
        """One line for the timeline. Failures say how close they came.

        'best 0.62 at scale 1.00' is actionable in a way that 'not found' is not:
        a near miss means the template is stale, a low score means the page is
        not the one expected.
        """
        if self.found and self.box:
            return (
                f"matched {self.score:.3f} >= {self.threshold:.2f} "
                f"at ({self.box.center.x:.0f},{self.box.center.y:.0f}) scale {self.scale:.2f}"
            )
        return (
            f"no match: best {self.score:.3f} < {self.threshold:.2f} "
            f"at scale {self.scale:.2f}"
        )


def _to_gray(image: "np.ndarray") -> "np.ndarray":
    import cv2

    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        image = image[:, :, :3]
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def match_template(
    haystack: "np.ndarray",
    needle: "np.ndarray",
    *,
    roi: Rect | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    mask: "np.ndarray | None" = None,
    scales: tuple[float, ...] = DEFAULT_SCALES,
    grayscale: bool = True,
) -> MatchResult:
    """Find ``needle`` inside ``haystack``, returning coordinates in haystack space.

    ``roi`` restricts the search; the returned box is still in full-image
    coordinates, so callers never have to remember to add the offset back --
    forgetting that is a classic source of taps landing at the top-left corner.
    """
    import cv2
    import numpy as np

    if haystack.size == 0 or needle.size == 0:
        raise ValueError("cannot match an empty image")

    offset_x, offset_y = 0, 0
    search = haystack
    if roi is not None:
        box = roi.clamped_to(Size(haystack.shape[1], haystack.shape[0]))
        search = haystack[box.y : box.bottom, box.x : box.right]
        offset_x, offset_y = box.x, box.y

    if grayscale:
        search = _to_gray(search)
        needle = _to_gray(needle)
        if mask is not None and mask.ndim == 3:
            mask = _to_gray(mask)

    # A template with (almost) no variance cannot identify a location: it matches
    # every region of the same colour equally well. TM_CCOEFF_NORMED subtracts
    # the mean, so on a uniform template the correlation is mathematically
    # undefined and OpenCV returns a degenerate 1.0 at the first position --
    # a confident-looking false positive at (0, 0). Refuse it instead, and say
    # what to do about it.
    probe = _to_gray(needle) if grayscale else needle
    if mask is None and float(np.std(probe)) < 1.0:
        raise ValueError(
            "template is nearly uniform (std "
            f"{float(np.std(probe)):.3f}), so it cannot identify a unique "
            "location -- every region of that colour matches equally. Re-crop it "
            "to include an edge, an icon or some text."
        )

    metric = "TM_CCORR_NORMED" if mask is not None else "TM_CCOEFF_NORMED"
    method = cv2.TM_CCORR_NORMED if mask is not None else cv2.TM_CCOEFF_NORMED

    best_score = -1.0
    best_box: Rect | None = None
    best_scale = 1.0
    per_scale: list[tuple[float, float]] = []

    for scale in scales:
        if scale == 1.0:
            scaled, scaled_mask = needle, mask
        else:
            width = max(1, int(round(needle.shape[1] * scale)))
            height = max(1, int(round(needle.shape[0] * scale)))
            interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
            scaled = cv2.resize(needle, (width, height), interpolation=interpolation)
            scaled_mask = (
                cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
                if mask is not None
                else None
            )
        if scaled.shape[0] > search.shape[0] or scaled.shape[1] > search.shape[1]:
            continue  # template larger than the search area at this scale

        kwargs = {"mask": scaled_mask} if scaled_mask is not None else {}
        result = cv2.matchTemplate(search, scaled, method, **kwargs)
        # A masked correlation can produce inf/NaN where the mask covers a
        # uniform patch; left in, those win the argmax and report a perfect
        # match on a blank region.
        result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, max_value, _, max_location = cv2.minMaxLoc(result)
        per_scale.append((scale, float(max_value)))

        if max_value > best_score:
            best_score = float(max_value)
            best_scale = scale
            best_box = Rect(
                x=int(max_location[0]) + offset_x,
                y=int(max_location[1]) + offset_y,
                width=int(scaled.shape[1]),
                height=int(scaled.shape[0]),
            )
        if max_value >= threshold and scale == 1.0:
            break  # the common case: exact scale matches, skip the pyramid

    found = best_score >= threshold and best_box is not None
    return MatchResult(
        found=found,
        score=max(best_score, 0.0),
        box=best_box if found else best_box,
        scale=best_scale,
        threshold=threshold,
        metric=metric,
        scores_by_scale=tuple(per_scale),
    )


def diff_ratio(first: "np.ndarray", second: "np.ndarray") -> float:
    """Fraction of pixels that differ meaningfully between two captures.

    Used to decide whether the screen has settled. Compares on a downscaled
    grayscale copy with a tolerance, so JPEG-ish noise and a blinking cursor do
    not read as motion while a sliding panel does.
    """
    import cv2
    import numpy as np

    if first.shape != second.shape:
        return 1.0
    a = _to_gray(first)
    b = _to_gray(second)
    if a.shape[0] > 360:
        scale = 360 / a.shape[0]
        size = (max(1, int(a.shape[1] * scale)), 360)
        a = cv2.resize(a, size, interpolation=cv2.INTER_AREA)
        b = cv2.resize(b, size, interpolation=cv2.INTER_AREA)
    delta = cv2.absdiff(a, b)
    changed = np.count_nonzero(delta > 8)  # 8/255 ignores encoder noise
    return float(changed) / float(delta.size)
