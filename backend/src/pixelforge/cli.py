"""Command line entry point.

Exists for two reasons that the web UI cannot cover:

``run`` drives a flow headlessly, which is what makes an exported script usable as
a CI gate rather than something a person has to click through.

``doctor`` reports which optional pieces are present. This tool degrades in four
independent directions -- no adb, no scrcpy jar, no uiautomator server, no
Tesseract -- and each one silently removes a capability. "Why is the screenshot
button greyed out" should be answerable in one command rather than by reading
startup logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pixelforge.adb.client import AdbClient, AdbError, AdbNotFoundError
from pixelforge.config import Settings, get_settings

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pixelforge",
        description="Visual automation IDE for Android real devices",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API and UI")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")

    sub.add_parser("doctor", help="report which optional capabilities are available")
    sub.add_parser("devices", help="list attached devices")

    connect = sub.add_parser(
        "connect", help="attach a device over TCP/IP (wireless adb)"
    )
    connect.add_argument("address", help="host:port, e.g. 192.168.2.5:5555")

    disconnect = sub.add_parser("disconnect", help="detach a wireless device")
    disconnect.add_argument("address", nargs="?", help="omit to detach all")

    tcpip = sub.add_parser(
        "tcpip", help="switch a USB device to TCP mode, then report its address"
    )
    tcpip.add_argument("--device", required=True, help="adb serial (over USB)")
    tcpip.add_argument("--port", type=int, default=5555)

    run = sub.add_parser("run", help="run a script headlessly (the CI entry point)")
    run.add_argument("project")
    run.add_argument("script")
    run.add_argument("--device", required=True, help="adb serial")
    run.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")
    run.add_argument("--json", action="store_true", help="emit the result as JSON")

    export = sub.add_parser("export", help="export a script")
    export.add_argument("project")
    export.add_argument("script")
    export.add_argument("--exporter", default="pixelforge")
    export.add_argument("--out", type=Path, default=None, help="default: stdout")

    sub.add_parser("exporters", help="list available exporters")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    handlers = {
        "serve": _serve,
        "doctor": _doctor,
        "devices": _devices,
        "connect": _connect,
        "disconnect": _disconnect,
        "tcpip": _tcpip,
        "run": _run,
        "export": _export,
        "exporters": _exporters,
    }
    return handlers[args.command](args, settings)


# --------------------------------------------------------------------- serve


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    # One worker, always. Device sessions, scrcpy sockets, leases and in-flight
    # runs are process-local; a second worker would receive requests for devices
    # it has no session for.
    uvicorn.run(
        "pixelforge.main:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
        workers=1,
    )
    return 0


# -------------------------------------------------------------------- doctor


def _doctor(args: argparse.Namespace, settings: Settings) -> int:
    from pixelforge.vision.ocr import find_tesseract

    checks: list[tuple[str, bool, str]] = []

    adb = AdbClient(
        settings.adb_executable,
        server_host=settings.adb_server_host,
        server_port=settings.adb_server_port,
        timeout=settings.adb_timeout_s,
    )
    try:
        resolved = adb.resolve_executable()
        version = asyncio.run(adb.version()).splitlines()[0]
        checks.append(("adb", True, f"{version} ({resolved})"))
    except (AdbNotFoundError, AdbError, OSError) as exc:
        checks.append(
            ("adb", False, f"{exc} -- all device features are disabled without it")
        )

    jar = settings.vendor_dir / "scrcpy-server.jar"
    checks.append(
        (
            "scrcpy server jar",
            jar.is_file(),
            str(jar)
            if jar.is_file()
            else f"missing at {jar} -- no live video or low-latency control; "
            "screenshots and inspection still work (vendor/README.md)",
        )
    )

    apks = sorted(settings.vendor_dir.glob("*uiautomator2*.apk"))
    checks.append(
        (
            "uiautomator2 APKs",
            len(apks) >= 2,
            ", ".join(path.name for path in apks)
            if len(apks) >= 2
            else "missing -- accessibility selectors unavailable; template and OCR "
            "still work (vendor/README.md)",
        )
    )

    tesseract = find_tesseract()
    checks.append(
        (
            "Tesseract",
            tesseract is not None,
            tesseract or "not found -- the OCR locator is unavailable",
        )
    )

    frontend = settings.frontend_dir
    checks.append(
        (
            "frontend",
            frontend is not None,
            str(frontend) if frontend else "not found -- API only, no UI",
        )
    )

    width = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        print(f"  {'OK ' if ok else '-- '} {name.ljust(width)}  {detail}")

    # Only adb is fatal: everything else removes one capability, and saying so is
    # more useful than a single pass/fail.
    return 0 if checks[0][1] else 1


# ------------------------------------------------------------------- devices


def _devices(args: argparse.Namespace, settings: Settings) -> int:
    adb = AdbClient(
        settings.adb_executable,
        server_host=settings.adb_server_host,
        server_port=settings.adb_server_port,
        timeout=settings.adb_timeout_s,
    )

    async def collect() -> int:
        try:
            devices = await adb.devices()
        except (AdbNotFoundError, AdbError) as exc:
            print(f"adb unavailable: {exc}", file=sys.stderr)
            return 1
        if not devices:
            print("no devices attached")
            return 0
        for device in devices:
            props = None
            if device.usable:
                try:
                    props = await adb.props(device.serial)
                except AdbError:
                    props = None
            size = f"{props.size[0]}x{props.size[1]}" if props and props.size else "?"
            label = props.label if props else ""
            print(f"  {device.serial:24} {device.state.value:14} {size:11} {label}")
        return 0

    return asyncio.run(collect())


# ------------------------------------------------------------------ wireless


def _adb(settings: Settings) -> AdbClient:
    return AdbClient(
        settings.adb_executable,
        server_host=settings.adb_server_host,
        server_port=settings.adb_server_port,
        timeout=settings.adb_timeout_s,
    )


def _connect(args: argparse.Namespace, settings: Settings) -> int:
    async def go() -> int:
        try:
            print(f"  {await _adb(settings).connect(args.address)}")
        except (ValueError, AdbError, AdbNotFoundError) as exc:
            print(f"{exc}", file=sys.stderr)
            print(
                "Check the phone is reachable at that address, and that wireless "
                "debugging is on (or run `pixelforge tcpip --device <serial>` once "
                "over USB first).",
                file=sys.stderr,
            )
            return 1
        return 0

    return asyncio.run(go())


def _disconnect(args: argparse.Namespace, settings: Settings) -> int:
    async def go() -> int:
        try:
            out = await _adb(settings).disconnect(args.address)
        except (AdbError, AdbNotFoundError) as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        print(f"  {out or 'disconnected'}")
        return 0

    return asyncio.run(go())


def _tcpip(args: argparse.Namespace, settings: Settings) -> int:
    async def go() -> int:
        adb = _adb(settings)
        try:
            print(f"  {await adb.tcpip(args.device, args.port)}")
            ip = await adb.device_ip(args.device)
        except (ValueError, AdbError, AdbNotFoundError) as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        if ip:
            print(f"  the cable can go now:  pixelforge connect {ip}:{args.port}")
        else:
            print(
                "  could not read the device's wlan0 address; find it under "
                "Settings > About > Status and run `pixelforge connect <ip>:"
                f"{args.port}`"
            )
        return 0

    return asyncio.run(go())


# ----------------------------------------------------------------------- run


def _run(args: argparse.Namespace, settings: Settings) -> int:
    from pixelforge.device.manager import SessionManager
    from pixelforge.device.scrcpy.session import ScrcpyConfig
    from pixelforge.script.executor import RunStatus, ScriptRunner
    from pixelforge.store.projects import ProjectStore
    from pixelforge.timeline.bus import EventKind, TimelineBus

    variables: dict[str, str] = {}
    for item in args.var:
        if "=" not in item:
            print(f"--var expects KEY=VALUE, got {item!r}", file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        variables[key] = value

    store = ProjectStore(settings.data_dir / "projects")
    try:
        project = store.get(args.project)
        script = store.get_script(args.project, args.script)
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    adb = AdbClient(
        settings.adb_executable,
        server_host=settings.adb_server_host,
        server_port=settings.adb_server_port,
        timeout=settings.adb_timeout_s,
    )
    sessions = SessionManager(
        adb, scrcpy_config=ScrcpyConfig(jar_path=settings.vendor_dir / "scrcpy-server.jar")
    )
    bus = TimelineBus()

    async def execute() -> int:
        try:
            props = await adb.props(args.device)
        except (AdbNotFoundError, AdbError) as exc:
            print(f"cannot reach {args.device}: {exc}", file=sys.stderr)
            return 1

        session, status = await sessions.open(
            args.device, props, templates_dir=store.templates_dir(project.id)
        )
        for note in status.notes:
            print(f"note: {note}", file=sys.stderr)

        runner = ScriptRunner(
            session,
            sessions.chain(),
            bus,
            templates_dir=store.templates_dir(project.id),
            ocr_language=project.ocr_language,
        )
        try:
            result = await runner.run(
                script, variables=project.resolve_variables(variables)
            )
        finally:
            await sessions.close(args.device)

        if args.json:
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        else:
            for step in result.steps:
                mark = "OK " if step.status is RunStatus.OK else "-- "
                line = f"  {mark} {step.step_id:8} {step.name}"
                if step.located:
                    line += f"  [{step.located.strategy.value} {step.located.score:.3f}]"
                print(line)
                # The diagnosis is the actionable part: it distinguishes "never
                # found" from "found but the tap did nothing".
                for detail in (step.error, step.diagnosis):
                    if detail:
                        print(f"        {detail}")
            print(f"\n{result.status.value} in {result.duration_ms / 1000:.1f}s")

        _ = bus.history(kinds={EventKind.RUN_END})
        return 0 if result.status is RunStatus.OK else 1

    return asyncio.run(execute())


# -------------------------------------------------------------------- export


def _export(args: argparse.Namespace, settings: Settings) -> int:
    from pixelforge.exporters.registry import export as run_export
    from pixelforge.exporters.registry import load_plugin_dir
    from pixelforge.store.projects import ProjectStore

    for directory in settings.exporter_plugins:
        load_plugin_dir(directory)

    store = ProjectStore(settings.data_dir / "projects")
    try:
        project = store.get(args.project)
        script = store.get_script(args.project, args.script)
        result = run_export(args.exporter, project, script)
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(result.content, encoding="utf-8")
        for name, payload in result.media.items():
            (args.out.parent / name).write_bytes(payload)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(result.content)

    # Warnings go to stderr so piping the file stays clean, and a lossy export
    # exits non-zero so CI notices rather than shipping a quietly reduced script.
    if result.warnings:
        print(f"\n{result.report()}", file=sys.stderr)
        return 3
    return 0


def _exporters(args: argparse.Namespace, settings: Settings) -> int:
    from pixelforge.exporters.registry import list_exporters, load_plugin_dir

    for directory in settings.exporter_plugins:
        load_plugin_dir(directory)
    for item in list_exporters():
        print(f"  {item['name']:22} {item['description']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
