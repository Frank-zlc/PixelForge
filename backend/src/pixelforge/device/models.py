"""Device-facing data models exposed over the API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from pixelforge.adb.client import DeviceProps
from pixelforge.adb.track import DeviceState

__all__ = ["DeviceCapabilities", "DeviceView", "DisplayInfo"]


class DisplayInfo(BaseModel):
    """Physical display facts. Feeds CoordinateMapper in P2."""

    model_config = ConfigDict(frozen=True)

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    density: int | None = Field(default=None, gt=0)
    rotation: int = Field(default=0, ge=0, le=3, description="0/1/2/3 = 0/90/180/270 degrees")


class DeviceCapabilities(BaseModel):
    """What can actually be done with this device right now.

    Kept explicit rather than inferred in the frontend: "why is the screenshot
    button greyed out" should be answerable from one payload.
    """

    model_config = ConfigDict(frozen=True)

    screenshot: bool = False
    control: bool = False
    video: bool = False
    ui_hierarchy: bool = False


class DeviceView(BaseModel):
    """A device as the API presents it."""

    model_config = ConfigDict(frozen=True)

    serial: str
    state: DeviceState
    label: str
    model: str | None = None
    manufacturer: str | None = None
    android_release: str | None = None
    sdk_int: int | None = None
    display: DisplayInfo | None = None
    capabilities: DeviceCapabilities = DeviceCapabilities()
    lease: dict[str, object] | None = None
    props_pending: bool = False
    last_error: str | None = None

    @classmethod
    def build(
        cls,
        *,
        serial: str,
        state: DeviceState,
        props: DeviceProps | None,
        rotation: int = 0,
        lease: dict[str, object] | None = None,
        props_pending: bool = False,
        last_error: str | None = None,
    ) -> DeviceView:
        display: DisplayInfo | None = None
        if props is not None and props.size is not None:
            display = DisplayInfo(
                width=props.size[0],
                height=props.size[1],
                density=props.density,
                rotation=rotation,
            )
        usable = state.usable
        return cls(
            serial=serial,
            state=state,
            label=props.label if props else serial,
            model=props.model if props else None,
            manufacturer=props.manufacturer if props else None,
            android_release=props.android_release if props else None,
            sdk_int=props.sdk_int if props else None,
            display=display,
            capabilities=DeviceCapabilities(
                screenshot=usable,
                control=usable,
                # Set in P1/P4 once the scrcpy session and uiautomator server
                # actually report ready; advertised as False until then so the
                # UI never offers a control that cannot work.
                video=False,
                ui_hierarchy=False,
            ),
            lease=lease,
            props_pending=props_pending,
            last_error=last_error,
        )
