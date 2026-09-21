"""Device listing and session lifecycle.

Acquiring a session does two things at once: takes an exclusive lease, and brings
up the device-side pieces (scrcpy server, uiautomator server). They are one
operation because a lease without a live session is useless and a session without
a lease is unsafe.

Bring-up reports partial capability rather than failing. A game with no
accessibility tree still works through template matching; a device whose scrcpy
will not start can still be inspected and screenshotted. Refusing the whole
session would throw that away, so the response says exactly what is available.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, status
from pydantic import BaseModel, Field

from pixelforge.adb.client import AdbError
from pixelforge.api.deps import (
    AdbDep,
    LeasesDep,
    ListenersDep,
    RegistryDep,
    SessionsDep,
    StoreDep,
    require_lease,
)
from pixelforge.device.lease import (
    DeviceBusyError,
    LeaseError,
    LeaseExpiredError,
    LeaseSupersededError,
)
from pixelforge.device.models import DeviceView

router = APIRouter(prefix="/api/devices", tags=["devices"])


class SessionRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=120)
    ttl_s: float | None = Field(default=None, gt=0, le=3600)
    force: bool = Field(
        default=False,
        description="Take over a device someone else holds; the previous holder is fenced off.",
    )
    project_id: str | None = Field(default=None, max_length=64)


class Capabilities(BaseModel):
    video: bool
    control: bool
    a11y: bool
    screenshot: bool
    secure_screen: bool


class SessionResponse(BaseModel):
    token: str
    epoch: int
    device: DeviceView
    expires_in_s: float
    capabilities: Capabilities
    display: list[int] | None = None
    frame: list[int] | None = None
    rotation: int = 0
    notes: list[str] = Field(default_factory=list)
    listeners: list[dict[str, object]] = Field(default_factory=list)


class RenewRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)


class ConnectRequest(BaseModel):
    address: str = Field(
        min_length=3,
        max_length=259,
        description="host:port, e.g. 192.168.2.5:5555",
    )


class TcpipRequest(BaseModel):
    port: int = Field(default=5555, ge=1024, le=65535)


class ListenerStartRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)


@router.post("/connect")
async def connect_wireless(body: ConnectRequest, adb: AdbDep) -> dict[str, object]:
    """Attach a device over TCP/IP.

    The practical use: a phone already on this machine's hotspot can be driven
    over that same link, which sidesteps USB entirely -- handy when the cable
    keeps dropping or the phone's USB mode gets switched away from debugging.
    The tracker picks the device up on its own once adb has it.
    """
    try:
        message = await adb.connect(body.address)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except AdbError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"{exc}. Check the phone is reachable at that address and that "
            "wireless debugging (or `adb tcpip`) is enabled on it.",
        ) from exc
    return {"address": body.address, "message": message}


@router.post("/disconnect")
async def disconnect_wireless(body: ConnectRequest, adb: AdbDep) -> dict[str, object]:
    return {"message": await adb.disconnect(body.address)}


@router.post("/{serial}/tcpip")
async def enable_tcpip(
    serial: str, body: TcpipRequest, registry: RegistryDep, adb: AdbDep
) -> dict[str, object]:
    """Switch a USB-attached device to TCP mode and report where to reach it.

    This is the one step that still needs the cable; afterwards it can go.
    """
    device = registry.get(serial)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown device: {serial}")
    try:
        # Read the address before restarting adbd. `adb tcpip` deliberately
        # tears down the USB transport for a moment, so asking for wlan0 after
        # it often races the reconnect and returns no address at all.
        address = await adb.device_ip(serial)
        message = await adb.tcpip(serial, body.port)
    except AdbError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {
        "message": message,
        "port": body.port,
        "device_ip": address,
        # Pre-filled so the user does not have to go hunting in Settings.
        "suggested_address": f"{address}:{body.port}" if address else None,
    }


@router.get("", response_model=list[DeviceView])
async def list_devices(registry: RegistryDep) -> list[DeviceView]:
    return registry.list()


@router.get("/{serial}", response_model=DeviceView)
async def get_device(serial: str, registry: RegistryDep) -> DeviceView:
    device = registry.get(serial)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown device: {serial}")
    return device


@router.post("/{serial}/session", response_model=SessionResponse)
async def open_session(
    serial: str,
    body: SessionRequest,
    registry: RegistryDep,
    leases: LeasesDep,
    sessions: SessionsDep,
    listeners: ListenersDep,
    store: StoreDep,
) -> SessionResponse:
    device = registry.get(serial)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown device: {serial}")
    if not device.state.usable:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"device {serial} is not ready (state={device.state.value})",
        )
    try:
        lease = leases.acquire(
            serial,
            owner=body.owner,
            ttl_s=body.ttl_s if body.ttl_s is not None else 30.0,
            force=body.force,
        )
    except DeviceBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    props = registry.props(serial)
    if props is None:
        leases.release(lease.token)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"device properties for {serial} are not available yet; retry shortly",
        )

    templates_dir = None
    project = None
    if body.project_id:
        try:
            project = store.get(body.project_id)
            templates_dir = store.templates_dir(body.project_id)
        except (KeyError, ValueError) as exc:
            leases.release(lease.token)
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    try:
        adb_serial = await registry.acquire(serial, ttl_s=lease.ttl_s)
        session, device_status = await sessions.open(
            serial,
            props,
            adb_serial=adb_serial,
            templates_dir=templates_dir,
        )
    except Exception as exc:
        leases.release(lease.token)
        await registry.release(serial)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not open {serial}: {exc}") from exc

    listener_statuses: list[dict[str, object]] = []
    if project is not None:
        listener_statuses = [
            item.as_dict()
            for item in await listeners.start(
                serial,
                adb_serial=session.serial,
                configs=project.listeners,
                app_package=project.app_package,
            )
        ]
        for item in listener_statuses:
            if item["error"]:
                device_status.notes.append(
                    f"listener {item['name']} unavailable: {item['error']}"
                )

    return SessionResponse(
        token=lease.token,
        epoch=lease.epoch,
        device=registry.get(serial) or device,
        expires_in_s=lease.remaining(),
        capabilities=Capabilities(
            video=device_status.video,
            control=device_status.control,
            a11y=device_status.a11y,
            screenshot=True,
            secure_screen=device_status.secure_screen,
        ),
        display=list(device_status.display) if device_status.display else None,
        frame=list(device_status.frame) if device_status.frame else None,
        rotation=device_status.rotation,
        notes=device_status.notes,
        listeners=listener_statuses,
    )


@router.post("/{serial}/session/renew", response_model=SessionResponse)
async def renew_session(
    serial: str,
    body: RenewRequest,
    registry: RegistryDep,
    leases: LeasesDep,
    sessions: SessionsDep,
    listeners: ListenersDep,
) -> SessionResponse:
    # Three distinct client actions, three statuses: 410 re-acquire after timeout,
    # 409 someone took over, 404 this token was never valid.
    try:
        lease = leases.renew(body.token)
    except LeaseExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, str(exc)) from exc
    except LeaseSupersededError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except LeaseError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if lease.device_id != serial:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "token does not belong to this device")
    try:
        await registry.renew(serial, ttl_s=lease.ttl_s)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"could not renew provider reservation for {serial}: {exc}",
        ) from exc

    device = registry.get(serial)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown device: {serial}")
    session = sessions.get(serial)
    device_status = session.status() if session else None
    return SessionResponse(
        token=lease.token,
        epoch=lease.epoch,
        device=device,
        expires_in_s=lease.remaining(),
        capabilities=Capabilities(
            video=bool(device_status and device_status.video),
            control=bool(device_status and device_status.control),
            a11y=bool(device_status and device_status.a11y),
            screenshot=True,
            secure_screen=bool(device_status and device_status.secure_screen),
        ),
        display=list(device_status.display) if device_status and device_status.display else None,
        frame=list(device_status.frame) if device_status and device_status.frame else None,
        rotation=device_status.rotation if device_status else 0,
        notes=device_status.notes if device_status else [],
        listeners=[item.as_dict() for item in listeners.statuses(serial)],
    )


@router.delete("/{serial}/session", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    serial: str,
    leases: LeasesDep,
    sessions: SessionsDep,
    listeners: ListenersDep,
    registry: RegistryDep,
    token: str = Body(embed=True, min_length=1, max_length=64),
) -> None:
    require_lease(leases, serial, token)
    await listeners.stop(serial)
    await sessions.close(serial)
    await registry.release(serial)
    leases.release(token)


@router.get("/{serial}/listeners")
async def listener_status(serial: str, listeners: ListenersDep) -> list[dict[str, object]]:
    return [item.as_dict() for item in listeners.statuses(serial)]


@router.post("/{serial}/listeners/start")
async def start_listeners(
    serial: str,
    body: ListenerStartRequest,
    leases: LeasesDep,
    sessions: SessionsDep,
    listeners: ListenersDep,
    store: StoreDep,
) -> list[dict[str, object]]:
    require_lease(leases, serial, body.token)
    session = sessions.get(serial)
    if session is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"no open session for {serial}")
    try:
        project = store.get(body.project_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return [
        item.as_dict()
        for item in await listeners.start(
            serial,
            adb_serial=session.serial,
            configs=project.listeners,
            app_package=project.app_package,
        )
    ]


@router.post("/{serial}/listeners/stop", status_code=status.HTTP_204_NO_CONTENT)
async def stop_listeners(
    serial: str,
    body: RenewRequest,
    leases: LeasesDep,
    listeners: ListenersDep,
) -> None:
    require_lease(leases, serial, body.token)
    await listeners.stop(serial)


@router.get("/{serial}/session/status")
async def session_status(serial: str, sessions: SessionsDep) -> dict[str, object]:
    session = sessions.get(serial)
    if session is None:
        return {"open": False}
    state = session.status()
    return {
        "open": True,
        "video": state.video,
        "control": state.control,
        "a11y": state.a11y,
        "display": list(state.display) if state.display else None,
        "frame": list(state.frame) if state.frame else None,
        "rotation": state.rotation,
        "secure_screen": state.secure_screen,
        "notes": state.notes,
        "frames_relayed": session.scrcpy.state.frames_relayed,
        "subscribers": session.scrcpy.state.subscribers,
    }
