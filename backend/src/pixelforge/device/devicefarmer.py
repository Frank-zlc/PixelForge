"""DeviceFarmer/STF provider using only the supported HTTP API."""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from pixelforge.adb.client import AdbClient, DeviceProps
from pixelforge.adb.track import DeviceState, TrackedDevice, is_valid_serial
from pixelforge.device.provider import DeviceEvent, _diff

__all__ = ["DeviceFarmerError", "DeviceFarmerProvider"]


class DeviceFarmerError(RuntimeError):
    pass


class DeviceFarmerProvider:
    """Reserve STF devices and expose them through PixelForge's adb pipeline."""

    name = "devicefarmer"

    def __init__(
        self,
        adb: AdbClient,
        base_url: str,
        access_token: str,
        *,
        poll_interval_s: float = 3.0,
        timeout_s: float = 10.0,
        verify_ssl: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid DeviceFarmer base URL")
        if parsed.scheme != "https" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
            "devicefarmer",
        }:
            raise ValueError("remote DeviceFarmer URL must use HTTPS")
        if not access_token:
            raise ValueError("DeviceFarmer access token is required")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")

        self._adb = adb
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._poll_interval_s = poll_interval_s
        self._client_owned = client is None
        self._client = client or httpx.AsyncClient(
            headers=self._headers,
            timeout=timeout_s,
            verify=verify_ssl,
        )
        self._rows: dict[str, dict[str, Any]] = {}
        self._reserved: set[str] = set()
        self._endpoints: dict[str, str] = {}

    async def discover(self) -> list[TrackedDevice]:
        rows = await self._list_rows()
        return [
            TrackedDevice(serial=serial, state=self._state(row))
            for serial, row in sorted(rows.items())
        ]

    async def watch(
        self, known: dict[str, DeviceState] | None = None
    ) -> AsyncIterator[DeviceEvent]:
        states = dict(known or {})
        while True:
            try:
                devices = await self.discover()
                for event in _diff(states, devices):
                    yield event
                states = {device.serial: device.state for device in devices}
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, DeviceFarmerError):
                # Keep the last snapshot during a central-service outage. An
                # already-open edge adb session can remain useful while STF is down.
                pass
            await asyncio.sleep(self._poll_interval_s)

    async def properties(self, serial: str) -> DeviceProps:
        self._validate_serial(serial)
        row = self._rows.get(serial)
        if row is None:
            row = (await self._list_rows()).get(serial)
        if row is None:
            raise DeviceFarmerError(f"unknown DeviceFarmer device: {serial}")
        display = row.get("display") if isinstance(row.get("display"), dict) else {}
        width = _positive_int(display.get("width"))
        height = _positive_int(display.get("height"))
        density = _positive_int(display.get("density")) or _positive_int(row.get("density"))
        return DeviceProps(
            serial=serial,
            model=_optional_text(row.get("model")),
            manufacturer=_optional_text(row.get("manufacturer")),
            android_release=_optional_text(row.get("version")),
            sdk_int=_positive_int(row.get("sdk")),
            size=(width, height) if width and height else None,
            density=density,
        )

    async def acquire(self, serial: str, *, ttl_s: float) -> str:
        self._validate_serial(serial)
        await self._request(
            "POST",
            f"/api/v1/user/devices/{serial}",
            json={"timeout": _reservation_seconds(ttl_s)},
        )
        self._reserved.add(serial)

        try:
            payload = await self._request(
                "POST", f"/api/v1/user/devices/{serial}/remoteConnect"
            )
            endpoint = str(payload.get("remoteConnectUrl") or "").strip()
            if not endpoint:
                raise DeviceFarmerError("remoteConnect did not return remoteConnectUrl")
            await self._adb.connect(endpoint)
        except Exception:
            # On a co-located edge, STF and PixelForge can share the same adb
            # server and remoteConnect may intentionally be disabled.
            local = {device.serial for device in await self._adb.devices()}
            if serial not in local:
                await self.release(serial)
                raise
            endpoint = serial

        self._endpoints[serial] = endpoint
        return endpoint

    async def renew(self, serial: str, *, ttl_s: float) -> None:
        if serial not in self._reserved:
            return
        await self._request(
            "POST",
            f"/api/v1/user/devices/{serial}",
            json={"timeout": _reservation_seconds(ttl_s)},
        )

    async def release(self, serial: str) -> None:
        self._validate_serial(serial)
        endpoint = self._endpoints.pop(serial, None)
        if endpoint and endpoint != serial:
            with contextlib.suppress(Exception):
                await self._adb.disconnect(endpoint)
        if serial in self._reserved:
            with contextlib.suppress(Exception):
                await self._request(
                    "DELETE", f"/api/v1/user/devices/{serial}/remoteConnect"
                )
            with contextlib.suppress(Exception):
                await self._request("DELETE", f"/api/v1/user/devices/{serial}")
            self._reserved.discard(serial)

    async def adb_endpoint(self, serial: str) -> str:
        return self._endpoints.get(serial, serial)

    async def close(self) -> None:
        for serial in list(self._reserved):
            await self.release(serial)
        if self._client_owned:
            await self._client.aclose()

    async def _list_rows(self) -> dict[str, dict[str, Any]]:
        payload = await self._request("GET", "/api/v1/devices")
        raw_rows = payload.get("devices", []) if isinstance(payload, dict) else []
        rows = {
            str(row["serial"]): row
            for row in raw_rows
            if isinstance(row, dict)
            and row.get("serial")
            and is_valid_serial(str(row["serial"]))
        }
        self._rows = rows
        return rows

    def _state(self, row: dict[str, Any]) -> DeviceState:
        if not row.get("present") or not row.get("ready"):
            return DeviceState.OFFLINE
        # ``using`` may mean this same token already owns the device after a
        # PixelForge restart. Let the reservation endpoint make the authoritative
        # ownership decision instead of disabling the Acquire button here.
        return DeviceState.DEVICE

    async def _request(self, method: str, path: str, **kwargs: object) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                self._base_url + path,
                headers=self._headers,
                **kwargs,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DeviceFarmerError(f"DeviceFarmer {method} {path} failed: {exc}") from exc
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise DeviceFarmerError(
                f"DeviceFarmer {path} returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise DeviceFarmerError(f"DeviceFarmer {path} returned a non-object response")
        if payload.get("success") is False:
            detail = payload.get("description") or payload.get("message") or "request rejected"
            raise DeviceFarmerError(f"DeviceFarmer {path}: {detail}")
        return payload

    @staticmethod
    def _validate_serial(serial: str) -> None:
        if not is_valid_serial(serial):
            raise ValueError(f"invalid DeviceFarmer serial: {serial!r}")


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _reservation_seconds(ttl_s: float) -> int:
    return min(86_400, max(1, math.ceil(ttl_s)))
