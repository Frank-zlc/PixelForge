"""Project and script persistence, as plain JSON files.

Files rather than a database, deliberately. This is single-team tooling, the data
is small, and a script you can open in an editor, diff in git and paste into a bug
report is worth more here than transactional guarantees. Templates live beside
their project as ordinary PNGs for the same reason.

Writes go through a temp file and a rename so an interrupted save cannot leave a
truncated script behind.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from pixelforge.script.model import Project, Script

__all__ = ["ProjectStore"]


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- layout

    def project_dir(self, project_id: str) -> Path:
        _validate_id(project_id)
        return self.root / project_id

    def project_file(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def templates_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id) / "templates"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def captures_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id) / "captures"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -------------------------------------------------------------- crud

    def list_projects(self) -> list[Project]:
        projects: list[Project] = []
        for candidate in sorted(self.root.glob("*/project.json")):
            try:
                projects.append(Project.model_validate_json(candidate.read_text("utf-8")))
            except Exception:  # noqa: BLE001 - one bad file must not hide the rest
                continue
        return projects

    def get(self, project_id: str) -> Project:
        path = self.project_file(project_id)
        if not path.is_file():
            raise KeyError(f"unknown project: {project_id}")
        return Project.model_validate_json(path.read_text("utf-8"))

    def save(self, project: Project) -> Project:
        path = self.project_file(project.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            path, project.model_dump_json(indent=2, exclude_none=True) + "\n"
        )
        self.templates_dir(project.id)
        return project

    def delete(self, project_id: str) -> None:
        directory = self.project_dir(project_id)
        if directory.is_dir():
            shutil.rmtree(directory)

    # ------------------------------------------------------------ scripts

    def save_script(self, project_id: str, script: Script) -> Project:
        project = self.get(project_id)
        scripts = [item for item in project.scripts if item.id != script.id]
        scripts.append(script)
        scripts.sort(key=lambda item: item.id)
        updated = project.model_copy(update={"scripts": scripts})
        return self.save(updated)

    def get_script(self, project_id: str, script_id: str) -> Script:
        for script in self.get(project_id).scripts:
            if script.id == script_id:
                return script
        raise KeyError(f"unknown script {script_id!r} in project {project_id!r}")

    def delete_script(self, project_id: str, script_id: str) -> Project:
        project = self.get(project_id)
        updated = project.model_copy(
            update={"scripts": [s for s in project.scripts if s.id != script_id]}
        )
        return self.save(updated)

    # ---------------------------------------------------------- templates

    def save_template(self, project_id: str, name: str, png: bytes) -> Path:
        _validate_id(name.removesuffix(".png"))
        path = self.templates_dir(project_id) / f"{name.removesuffix('.png')}.png"
        path.write_bytes(png)
        return path

    def list_templates(self, project_id: str) -> list[dict[str, object]]:
        return [
            {"name": path.stem, "bytes": path.stat().st_size}
            for path in sorted(self.templates_dir(project_id).glob("*.png"))
        ]


def _validate_id(value: str) -> None:
    # These become path segments, so anything that could escape the project
    # directory is rejected outright rather than sanitised.
    if not value or not all(char.isalnum() or char in "-_" for char in value):
        raise ValueError(
            f"invalid identifier {value!r}: use letters, digits, '-' and '_' only"
        )


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
