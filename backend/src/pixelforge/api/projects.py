"""Projects, scripts, templates and export."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from pixelforge.api.deps import SettingsDep, StoreDep
from pixelforge.exporters.registry import export as run_export
from pixelforge.exporters.registry import list_exporters
from pixelforge.listeners.registry import available_listeners
from pixelforge.script.model import Project, Script

router = APIRouter(prefix="/api", tags=["projects"])


class ExportRequest(BaseModel):
    exporter: str = Field(min_length=1, max_length=40)


@router.get("/exporters")
async def exporters() -> list[dict[str, str]]:
    return list_exporters()


@router.get("/listeners")
async def listeners() -> list[dict[str, str]]:
    return available_listeners()


@router.get("/storage")
async def storage(
    settings: SettingsDep,
    store: StoreDep,
    project_id: str | None = None,
) -> dict[str, str | None]:
    """Report real save locations without exposing any secret configuration."""
    project_templates = None
    if project_id:
        try:
            store.get(project_id)
            project_templates = str(store.templates_dir(project_id).resolve())
        except (KeyError, ValueError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {
        "asset_root": str(settings.assets_dir.resolve()),
        "project_templates": project_templates,
    }


@router.get("/projects", response_model=list[Project])
async def list_projects(store: StoreDep) -> list[Project]:
    return store.list_projects()


@router.post("/projects", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(project: Project, store: StoreDep) -> Project:
    if store.project_file(project.id).is_file():
        raise HTTPException(status.HTTP_409_CONFLICT, f"project {project.id!r} already exists")
    return store.save(project)


@router.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: str, store: StoreDep) -> Project:
    try:
        return store.get(project_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.put("/projects/{project_id}", response_model=Project)
async def update_project(project_id: str, project: Project, store: StoreDep) -> Project:
    if project.id != project_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "project id mismatch")
    return store.save(project)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, store: StoreDep) -> None:
    store.delete(project_id)


@router.put("/projects/{project_id}/scripts/{script_id}", response_model=Project)
async def save_script(
    project_id: str, script_id: str, script: Script, store: StoreDep
) -> Project:
    if script.id != script_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "script id mismatch")
    try:
        return store.save_script(project_id, script)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.delete("/projects/{project_id}/scripts/{script_id}", response_model=Project)
async def delete_script(project_id: str, script_id: str, store: StoreDep) -> Project:
    try:
        return store.delete_script(project_id, script_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/projects/{project_id}/templates")
async def templates(project_id: str, store: StoreDep) -> list[dict[str, object]]:
    try:
        return store.list_templates(project_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/projects/{project_id}/templates/{name}.png")
async def template_image(project_id: str, name: str, store: StoreDep) -> Response:
    try:
        path = store.templates_dir(project_id) / f"{name}.png"
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no template {name!r}")
    return Response(path.read_bytes(), media_type="image/png")


@router.post("/projects/{project_id}/scripts/{script_id}/export")
async def export_script(
    project_id: str, script_id: str, body: ExportRequest, store: StoreDep
) -> dict[str, object]:
    """Export, always reporting what could not be represented.

    The warnings are the point: an exporter that silently drops an assertion
    produces a script that passes in the IDE and quietly does less in production.
    """
    try:
        project = store.get(project_id)
        script = store.get_script(project_id, script_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    try:
        result = run_export(body.exporter, project, script)
    except KeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {
        "filename": result.filename,
        "content": result.content,
        "lossless": result.lossless,
        "warnings": [
            {"step_id": w.step_id, "lost": w.lost, "suggestion": w.suggestion}
            for w in result.warnings
        ],
        "report": result.report(),
    }
