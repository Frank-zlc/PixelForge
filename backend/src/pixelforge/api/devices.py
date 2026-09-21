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

from pixelforge.api.deps import LeasesDep, RegistryDep, SessionsDep, StoreDep
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


class RenewRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)


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
    if body.project_id:
        try:
            templates_dir = store.templates_dir(body.project_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    try:
        _, device_status = await sessions.open(serial, props, templates_dir=templates_dir)
    except Exception as exc:  # noqa: BLE001
        leases.release(lease.token)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not open {serial}: {exc}") from exc

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
    )


@router.post("/{serial}/session/renew", response_model=SessionResponse)
async def renew_session(
    serial: str,
    body: RenewRequest,
    registry: RegistryDep,
    leases: LeasesDep,
    sessions: SessionsDep,
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
    )


@router.delete("/{serial}/session", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    serial: str,
    leases: LeasesDep,
    sessions: SessionsDep,
    token: str = Body(embed=True, min_length=1, max_length=64),
) -> None:
    # release() is idempotent, so a page-unload plus an explicit click is fine.
    leases.release(token)
    await sessions.close(serial)


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
