"""Native export: the full Step model, nothing lost."""

from __future__ import annotations

import json

from pixelforge.exporters.base import ExportResult
from pixelforge.script.model import Project, Script

__all__ = ["PixelForgeJsonExporter"]


class PixelForgeJsonExporter:
    name = "pixelforge"
    description = "PixelForge native JSON -- every field, re-importable"

    def export(self, project: Project, script: Script) -> ExportResult:
        payload = {
            "schema": "pixelforge/script@1",
            "project": {
                "id": project.id,
                "name": project.name,
                "app_package": project.app_package,
                "ocr_language": project.ocr_language,
            },
            "script": script.model_dump(mode="json", exclude_none=True),
        }
        return ExportResult(
            filename=f"{script.id}.pixelforge.json",
            content=json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )
