"""FastAPI application entry point.

Run with **one** worker::

    uvicorn pixelforge.main:app --port 8420

Device sessions (scrcpy sockets, adb forwards, leases, in-flight runs) are
process-local objects. Under ``--workers N`` a request lands on whichever worker
the OS picked, which is usually not the one holding that device -- the symptom is
"works, then randomly 409s". If this ever needs to outgrow one machine, the device
layer moves to its own process and leases move behind Redis; ``Lease.epoch`` is
already the right fencing primitive for that.

Startup is deliberately forgiving. A missing adb, a phone whose scrcpy will not
start, an absent uiautomator server or no Tesseract each disable one capability
and are reported through the API. None of them stops the server, because a UI that
loads and says what is wrong is far more useful than a process that refuses to
boot.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from pixelforge.adb.client import AdbClient, AdbNotFoundError
from pixelforge.api import capture as capture_api
from pixelforge.api import control as control_api
from pixelforge.api import devices as devices_api
from pixelforge.api import projects as projects_api
from pixelforge.api import runs as runs_api
from pixelforge.config import Settings, get_settings
from pixelforge.device.devicefarmer import DeviceFarmerProvider
from pixelforge.device.lease import LeaseManager
from pixelforge.device.manager import SessionManager
from pixelforge.device.provider import DeviceProvider, LocalAdbProvider
from pixelforge.device.registry import DeviceRegistry
from pixelforge.device.scrcpy.session import ScrcpyConfig
from pixelforge.exporters.registry import list_exporters, load_plugin_dir
from pixelforge.listeners.manager import ListenerManager
from pixelforge.store.projects import ProjectStore
from pixelforge.timeline.bus import TimelineBus
from pixelforge.ws import events as events_ws
from pixelforge.ws import screen as screen_ws
from pixelforge.ws import timeline as timeline_ws

logger = logging.getLogger(__name__)


def build_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        adb = AdbClient(
            config.adb_executable,
            server_host=config.adb_server_host,
            server_port=config.adb_server_port,
            timeout=config.adb_timeout_s,
        )
        leases = LeaseManager()
        provider: DeviceProvider
        if config.device_provider == "devicefarmer":
            assert config.devicefarmer_url is not None
            assert config.devicefarmer_access_token is not None
            provider = DeviceFarmerProvider(
                adb,
                config.devicefarmer_url,
                config.devicefarmer_access_token.get_secret_value(),
                poll_interval_s=config.devicefarmer_poll_interval_s,
                timeout_s=config.adb_timeout_s,
                verify_ssl=config.devicefarmer_verify_ssl,
            )
        else:
            provider = LocalAdbProvider(adb)
        registry = DeviceRegistry(provider, adb, leases)
        sessions = SessionManager(
            adb,
            scrcpy_config=ScrcpyConfig(jar_path=config.vendor_dir / "scrcpy-server.jar"),
        )
        store = ProjectStore(config.data_dir / "projects")
        bus = TimelineBus()
        listeners = ListenerManager(adb, bus)

        app.state.settings = config
        app.state.adb = adb
        app.state.leases = leases
        app.state.registry = registry
        app.state.sessions = sessions
        app.state.store = store
        app.state.bus = bus
        app.state.listeners = listeners
        app.state.adb_available = False

        try:
            resolved = adb.resolve_executable()
            version = await adb.version()
            app.state.adb_available = True
            logger.info("adb %s at %s (server port %d)", version, resolved, adb.server_port)
        except AdbNotFoundError:
            logger.error(
                "adb not found at %r -- device features are disabled. Set "
                "PIXELFORGE_ADB_EXECUTABLE or vendor a binary into "
                "vendor/platform-tools/.",
                config.adb_executable,
            )
        except Exception:
            logger.exception("adb probe failed; device features may be unavailable")

        if not (config.vendor_dir / "scrcpy-server.jar").is_file():
            logger.warning(
                "vendor/scrcpy-server.jar is missing -- live video and low-latency "
                "control are unavailable. Screenshots and inspection still work. "
                "See vendor/README.md."
            )
        for directory in config.exporter_plugins:
            loaded = load_plugin_dir(directory)
            if loaded:
                logger.info("exporter plugins from %s: %s", directory, ", ".join(loaded))

        if not sessions.ocr_available:
            logger.warning(
                "Tesseract not found -- the OCR locator is unavailable "
                "(a11y, template and coordinate strategies still work)."
            )

        if app.state.adb_available:
            await registry.start()
        try:
            yield
        finally:
            await runs_api.shutdown_runs()
            await listeners.close_all()
            await sessions.close_all()
            await registry.stop()

    app = FastAPI(
        title="PixelForge",
        version="0.1.0",
        summary="Visual automation IDE for Android real devices",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for router in (
        devices_api.router,
        capture_api.router,
        control_api.router,
        projects_api.router,
        runs_api.router,
        events_ws.router,
        screen_ws.router,
        timeline_ws.router,
    ):
        app.include_router(router)

    @app.get("/api/health", tags=["meta"])
    async def health() -> dict[str, object]:
        sessions: SessionManager = app.state.sessions
        return {
            "status": "ok",
            "adb_available": app.state.adb_available,
            "adb_server_port": config.adb_server_port,
            "device_provider": app.state.registry.provider_name,
            "devices": len(app.state.registry.list()),
            "open_sessions": sorted(sessions.statuses()),
            "capabilities": {
                "scrcpy_jar": (config.vendor_dir / "scrcpy-server.jar").is_file(),
                "ocr": sessions.ocr_available,
                "frontend": frontend is not None,
            },
            "exporters": [item["name"] for item in list_exporters()],
        }

    # The frontend is build-free static files, so it is served from the same
    # origin: no bundler, no node_modules, no CORS to configure in development.
    # Absent in a wheel install, where the API still has to come up without it.
    frontend = config.frontend_dir
    if frontend is not None:
        app.mount("/app", StaticFiles(directory=frontend, html=True), name="frontend")

        # Redirect rather than serving index.html at "/" directly. index.html
        # references "styles.css" and "app.js" relatively, so a page served at "/"
        # resolves them to "/styles.css" and "/app.js" -- outside the mount, and a
        # 404 each. Redirecting puts the document at "/app/", where the relative
        # paths land inside the mount.
        #
        # Mounting StaticFiles at "/" instead would also fix the paths, but it
        # swallows every unmatched request, so a mistyped API path would return
        # the HTML page instead of a 404.
        @app.get("/", include_in_schema=False)
        async def index() -> RedirectResponse:
            return RedirectResponse(url="/app/", status_code=307)
    else:
        logger.info(
            "no frontend directory found -- serving the API only. Set "
            "PIXELFORGE_FRONTEND_DIR to mount the UI."
        )

    return app


app = build_app()
