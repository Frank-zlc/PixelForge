"""Exporter registry, with third-party plugins.

Only two exporters ship in core, and both are business-agnostic: the native JSON
format and a pytest module. That is deliberate. An exporter that targets one
company's execution engine encodes *that engine's* schema -- its action enum, its
field names, its limits -- and shipping it here would make PixelForge carry
business knowledge it claims not to have. The second such engine would then want
its own, and the registry becomes a list of other people's formats.

So downstream formats arrive as plugins. A project drops a module into a directory
named by ``PIXELFORGE_EXPORTER_PLUGINS`` and PixelForge loads it at startup:

    # my_engine_exporter.py
    class MyEngineExporter:
        name = "my-engine"
        description = "My engine's step profile (lossy)"

        def export(self, project, script) -> ExportResult:
            ...

    EXPORTER = MyEngineExporter()

``examples/exporters/`` has a complete, working one to copy.

Plugin loading imports Python from a configured path, which is code execution. That
is the same trust level as the rest of this tool -- it already runs scripts that
drive a phone -- but it means plugin directories are as sensitive as the code
itself, and it is why discovery is explicit configuration rather than a scan of
the filesystem.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from pixelforge.exporters.base import Exporter, ExportResult
from pixelforge.exporters.pixelforge_json import PixelForgeJsonExporter
from pixelforge.exporters.pytest_export import PytestExporter
from pixelforge.script.model import Project, Script

logger = logging.getLogger(__name__)

__all__ = [
    "EXPORTERS",
    "export",
    "list_exporters",
    "load_plugin_dir",
    "register",
]

# Attribute a plugin module may expose, checked in this order.
_PLUGIN_ATTRS = ("EXPORTER", "get_exporter")

EXPORTERS: dict[str, Exporter] = {}


def register(exporter: Exporter, *, replace: bool = False) -> None:
    """Add an exporter.

    Refuses to shadow an existing name unless ``replace`` is set: a plugin that
    silently overrode the native exporter would produce the wrong file under a
    familiar name, which is worse than a startup error.
    """
    name = getattr(exporter, "name", "")
    if not name:
        raise ValueError("an exporter needs a non-empty name")
    if not callable(getattr(exporter, "export", None)):
        raise TypeError(f"exporter {name!r} has no export() method")
    if name in EXPORTERS and not replace:
        raise ValueError(
            f"exporter {name!r} is already registered; pass replace=True to override"
        )
    EXPORTERS[name] = exporter


for _builtin in (PixelForgeJsonExporter(), PytestExporter()):
    register(_builtin)


def load_plugin_dir(directory: Path) -> list[str]:
    """Import every ``*.py`` in a directory and register what it exposes.

    Returns the names loaded. A plugin that fails to import is logged and skipped
    rather than raising: one broken plugin must not stop the server, or a typo in
    an optional exporter takes the whole tool down.
    """
    loaded: list[str] = []
    if not directory.is_dir():
        logger.warning("exporter plugin directory not found: %s", directory)
        return loaded

    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            exporter = _load_module_exporter(path)
        except Exception as exc:  # noqa: BLE001 - a bad plugin is not fatal
            logger.error("could not load exporter plugin %s: %s", path.name, exc)
            continue
        if exporter is None:
            logger.warning(
                "%s defines no EXPORTER or get_exporter(); skipped", path.name
            )
            continue
        try:
            register(exporter)
        except (ValueError, TypeError) as exc:
            logger.error("could not register exporter from %s: %s", path.name, exc)
            continue
        loaded.append(exporter.name)
        logger.info("loaded exporter plugin %r from %s", exporter.name, path.name)
    return loaded


def _load_module_exporter(path: Path) -> Exporter | None:
    spec = importlib.util.spec_from_file_location(f"pixelforge_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for attribute in _PLUGIN_ATTRS:
        candidate = getattr(module, attribute, None)
        if candidate is None:
            continue
        return candidate() if callable(candidate) and attribute == "get_exporter" else candidate
    return None


def list_exporters() -> list[dict[str, str]]:
    return [
        {"name": name, "description": getattr(exporter, "description", "")}
        for name, exporter in sorted(EXPORTERS.items())
    ]


def export(name: str, project: Project, script: Script) -> ExportResult:
    exporter = EXPORTERS.get(name)
    if exporter is None:
        raise KeyError(f"unknown exporter {name!r}; available: {sorted(EXPORTERS)}")
    return exporter.export(project, script)
