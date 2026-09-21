"""scrcpy video stream: demux packets, never decode them.

The backend deliberately does no decoding. It splits the stream into packets and
forwards the bytes; the browser's ``VideoDecoder`` (WebCodecs) does the work on
the GPU. That keeps per-device CPU cost near zero, which is what makes running
several phones from one process viable -- decoding even two 1080p streams in
Python would saturate the event loop this process also uses for adb, control and
the API.

Stream layout (scrcpy with the default meta flags):

    device meta   64 bytes, NUL-padded device name   (first socket only)
    codec meta    u32 codec fourcc, u32 width, u32 height
    frame N       u64 pts-and-flags, u32 length, <length> bytes
    ...

The top two bits of the pts field are flags rather than time: CONFIG marks the
SPS/PPS parameter sets, KEY_FRAME marks an IDR.

**Why those two get cached.** H.264 is only decodable from a keyframe, and only
after the decoder has the parameter sets. A viewer that connects between
keyframes and is simply handed the live stream renders nothing at all -- not a
glitch, a permanently black canvas -- until the next IDR, which at a typical
game's keyframe interval can be seconds away. So the last config packet and the
last keyframe are retained and replayed to each new subscriber before live
frames start.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "Codec",
    "DEVICE_NAME_LEN",
    "NalType",
    "VideoPacket",
    "VideoStreamParser",
    "iter_nal_units",
    "split_annexb",
]

DEVICE_NAME_LEN = 64
_CODEC_META_LEN = 12
_FRAME_HEADER_LEN = 12

PACKET_FLAG_CONFIG = 1 << 63
PACKET_FLAG_KEY_FRAME = 1 << 62
_PTS_MASK = (1 << 62) - 1

# A packet larger than this is a desynchronised stream, not a real frame. Without
# the guard a bad length read would make us allocate gigabytes waiting for bytes
# that never come.
MAX_PACKET_BYTES = 32 * 1024 * 1024


class Codec(IntEnum):
    """scrcpy sends the codec as a big-endian fourcc."""

    H264 = 0x68323634  # 'h264'
    H265 = 0x68323635  # 'h265'
    AV1 = 0x61763031  # 'av01'

    @property
    def webcodecs_id(self) -> str:
        """The string ``VideoDecoder.configure({codec})`` expects.

        The H.264 profile/level suffix is a baseline-compatible placeholder; the
        browser reads the real parameters out of the in-band SPS, so this only
        has to be plausible enough to pass ``isConfigSupported``.
        """
        return {
            Codec.H264: "avc1.42E01E",
            Codec.H265: "hev1.1.6.L93.B0",
            Codec.AV1: "av01.0.04M.08",
        }[self]


class NalType(IntEnum):
    """H.264 NAL unit types worth recognising."""

    NON_IDR = 1
    IDR = 5
    SEI = 6
    SPS = 7
    PPS = 8
    AUD = 9


@dataclass(frozen=True, slots=True)
class VideoPacket:
    data: bytes
    pts_us: int | None
    is_config: bool
    is_keyframe: bool

    @property
    def bootstrap(self) -> bool:
        """Whether a new subscriber needs this packet before it can decode."""
        return self.is_config or self.is_keyframe


class VideoStreamParser:
    """Incremental parser: feed arbitrary byte chunks, get whole packets.

    Written as a fed state machine rather than around a reader so it can be
    tested exhaustively without sockets -- including the case that actually
    breaks naive implementations, a header split across two TCP reads.
    """

    def __init__(self, *, expect_device_meta: bool = True) -> None:
        self._buffer = bytearray()
        self._expect_device_meta = expect_device_meta
        self.device_name: str | None = None
        self.codec: Codec | None = None
        self.width: int | None = None
        self.height: int | None = None
        # Retained so a late subscriber can be bootstrapped; see module docstring.
        self.config_packet: VideoPacket | None = None
        self.last_keyframe: VideoPacket | None = None

    @property
    def ready(self) -> bool:
        """True once codec metadata has been read and packets can follow."""
        return self.codec is not None

    @property
    def size(self) -> tuple[int, int] | None:
        if self.width is None or self.height is None:
            return None
        return (self.width, self.height)

    def bootstrap_packets(self) -> list[VideoPacket]:
        """Packets to replay to a new subscriber, in decode order."""
        out = []
        if self.config_packet is not None:
            out.append(self.config_packet)
        if self.last_keyframe is not None:
            out.append(self.last_keyframe)
        return out

    def feed(self, chunk: bytes) -> Iterator[VideoPacket]:
        """Consume bytes and yield every complete packet they finish."""
        self._buffer += chunk
        while True:
            if self._expect_device_meta:
                if len(self._buffer) < DEVICE_NAME_LEN:
                    return
                raw = bytes(self._buffer[:DEVICE_NAME_LEN])
                del self._buffer[:DEVICE_NAME_LEN]
                self.device_name = raw.split(b"\x00", 1)[0].decode(
                    "utf-8", errors="replace"
                )
                self._expect_device_meta = False
                continue

            if self.codec is None:
                if len(self._buffer) < _CODEC_META_LEN:
                    return
                codec_id, width, height = struct.unpack_from(">III", self._buffer, 0)
                del self._buffer[:_CODEC_META_LEN]
                try:
                    self.codec = Codec(codec_id)
                except ValueError as exc:
                    raise ValueError(
                        f"unknown scrcpy codec id 0x{codec_id:08x}; the server jar "
                        "version probably does not match this client"
                    ) from exc
                self.width, self.height = width, height
                continue

            if len(self._buffer) < _FRAME_HEADER_LEN:
                return
            pts_and_flags, length = struct.unpack_from(">QI", self._buffer, 0)
            if length > MAX_PACKET_BYTES:
                raise ValueError(
                    f"implausible packet length {length}; stream is desynchronised"
                )
            if len(self._buffer) < _FRAME_HEADER_LEN + length:
                return  # header arrived, payload has not -- wait for more
            data = bytes(
                self._buffer[_FRAME_HEADER_LEN : _FRAME_HEADER_LEN + length]
            )
            del self._buffer[: _FRAME_HEADER_LEN + length]

            is_config = bool(pts_and_flags & PACKET_FLAG_CONFIG)
            is_keyframe = bool(pts_and_flags & PACKET_FLAG_KEY_FRAME)
            packet = VideoPacket(
                data=data,
                # A config packet carries parameter sets, not a moment in time.
                pts_us=None if is_config else pts_and_flags & _PTS_MASK,
                is_config=is_config,
                is_keyframe=is_keyframe,
            )
            if is_config:
                self.config_packet = packet
            elif is_keyframe:
                self.last_keyframe = packet
            yield packet


def split_annexb(data: bytes) -> list[bytes]:
    """Split an Annex-B buffer into NAL units, start codes stripped.

    Only needed when frame metadata is disabled, and for inspecting the parameter
    sets inside a config packet.
    """
    units: list[bytes] = []
    start = -1
    index = 0
    length = len(data)
    while index < length - 2:
        if data[index] == 0 and data[index + 1] == 0:
            if data[index + 2] == 1:
                code_len = 3
            elif (
                index < length - 3 and data[index + 2] == 0 and data[index + 3] == 1
            ):
                code_len = 4
            else:
                index += 1
                continue
            if start >= 0:
                unit = data[start:index]
                if unit:
                    units.append(unit)
            start = index + code_len
            index = start
            continue
        index += 1
    if start >= 0 and start < length:
        units.append(data[start:])
    return units


def iter_nal_units(data: bytes) -> Iterator[tuple[NalType | int, bytes]]:
    """Yield ``(nal_type, payload)`` for each NAL unit in an Annex-B buffer."""
    for unit in split_annexb(data):
        raw_type = unit[0] & 0x1F
        try:
            yield NalType(raw_type), unit
        except ValueError:
            yield raw_type, unit


def looks_like_keyframe(data: bytes) -> bool:
    """Detect an IDR or parameter set without frame metadata.

    The fallback for ``send_frame_meta=false``; the header flags are cheaper and
    authoritative when present.
    """
    return any(
        nal_type in (NalType.IDR, NalType.SPS, NalType.PPS)
        for nal_type, _ in iter_nal_units(data)
    )
