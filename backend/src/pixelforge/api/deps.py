"""Shared dependencies and the lease check every device-touching route makes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from pixelforge.adb.client import AdbClient
from pixelforge.config import Settings
from pixelforge.device.lease import Lease, LeaseManager
from pixelforge.device.manager import SessionManager
from pixelforge.device.registry import DeviceRegistry
from pixelforge.device.session import DeviceSession
from pixelforge.store.projects import ProjectStore
from pixelforge.timeline.bus import TimelineBus

__all__ = [
    "AdbDep",
    "BusDep",
    "LeasesDep",
    "RegistryDep",
    "SessionsDep",
    "SettingsDep",
    "StoreDep",
    "require_lease",
    "require_session",
]


def _state(request: Request, name: str) -> object:
    value = getattr(request.app.state, name, None)
    if value is None:  # pragma: no cover - misconfiguration, not a user error
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{name} unavailable")
    return value


def get_registry(request: Request) -> DeviceRegistry:
    return _state(request, "registry")  # type: ignore[return-value]


def get_leases(request: Request) -> LeaseManager:
    return _state(request, "leases")  # type: ignore[return-value]


def get_adb(request: Request) -> AdbClient:
    return _state(request, "adb")  # type: ignore[return-value]


def get_sessions(request: Request) -> SessionManager:
    return _state(request, "sessions")  # type: ignore[return-value]


def get_store(request: Request) -> ProjectStore:
    return _state(request, "store")  # type: ignore[return-value]


def get_bus(request: Request) -> TimelineBus:
    return _state(request, "bus")  # type: ignore[return-value]


def get_settings(request: Request) -> Settings:
    return _state(request, "settings")  # type: ignore[return-value]


RegistryDep = Annotated[DeviceRegistry, Depends(get_registry)]
LeasesDep = Annotated[LeaseManager, Depends(get_leases)]
AdbDep = Annotated[AdbClient, Depends(get_adb)]
SessionsDep = Annotated[SessionManager, Depends(get_sessions)]
StoreDep = Annotated[ProjectStore, Depends(get_store)]
BusDep = Annotated[TimelineBus, Depends(get_bus)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def require_lease(leases: LeaseManager, serial: str, token: str) -> Lease:
    """Every action that touches a device passes through here.

    Exclusivity is not advisory: without it two operators interleave taps on one
    phone and produce a failure neither can reproduce.
    """
    lease = leases.current(serial)
    if lease is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"no active session on {serial}; acquire one first",
        )
    if lease.token != token:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{serial} is held by {lease.owner}; your token is not current",
        )
    return lease


def require_session(sessions: SessionManager, serial: str) -> DeviceSession:
    try:
        return sessions.require(serial)
    except KeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
