"""DeviceFarmer uses the public STF API and hands an adb endpoint upward."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from pixelforge.adb.track import DeviceState, TrackedDevice
from pixelforge.device.devicefarmer import DeviceFarmerProvider


@dataclass
class FakeAdb:
    connected: list[str] = field(default_factory=list)
    disconnected: list[str] = field(default_factory=list)
    local: list[TrackedDevice] = field(default_factory=list)

    async def connect(self, address: str) -> str:
        self.connected.append(address)
        return f"connected to {address}"

    async def disconnect(self, address: str) -> str:
        self.disconnected.append(address)
        return f"disconnected {address}"

    async def devices(self) -> list[TrackedDevice]:
        return self.local


@pytest.mark.asyncio
async def test_inventory_properties_reservation_and_release() -> None:
    requests: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/api/v1/devices":
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {
                            "serial": "phone-1",
                            "present": True,
                            "ready": True,
                            "using": False,
                            "manufacturer": "Xiaomi",
                            "model": "Redmi K30 5G",
                            "version": "10",
                            "sdk": 29,
                            "display": {"width": 1080, "height": 2400, "density": 440},
                        }
                    ]
                },
            )
        if request.url.path.endswith("/remoteConnect") and request.method == "POST":
            return httpx.Response(200, json={"remoteConnectUrl": "10.0.0.5:7400"})
        return httpx.Response(200, json={"success": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adb = FakeAdb()
    provider = DeviceFarmerProvider(
        adb,  # type: ignore[arg-type]
        "https://devices.example.test",
        "secret-token",
        client=client,
    )

    assert await provider.discover() == [TrackedDevice("phone-1", DeviceState.DEVICE)]
    props = await provider.properties("phone-1")
    assert props.label == "Xiaomi Redmi K30 5G"
    assert props.size == (1080, 2400)

    assert await provider.acquire("phone-1", ttl_s=60) == "10.0.0.5:7400"
    assert adb.connected == ["10.0.0.5:7400"]
    await provider.renew("phone-1", ttl_s=60)
    await provider.release("phone-1")
    assert adb.disconnected == ["10.0.0.5:7400"]
    assert ("DELETE", "/api/v1/user/devices/phone-1", "Bearer secret-token") in requests
    await client.aclose()


@pytest.mark.asyncio
async def test_ready_device_stays_acquirable_when_stf_reports_using() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "devices": [
                    {
                        "serial": "busy",
                        "present": True,
                        "ready": True,
                        "using": True,
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeviceFarmerProvider(
        FakeAdb(),  # type: ignore[arg-type]
        "https://devices.example.test",
        "token",
        client=client,
    )
    # Reservation POST is authoritative. ``using`` may be our own reservation
    # surviving a PixelForge restart, so inventory alone must not disable it.
    assert (await provider.discover())[0].state is DeviceState.DEVICE
    await client.aclose()


def test_remote_plain_http_is_rejected() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        DeviceFarmerProvider(
            FakeAdb(),  # type: ignore[arg-type]
            "http://devices.example.test",
            "token",
        )
