"""scrcpy control protocol: binary messages sent to the device.

Why this exists instead of ``adb shell input tap``: that command spawns a JVM
per invocation (``app_process`` -> ``com.android.commands.input.Input``), costing
150-400ms, and it can only express complete gestures -- there is no way to hold
a finger down, move it, and lift it. The control socket costs under 10ms and
takes individual DOWN / MOVE / UP events, so drags, long presses and multi-touch
all become expressible.

It also fixes text input. ``adb shell input text`` cannot type non-ASCII at all,
which is why an ASCII-only guard tends to appear in adb wrappers. Here text is
just a length-prefixed UTF-8 blob, so CJK works with no IME involved.

One property of the touch message is worth knowing, because it removes a whole
class of bug: the payload carries the sender's idea of the screen size alongside
the coordinates, and the device rescales. So coordinates may be sent in *frame*
space as long as the matching frame size travels with them -- no conversion to
physical device pixels on our side, and one less place to be off by three.

Layouts follow scrcpy's ``ControlMessage``; all integers are big-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "Action",
    "Button",
    "ControlMessage",
    "ControlType",
    "Keycode",
    "MotionAction",
    "PointerId",
    "encode_key",
    "encode_scroll",
    "encode_text",
    "encode_touch",
]

_U16_MAX = 0xFFFF
_I16_MAX = 0x7FFF
# scrcpy caps a single INJECT_TEXT at 300 bytes and expects longer strings to be
# split by the client.
MAX_TEXT_BYTES = 300


class ControlType(IntEnum):
    INJECT_KEYCODE = 0
    INJECT_TEXT = 1
    INJECT_TOUCH_EVENT = 2
    INJECT_SCROLL_EVENT = 3
    BACK_OR_SCREEN_ON = 4
    EXPAND_NOTIFICATION_PANEL = 5
    EXPAND_SETTINGS_PANEL = 6
    COLLAPSE_PANELS = 7
    GET_CLIPBOARD = 8
    SET_CLIPBOARD = 9
    SET_DISPLAY_POWER = 10
    ROTATE_DEVICE = 11


class Action(IntEnum):
    """Key action (``android.view.KeyEvent``)."""

    DOWN = 0
    UP = 1


class MotionAction(IntEnum):
    """Touch action (``android.view.MotionEvent``)."""

    DOWN = 0
    UP = 1
    MOVE = 2


class Button(IntEnum):
    """``AMOTION_EVENT_BUTTON_*``. Touches use PRIMARY."""

    NONE = 0
    PRIMARY = 1 << 0
    SECONDARY = 1 << 1
    TERTIARY = 1 << 2


class PointerId(IntEnum):
    """Reserved pointer ids.

    Real multi-touch uses 0, 1, 2 ...; these sentinels mark synthetic input so
    the device can treat it as mouse-like where that matters.
    """

    MOUSE = -1
    GENERIC_FINGER = -2
    VIRTUAL_FINGER = -3


class Keycode(IntEnum):
    """The handful of ``KeyEvent`` codes an automation UI actually needs."""

    HOME = 3
    BACK = 4
    CALL = 5
    ENDCALL = 6
    DPAD_UP = 19
    DPAD_DOWN = 20
    DPAD_LEFT = 21
    DPAD_RIGHT = 22
    DPAD_CENTER = 23
    VOLUME_UP = 24
    VOLUME_DOWN = 25
    POWER = 26
    CAMERA = 27
    CLEAR = 28
    TAB = 61
    SPACE = 62
    ENTER = 66
    DEL = 67
    MENU = 82
    NOTIFICATION = 83
    SEARCH = 84
    PAGE_UP = 92
    PAGE_DOWN = 93
    ESCAPE = 111
    FORWARD_DEL = 112
    MOVE_HOME = 122
    MOVE_END = 123
    APP_SWITCH = 187


@dataclass(frozen=True, slots=True)
class ControlMessage:
    """An encoded message plus a human-readable label for the timeline."""

    payload: bytes
    label: str

    def __len__(self) -> int:
        return len(self.payload)


def _clamp_i32(value: float) -> int:
    return max(-(2**31), min(2**31 - 1, int(round(value))))


def _fixed_u16(value: float) -> int:
    """Map 0.0-1.0 onto scrcpy's unsigned 16-bit fixed point."""
    return max(0, min(_U16_MAX, int(round(value * _U16_MAX))))


def _fixed_i16(value: float) -> int:
    """Map -1.0-1.0 onto scrcpy's signed 16-bit fixed point."""
    return max(-_I16_MAX, min(_I16_MAX, int(round(value * _I16_MAX))))


def encode_touch(
    action: MotionAction,
    x: float,
    y: float,
    width: int,
    height: int,
    *,
    pointer_id: int = PointerId.GENERIC_FINGER,
    pressure: float = 1.0,
    buttons: int = Button.PRIMARY,
    action_button: int = 0,
) -> ControlMessage:
    """Encode a touch event.

    ``width``/``height`` are the dimensions the coordinates are expressed in --
    normally the video frame, not the physical display. The device does the
    final scaling, so passing frame coordinates with the frame size is both
    correct and one conversion cheaper than resolving to device pixels first.

    An UP event conventionally carries no buttons, matching what a real finger
    lift reports.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"screen size must be positive, got {width}x{height}")
    if action is MotionAction.UP and buttons == Button.PRIMARY and action_button == 0:
        buttons = Button.NONE
    return ControlMessage(
        struct.pack(
            ">BBqiiHHHii",
            ControlType.INJECT_TOUCH_EVENT,
            int(action),
            int(pointer_id),
            _clamp_i32(x),
            _clamp_i32(y),
            width,
            height,
            _fixed_u16(pressure),
            int(action_button),
            int(buttons),
        ),
        label=f"touch {action.name.lower()} ({x:.0f},{y:.0f})",
    )


def encode_key(
    action: Action,
    keycode: int,
    *,
    repeat: int = 0,
    meta_state: int = 0,
) -> ControlMessage:
    if not 0 <= keycode <= 0xFFFF:
        raise ValueError(f"implausible keycode: {keycode}")
    name = Keycode(keycode).name if keycode in set(Keycode) else str(keycode)
    return ControlMessage(
        struct.pack(
            ">BBiii",
            ControlType.INJECT_KEYCODE,
            int(action),
            int(keycode),
            int(repeat),
            int(meta_state),
        ),
        label=f"key {action.name.lower()} {name}",
    )


def encode_text(text: str) -> list[ControlMessage]:
    """Encode text as one or more INJECT_TEXT messages.

    Returns a list because scrcpy caps a single message at 300 bytes. Splitting
    happens on character boundaries, never inside a multi-byte sequence -- a
    split mid-character would put a mojibake fragment on the device.
    """
    if not text:
        return []
    messages: list[ControlMessage] = []
    chunk = ""
    chunk_bytes = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if chunk_bytes + size > MAX_TEXT_BYTES:
            messages.append(_encode_text_chunk(chunk))
            chunk, chunk_bytes = "", 0
        chunk += char
        chunk_bytes += size
    if chunk:
        messages.append(_encode_text_chunk(chunk))
    return messages


def _encode_text_chunk(text: str) -> ControlMessage:
    raw = text.encode("utf-8")
    preview = text if len(text) <= 20 else text[:20] + "..."
    return ControlMessage(
        struct.pack(">BI", ControlType.INJECT_TEXT, len(raw)) + raw,
        label=f"text {preview!r}",
    )


def encode_scroll(
    x: float,
    y: float,
    width: int,
    height: int,
    *,
    horizontal: float = 0.0,
    vertical: float = 0.0,
    buttons: int = Button.NONE,
) -> ControlMessage:
    if width <= 0 or height <= 0:
        raise ValueError(f"screen size must be positive, got {width}x{height}")
    return ControlMessage(
        struct.pack(
            ">BiiHHhhi",
            ControlType.INJECT_SCROLL_EVENT,
            _clamp_i32(x),
            _clamp_i32(y),
            width,
            height,
            _fixed_i16(horizontal),
            _fixed_i16(vertical),
            int(buttons),
        ),
        label=f"scroll ({x:.0f},{y:.0f}) h={horizontal:+.2f} v={vertical:+.2f}",
    )


def encode_simple(control_type: ControlType) -> ControlMessage:
    """Encode a message whose payload is just its type byte."""
    if control_type not in {
        ControlType.BACK_OR_SCREEN_ON,
        ControlType.EXPAND_NOTIFICATION_PANEL,
        ControlType.EXPAND_SETTINGS_PANEL,
        ControlType.COLLAPSE_PANELS,
        ControlType.ROTATE_DEVICE,
    }:
        raise ValueError(f"{control_type.name} carries a payload")
    # BACK_OR_SCREEN_ON additionally takes an action byte in scrcpy >= 1.22.
    if control_type is ControlType.BACK_OR_SCREEN_ON:
        return ControlMessage(
            struct.pack(">BB", control_type, int(Action.DOWN)), label="back"
        )
    return ControlMessage(
        struct.pack(">B", control_type), label=control_type.name.lower()
    )
