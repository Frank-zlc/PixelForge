"""Exporter interface.

PixelForge's own Step model is richer than most execution engines'. Exporting is
therefore a *downgrade*, and the rule is that a downgrade must be announced. An
exporter that quietly drops an assertion produces a script that passes in the IDE
and silently does less in production -- the worst possible failure, because
nothing looks wrong.

So every exporter returns warnings alongside its file, and each warning names the
step, what could not be represented, and what the options are. Those warnings are
also useful in the other direction: they are a list of what the target engine
would need to grow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pixelforge.script.model import Project, Script

__all__ = ["ExportResult", "ExportWarning", "Exporter"]


@dataclass(frozen=True, slots=True)
class ExportWarning:
    step_id: str | None
    lost: str
    suggestion: str

    def __str__(self) -> str:
        where = f"step {self.step_id}" if self.step_id else "script"
        return f"{where}: {self.lost} -- {self.suggestion}"


@dataclass(slots=True)
class ExportResult:
    filename: str
    content: str
    warnings: list[ExportWarning] = field(default_factory=list)
    media: dict[str, bytes] = field(default_factory=dict)

    @property
    def lossless(self) -> bool:
        return not self.warnings

    def report(self) -> str:
        if self.lossless:
            return f"{self.filename}: exported with nothing lost"
        lines = "\n".join(f"  - {warning}" for warning in self.warnings)
        return f"{self.filename}: {len(self.warnings)} thing(s) could not be represented\n{lines}"


@runtime_checkable
class Exporter(Protocol):
    name: str
    description: str

    def export(self, project: Project, script: Script) -> ExportResult: ...
