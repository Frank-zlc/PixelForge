"""scrcpy wire protocol: control messages and video demuxing.

Both halves are pure byte manipulation, so they can be pinned down exactly
without a device. The sizes asserted here come from scrcpy's ``ControlMessage``
layout -- if a server upgrade changes them, these tests fail loudly instead of
the device silently ignoring malformed input.
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest

from pixelforge.device.scrcpy.control import (
    MAX_TEXT_BYTES,
    Action,
    Button,
    ControlType,
    Keycode,
    MotionAction,
    PointerId,
    encode_key,
    encode_scroll,
    encode_simple,
    encode_text,
    encode_touch,
)
from pixelforge.device.scrcpy.session import ScrcpyConfig, ScrcpySession
from pixelforge.device.scrcpy.video import (
    DEVICE_NAME_LEN,
    PACKET_FLAG_CONFIG,
    PACKET_FLAG_KEY_FRAME,
    Codec,
    NalType,
    VideoPacket,
    VideoStreamParser,
    iter_nal_units,
    looks_like_keyframe,
    split_annexb,
)

# --------------------------------------------------------------------- control


class TestTouch:
    def test_wire_size_and_type(self) -> None:
        msg = encode_touch(MotionAction.DOWN, 100, 200, 1080, 2400)
        assert len(msg) == 32, "scrcpy INJECT_TOUCH_EVENT is 32 bytes"
        assert msg.payload[0] == ControlType.INJECT_TOUCH_EVENT
        assert msg.payload[1] == MotionAction.DOWN

    def test_carries_the_screen_size_with_the_coordinates(self) -> None:
        # This is what lets us send frame coordinates and let the device do the
        # final scaling, removing one conversion from the pipeline.
        msg = encode_touch(MotionAction.DOWN, 100, 200, 1080, 2400)
        _, _, _, x, y, width, height = struct.unpack_from(">BBqiiHH", msg.payload, 0)
        assert (x, y, width, height) == (100, 200, 1080, 2400)

    def test_pressure_is_fixed_point(self) -> None:
        # Layout offsets: type@0 action@1 pointerId@2 x@10 y@14 w@18 h@20
        # pressure@22 actionButton@24 buttons@28.
        def pressure_at(value: float) -> int:
            msg = encode_touch(MotionAction.DOWN, 0, 0, 100, 100, pressure=value)
            return struct.unpack_from(">H", msg.payload, 22)[0]

        assert pressure_at(1.0) == 0xFFFF
        assert pressure_at(0.5) == pytest.approx(0x8000, abs=2)
        assert pressure_at(0.0) == 0
        assert pressure_at(5.0) == 0xFFFF, "out-of-range pressure clamps"

    def test_up_clears_buttons(self) -> None:
        # A real finger lift reports no buttons held.
        up = encode_touch(MotionAction.UP, 0, 0, 100, 100)
        assert struct.unpack_from(">i", up.payload, 28)[0] == Button.NONE
        down = encode_touch(MotionAction.DOWN, 0, 0, 100, 100)
        assert struct.unpack_from(">i", down.payload, 28)[0] == Button.PRIMARY

    def test_negative_pointer_ids_survive(self) -> None:
        # Sentinels are negative; an unsigned field would corrupt them.
        msg = encode_touch(
            MotionAction.DOWN, 0, 0, 100, 100, pointer_id=PointerId.VIRTUAL_FINGER
        )
        assert struct.unpack_from(">q", msg.payload, 2)[0] == -3

    def test_multitouch_pointer_ids(self) -> None:
        for pid in (0, 1, 2):
            msg = encode_touch(MotionAction.DOWN, 0, 0, 100, 100, pointer_id=pid)
            assert struct.unpack_from(">q", msg.payload, 2)[0] == pid

    def test_rejects_zero_screen_size(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            encode_touch(MotionAction.DOWN, 0, 0, 0, 100)

    def test_coordinates_are_clamped_to_int32(self) -> None:
        msg = encode_touch(MotionAction.DOWN, 1e12, -1e12, 100, 100)
        x, y = struct.unpack_from(">ii", msg.payload, 10)
        assert x == 2**31 - 1 and y == -(2**31)

    def test_drag_is_expressible(self) -> None:
        """down/move/up as separate events -- the thing `input swipe` cannot do."""
        seq = [
            encode_touch(MotionAction.DOWN, 10, 10, 100, 100),
            encode_touch(MotionAction.MOVE, 50, 50, 100, 100),
            encode_touch(MotionAction.MOVE, 90, 90, 100, 100),
            encode_touch(MotionAction.UP, 90, 90, 100, 100),
        ]
        assert [m.payload[1] for m in seq] == [0, 2, 2, 1]


class TestKey:
    def test_wire_size(self) -> None:
        msg = encode_key(Action.DOWN, Keycode.HOME)
        assert len(msg) == 14
        assert msg.payload[0] == ControlType.INJECT_KEYCODE

    def test_fields(self) -> None:
        msg = encode_key(Action.UP, Keycode.BACK, repeat=2, meta_state=0x1000)
        _, action, keycode, repeat, meta = struct.unpack(">BBiii", msg.payload)
        assert (action, keycode, repeat, meta) == (1, 4, 2, 0x1000)

    def test_label_names_known_keys(self) -> None:
        assert "HOME" in encode_key(Action.DOWN, Keycode.HOME).label
        assert "9999" in encode_key(Action.DOWN, 9999).label

    def test_rejects_implausible_keycode(self) -> None:
        with pytest.raises(ValueError, match="implausible keycode"):
            encode_key(Action.DOWN, -1)


class TestText:
    def test_ascii(self) -> None:
        (msg,) = encode_text("hello")
        assert msg.payload[0] == ControlType.INJECT_TEXT
        assert struct.unpack_from(">I", msg.payload, 1)[0] == 5
        assert msg.payload[5:] == b"hello"

    def test_cjk_works(self) -> None:
        """The whole point: `adb shell input text` cannot type this at all."""
        (msg,) = encode_text("你好世界")
        payload = msg.payload[5:]
        assert payload.decode("utf-8") == "你好世界"
        assert struct.unpack_from(">I", msg.payload, 1)[0] == len(payload) == 12

    def test_emoji(self) -> None:
        (msg,) = encode_text("ok 👍")
        assert msg.payload[5:].decode("utf-8") == "ok 👍"

    def test_empty(self) -> None:
        assert encode_text("") == []

    def test_long_text_splits(self) -> None:
        messages = encode_text("a" * 1000)
        assert len(messages) == 4
        assert sum(len(m.payload) - 5 for m in messages) == 1000

    def test_split_never_breaks_a_character(self) -> None:
        # Splitting inside a multi-byte sequence would put mojibake on screen.
        text = "你" * 200  # 600 bytes
        messages = encode_text(text)
        assert len(messages) > 1
        rejoined = "".join(m.payload[5:].decode("utf-8") for m in messages)
        assert rejoined == text

    def test_chunks_respect_the_byte_cap(self) -> None:
        for msg in encode_text("啊" * 500):
            assert len(msg.payload) - 5 <= MAX_TEXT_BYTES


class TestScroll:
    def test_wire_size(self) -> None:
        assert len(encode_scroll(0, 0, 100, 100, vertical=1.0)) == 21

    def test_fixed_point_direction(self) -> None:
        # Layout offsets: type@0 x@1 y@5 w@9 h@11 hscroll@13 vscroll@15 buttons@17.
        def vscroll(value: float) -> int:
            msg = encode_scroll(0, 0, 9, 9, vertical=value)
            return struct.unpack_from(">h", msg.payload, 15)[0]

        assert vscroll(1.0) > 0 > vscroll(-1.0)
        assert vscroll(1.0) == 0x7FFF

    def test_horizontal_and_vertical_are_independent(self) -> None:
        msg = encode_scroll(0, 0, 9, 9, horizontal=-1.0, vertical=1.0)
        horizontal, vertical = struct.unpack_from(">hh", msg.payload, 13)
        assert horizontal < 0 < vertical


class TestSimple:
    def test_back_carries_an_action_byte(self) -> None:
        assert len(encode_simple(ControlType.BACK_OR_SCREEN_ON)) == 2

    def test_bare_types(self) -> None:
        assert len(encode_simple(ControlType.COLLAPSE_PANELS)) == 1

    def test_rejects_types_with_payloads(self) -> None:
        with pytest.raises(ValueError, match="carries a payload"):
            encode_simple(ControlType.INJECT_TEXT)


# ----------------------------------------------------------------------- video


def build_stream(
    *,
    device_name: str = "Pixel 7",
    codec: Codec = Codec.H264,
    size: tuple[int, int] = (1080, 2400),
    packets: list[tuple[bytes, bool, bool]] | None = None,
) -> bytes:
    """Assemble bytes exactly as the scrcpy server would."""
    out = bytearray()
    out += device_name.encode().ljust(DEVICE_NAME_LEN, b"\x00")
    out += struct.pack(">III", int(codec), *size)
    for data, is_config, is_key in packets or []:
        pts = 12345
        if is_config:
            pts |= PACKET_FLAG_CONFIG
        if is_key:
            pts |= PACKET_FLAG_KEY_FRAME
        out += struct.pack(">QI", pts, len(data)) + data
    return bytes(out)


class TestVideoParser:
    def test_reads_metadata(self) -> None:
        parser = VideoStreamParser()
        list(parser.feed(build_stream()))
        assert parser.device_name == "Pixel 7"
        assert parser.codec is Codec.H264
        assert parser.size == (1080, 2400)
        assert parser.ready

    def test_yields_packets(self) -> None:
        stream = build_stream(packets=[(b"\x00\x00\x00\x01aaa", False, False)])
        packets = list(VideoStreamParser().feed(stream))
        assert len(packets) == 1
        assert packets[0].data == b"\x00\x00\x00\x01aaa"
        assert packets[0].pts_us == 12345

    @pytest.mark.parametrize("chunk_size", [1, 2, 7, 13, 64, 65, 77, 1024])
    def test_survives_arbitrary_chunk_boundaries(self, chunk_size: int) -> None:
        """A header split across two TCP reads is what breaks naive parsers."""
        stream = build_stream(
            packets=[(b"A" * 100, True, False), (b"B" * 250, False, True),
                     (b"C" * 37, False, False)]
        )
        parser = VideoStreamParser()
        got = []
        for offset in range(0, len(stream), chunk_size):
            got.extend(parser.feed(stream[offset : offset + chunk_size]))
        assert [len(p.data) for p in got] == [100, 250, 37]
        assert parser.size == (1080, 2400)

    def test_flags_are_read_off_the_pts_field(self) -> None:
        stream = build_stream(
            packets=[(b"cfg", True, False), (b"idr", False, True), (b"p", False, False)]
        )
        config, keyframe, inter = list(VideoStreamParser().feed(stream))
        assert config.is_config and not config.is_keyframe
        assert keyframe.is_keyframe and not keyframe.is_config
        assert not inter.is_config and not inter.is_keyframe

    def test_config_packet_has_no_timestamp(self) -> None:
        # Parameter sets are not a moment in time; reporting the flag bits as a
        # PTS would produce absurd timestamps in the timeline.
        (config,) = list(VideoStreamParser().feed(build_stream(packets=[(b"c", True, False)])))
        assert config.pts_us is None

    def test_bootstrap_retains_config_and_last_keyframe(self) -> None:
        """A viewer joining mid-stream renders nothing without these."""
        stream = build_stream(
            packets=[
                (b"cfg", True, False),
                (b"idr1", False, True),
                (b"p1", False, False),
                (b"idr2", False, True),
                (b"p2", False, False),
            ]
        )
        parser = VideoStreamParser()
        list(parser.feed(stream))
        boot = parser.bootstrap_packets()
        assert [p.data for p in boot] == [b"cfg", b"idr2"], "newest keyframe, in decode order"

    def test_bootstrap_is_empty_before_any_keyframe(self) -> None:
        parser = VideoStreamParser()
        list(parser.feed(build_stream()))
        assert parser.bootstrap_packets() == []

    def test_without_device_meta(self) -> None:
        parser = VideoStreamParser(expect_device_meta=False)
        stream = build_stream()[DEVICE_NAME_LEN:]
        list(parser.feed(stream))
        assert parser.codec is Codec.H264
        assert parser.device_name is None

    def test_unknown_codec_explains_the_likely_cause(self) -> None:
        bad = b"x".ljust(DEVICE_NAME_LEN, b"\x00") + struct.pack(">III", 0xDEADBEEF, 1, 1)
        with pytest.raises(ValueError, match="server jar version"):
            list(VideoStreamParser().feed(bad))

    def test_implausible_length_is_rejected_not_allocated(self) -> None:
        # Without the guard a desynchronised read allocates gigabytes.
        bad = (
            b"x".ljust(DEVICE_NAME_LEN, b"\x00")
            + struct.pack(">III", int(Codec.H264), 1, 1)
            + struct.pack(">QI", 0, 0xFFFFFFF0)
        )
        with pytest.raises(ValueError, match="desynchronised"):
            list(VideoStreamParser().feed(bad))

    def test_partial_feed_yields_nothing_yet(self) -> None:
        parser = VideoStreamParser()
        assert list(parser.feed(build_stream()[:30])) == []
        assert not parser.ready

    def test_codec_ids_are_fourcc(self) -> None:
        assert int(Codec.H264).to_bytes(4, "big") == b"h264"
        assert int(Codec.H265).to_bytes(4, "big") == b"h265"
        assert int(Codec.AV1).to_bytes(4, "big") == b"av01"

    def test_webcodecs_identifiers(self) -> None:
        assert Codec.H264.webcodecs_id.startswith("avc1.")
        assert Codec.H265.webcodecs_id.startswith("hev1.")


async def test_live_subscriber_is_hashable_and_receives_packets() -> None:
    """Regression: the WebSocket used to fail before yielding its first frame."""
    session = ScrcpySession(None, "DEVICE")  # type: ignore[arg-type]
    packet = VideoPacket(data=b"frame", pts_us=1, is_config=False, is_keyframe=True)
    async with session.subscribe() as packets:
        assert session.state.subscribers == 1
        session._fan_out(packet)
        assert await anext(packets) is packet
    assert session.state.subscribers == 0


async def test_scrcpy_start_retries_an_empty_handshake(monkeypatch, tmp_path: Path) -> None:
    jar = tmp_path / "scrcpy-server.jar"
    jar.write_bytes(b"jar")
    calls: list[str] = []

    class Adb:
        async def push(self, *_args) -> None:
            calls.append("push")

        async def forward(self, _serial, _local, remote) -> str:
            calls.append("forward")
            assert remote.startswith("localabstract:scrcpy_")
            return "12345"

        async def forward_remove(self, *_args) -> None:
            calls.append("remove")

    session = ScrcpySession(Adb(), "DEVICE", config=ScrcpyConfig(jar_path=jar))  # type: ignore[arg-type]
    handshakes = 0

    async def launch(scid: int) -> None:
        calls.append("launch")
        assert 0 <= scid < 0x7FFF_FFFF

    async def connect() -> None:
        nonlocal handshakes
        handshakes += 1
        if handshakes == 1:
            raise asyncio.IncompleteReadError(b"", 1)

    async def relay() -> None:
        await asyncio.Event().wait()

    async def metadata() -> None:
        return None

    monkeypatch.setattr(session, "_launch", launch)
    monkeypatch.setattr(session, "_connect_sockets", connect)
    monkeypatch.setattr(session, "_relay_loop", relay)
    monkeypatch.setattr(session, "_await_metadata", metadata)

    await session.start()
    try:
        assert session.state.started
        assert handshakes == 2
        assert calls.count("remove") == 1
        assert calls[:3] == ["push", "forward", "launch"]
    finally:
        await session.stop()


def test_scrcpy_scid_is_passed_as_eight_digit_hex() -> None:
    args = ScrcpyConfig().server_args(scid=0x1234ABCD)
    assert args[0] == "scid=1234abcd"


async def test_forwarded_empty_handshake_is_retried(monkeypatch) -> None:
    class Adb:
        server_host = "127.0.0.1"

    class Writer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    calls = 0

    async def open_connection(_host, _port):
        nonlocal calls
        calls += 1
        reader = asyncio.StreamReader()
        if calls == 1:
            reader.feed_eof()
        elif calls == 2:
            reader.feed_data(b"\x00")
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    session = ScrcpySession(
        Adb(),  # type: ignore[arg-type]
        "DEVICE",
        config=ScrcpyConfig(connect_timeout_s=1),
    )
    session._forward_port = "12345"
    await session._connect_sockets()
    assert calls == 3  # empty video, valid video, then control


class TestAnnexB:
    def test_splits_on_four_byte_start_codes(self) -> None:
        data = b"\x00\x00\x00\x01" + b"AAA" + b"\x00\x00\x00\x01" + b"BB"
        assert split_annexb(data) == [b"AAA", b"BB"]

    def test_splits_on_three_byte_start_codes(self) -> None:
        data = b"\x00\x00\x01" + b"AAA" + b"\x00\x00\x01" + b"BB"
        assert split_annexb(data) == [b"AAA", b"BB"]

    def test_mixed_start_code_lengths(self) -> None:
        data = b"\x00\x00\x00\x01" + b"AAA" + b"\x00\x00\x01" + b"BB"
        assert split_annexb(data) == [b"AAA", b"BB"]

    def test_no_start_code(self) -> None:
        assert split_annexb(b"nothing here") == []

    def test_identifies_nal_types(self) -> None:
        sps = bytes([0x67]) + b"sps"      # 0x67 & 0x1f == 7
        pps = bytes([0x68]) + b"pps"      # == 8
        idr = bytes([0x65]) + b"idr"      # == 5
        data = b"".join(b"\x00\x00\x00\x01" + unit for unit in (sps, pps, idr))
        assert [t for t, _ in iter_nal_units(data)] == [
            NalType.SPS, NalType.PPS, NalType.IDR
        ]

    def test_keyframe_detection_fallback(self) -> None:
        idr = b"\x00\x00\x00\x01" + bytes([0x65]) + b"idr"
        inter = b"\x00\x00\x00\x01" + bytes([0x41]) + b"p"  # 0x41 & 0x1f == 1
        assert looks_like_keyframe(idr)
        assert not looks_like_keyframe(inter)

    def test_unknown_nal_type_is_returned_raw(self) -> None:
        data = b"\x00\x00\x00\x01" + bytes([0x1F]) + b"?"
        (nal_type, _), = list(iter_nal_units(data))
        assert nal_type == 31
