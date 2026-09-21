"""Exporter registry."""

from __future__ import annotations

from pixelforge.exporters.albionhelper import AlbionHelperExporter
from pixelforge.exporters.base import Exporter, ExportResult
from pixelforge.exporters.pixelforge_json import PixelForgeJsonExporter
from pixelforge.exporters.pytest_export import PytestExporter
from pixelforge.script.model import Project, Script

__all__ = ["EXPORTERS", "export", "list_exporters"]

EXPORTERS: dict[str, Exporter] = {
    exporter.name: exporter
    for exporter in (PixelForgeJsonExporter(), AlbionHelperExporter(), PytestExporter())
}


def list_exporters() -> list[dict[str, str]]:
    return [
        {"name": name, "description": exporter.description}
        for name, exporter in EXPORTERS.items()
    ]


def export(name: str, project: Project, script: Script) -> ExportResult:
    exporter = EXPORTERS.get(name)
    if exporter is None:
        raise KeyError(f"unknown exporter {name!r}; available: {sorted(EXPORTERS)}")
    return exporter.export(project, script)
