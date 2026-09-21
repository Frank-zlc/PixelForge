"""The Locator interface and the degradation chain.

This is the extension point that makes one tool cover both a normal app and a
game. Rather than asking the user to choose a mode, every target carries all four
descriptions and the chain tries them in order, stopping at the first hit.

The chain also produces the diagnosis. When every strategy fails it does not say
"not found" -- it reports what each one attempted and how close it came, because
'a11y: tree empty / template: best 0.62 at scale 1.0 / ocr: no match' tells you
the page is wrong, whereas 'template: best 0.89 vs threshold 0.90' tells you the
template is stale. Those need opposite fixes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pixelforge.geometry.mapper import Point, Rect, Size
from pixelforge.script.model import Strategy, Target

__all__ = [
    "LocateContext",
    "LocateFailure",
    "LocateResult",
    "Locator",
    "LocatorChain",
]


@dataclass
class LocateContext:
    """What a locator may look at. Assembled once per attempt and shared.

    Screenshot and hierarchy are lazily provided callables rather than values so
    a chain that resolves on the accessibility tree never pays for a screenshot,
    and one that needs both pays for each once.
    """

    display: Size
    screenshot: object | None = None  # np.ndarray, lazily filled
    nodes: list[object] = field(default_factory=list)
    templates_dir: object | None = None
    ocr_language: str = "eng"
    a11y_available: bool = True


@dataclass(frozen=True, slots=True)
class LocateResult:
    strategy: Strategy
    box: Rect
    score: float
    detail: str = ""

    @property
    def center(self) -> Point:
        return self.box.center


class LocateFailure(Exception):
    """Every strategy failed, with what each one saw.

    A plain exception rather than a dataclass: ``@dataclass(frozen=True,
    slots=True)`` on an Exception subclass leaves ``args`` empty and interferes
    with pickling and ``copy``, which matters the moment one of these crosses a
    process boundary into the CV worker pool.
    """

    def __init__(
        self, target: Target, attempts: tuple[tuple[Strategy, str], ...]
    ) -> None:
        self.target = target
        self.attempts = attempts
        super().__init__(self._describe())

    def _describe(self) -> str:
        if not self.attempts:
            return "no strategy was applicable to this target"
        lines = " | ".join(f"{s.value}: {why}" for s, why in self.attempts)
        return f"element not found -- {lines}"

    def __str__(self) -> str:
        return self._describe()


@runtime_checkable
class Locator(Protocol):
    strategy: Strategy

    def applicable(self, target: Target, context: LocateContext) -> bool: ...

    async def locate(
        self, target: Target, context: LocateContext
    ) -> LocateResult | None: ...

    def why_not(self, target: Target, context: LocateContext) -> str:
        """Human explanation for the diagnosis line when this strategy was skipped."""
        ...


class LocatorChain:
    """Tries locators in the target's declared order."""

    def __init__(self, locators: dict[Strategy, Locator]) -> None:
        self._locators = locators

    async def locate(self, target: Target, context: LocateContext) -> LocateResult:
        attempts: list[tuple[Strategy, str]] = []
        for strategy in target.strategy:
            locator = self._locators.get(strategy)
            if locator is None:
                continue
            if getattr(target, strategy.value) is None:
                continue  # this target simply was not recorded with that data
            if not locator.applicable(target, context):
                attempts.append((strategy, locator.why_not(target, context)))
                continue
            try:
                result = await locator.locate(target, context)
            except Exception as exc:  # noqa: BLE001
                # One broken strategy must not abort the chain: a crashed OCR
                # binary should still let template matching answer.
                attempts.append((strategy, f"{type(exc).__name__}: {exc}"))
                continue
            if result is not None:
                return result
            attempts.append((strategy, locator.why_not(target, context)))
        raise LocateFailure(target=target, attempts=tuple(attempts))

    async def try_locate(
        self, target: Target, context: LocateContext
    ) -> LocateResult | None:
        try:
            return await self.locate(target, context)
        except LocateFailure:
            return None
