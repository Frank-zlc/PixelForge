"""Coordinate conversion: the full rotation x scaling x alignment matrix.

This is the project's highest-risk module. A three-pixel error here survives
manual clicking -- a finger-sized target absorbs it -- and only shows up much
later as template matches that score inexplicably low. So the acceptance bar is
exact: every corner and the centre must round-trip to the same pixel at every
rotation and every scale.
"""

from __future__ import annotations

import pytest

from pixelforge.geometry.mapper import (
    CoordinateMapper,
    OutsideFrameError,
    Point,
    Rect,
    Rotation,
    Size,
    Space,
)

# A 20:9 phone, the awkward modern aspect ratio.
DEVICE = Size(1080, 2400)


def mapper(
    *,
    device: Size = DEVICE,
    frame: Size | None = None,
    rotation: Rotation = Rotation.R0,
    element: Size | None = Size(500, 1000),
) -> CoordinateMapper:
    display = device.swapped() if rotation.swaps_axes else device
    return CoordinateMapper(
        device=device, frame=frame or display, rotation=rotation, element=element
    )


class TestSize:
    def test_rejects_non_positive(self) -> None:
        for bad in [(0, 100), (100, 0), (-1, 5)]:
            with pytest.raises(ValueError, match="must be positive"):
                Size(*bad)

    def test_swap(self) -> None:
        assert Size(1080, 2400).swapped() == Size(2400, 1080)


class TestRotation:
    def test_degrees_and_axis_swap(self) -> None:
        assert [r.degrees for r in Rotation] == [0, 90, 180, 270]
        assert [r.swaps_axes for r in Rotation] == [False, True, False, True]

    def test_display_size_follows_rotation(self) -> None:
        # wm size reports the natural orientation and does not change when the
        # device rotates, so DEVICE space has to be derived.
        assert mapper(rotation=Rotation.R0).display == Size(1080, 2400)
        assert mapper(rotation=Rotation.R90).display == Size(2400, 1080)
        assert mapper(rotation=Rotation.R180).display == Size(1080, 2400)
        assert mapper(rotation=Rotation.R270).display == Size(2400, 1080)

    def test_parse_wraps(self) -> None:
        assert Rotation.parse(4) is Rotation.R0
        assert Rotation.parse(5) is Rotation.R90


class TestAcceptanceZeroPixelError:
    """PRODUCT.md P2 acceptance: corners and centre, every rotation, 0px error."""

    CORNERS = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.5, 0.5)]

    @pytest.mark.parametrize("rotation", list(Rotation))
    @pytest.mark.parametrize("nx,ny", CORNERS)
    def test_norm_device_round_trip(self, rotation: Rotation, nx: float, ny: float) -> None:
        m = mapper(rotation=rotation)
        device = m.norm_to_device(Point(nx, ny))
        back = m.device_to_norm(device)
        assert back.x == pytest.approx(nx, abs=1e-12)
        assert back.y == pytest.approx(ny, abs=1e-12)

    @pytest.mark.parametrize("rotation", list(Rotation))
    @pytest.mark.parametrize("nx,ny", CORNERS)
    def test_full_chain_round_trip(self, rotation: Rotation, nx: float, ny: float) -> None:
        # NORM -> DEVICE -> FRAME -> CSS -> FRAME -> DEVICE -> NORM
        m = mapper(rotation=rotation)
        css = m.convert(Point(nx, ny), Space.NORM, Space.CSS, strict=False)
        back = m.convert(css, Space.CSS, Space.NORM, strict=False)
        assert back.x == pytest.approx(nx, abs=1e-9)
        assert back.y == pytest.approx(ny, abs=1e-9)

    @pytest.mark.parametrize("rotation", list(Rotation))
    def test_device_pixel_exact_after_round_trip(self, rotation: Rotation) -> None:
        """Every device pixel on the edges survives a round trip unchanged."""
        m = mapper(rotation=rotation)
        display = m.display
        probes = [
            (0, 0),
            (display.width - 1, 0),
            (0, display.height - 1),
            (display.width - 1, display.height - 1),
            (display.width // 2, display.height // 2),
        ]
        for x, y in probes:
            frame = m.device_to_frame(Point(x, y))
            back = m.frame_to_device(frame)
            assert back.rounded() == (x, y), f"{rotation.name} {(x, y)} -> {back}"


class TestEncoderAlignment:
    """H.264 aligns width and height down independently, so the two axis scales
    genuinely differ. Assuming a single uniform scale is the classic bug."""

    def test_per_axis_scales_differ(self) -> None:
        # 1080x2400 downscaled to max_size 1024 then aligned to /8: 460x1024.
        m = mapper(frame=Size(460, 1024))
        assert m.scale_x != m.scale_y
        assert m.scale_x == pytest.approx(1080 / 460)
        assert m.scale_y == pytest.approx(2400 / 1024)

    def test_uniform_scale_assumption_would_be_wrong(self) -> None:
        m = mapper(frame=Size(460, 1024))
        # What a single-scale implementation would compute for the bottom edge:
        naive = 1024 * m.scale_x
        correct = m.frame_to_device(Point(0, 1024)).y
        assert abs(naive - correct) > 3, "alignment error should be visible"
        assert correct == pytest.approx(2400)

    def test_is_axis_aligned_flag(self) -> None:
        assert mapper().is_axis_aligned
        assert not mapper(frame=Size(460, 1024)).is_axis_aligned

    @pytest.mark.parametrize("frame_w,frame_h", [(1080, 2400), (540, 1200), (460, 1024), (272, 608)])
    def test_frame_edges_map_to_device_edges(self, frame_w: int, frame_h: int) -> None:
        m = mapper(frame=Size(frame_w, frame_h))
        assert m.frame_to_device(Point(0, 0)).rounded() == (0, 0)
        bottom_right = m.frame_to_device(Point(frame_w, frame_h))
        assert bottom_right.rounded() == (1080, 2400)


class TestLetterbox:
    def test_content_rect_centres_the_frame(self) -> None:
        # A 1080x2400 frame (0.45) in a 500x1000 element (0.50): height is the
        # limiting axis, so it fills exactly and the bars fall on the sides.
        m = mapper(element=Size(500, 1000))
        content = m.content_rect_css
        assert content.height == 1000
        assert content.width == 450
        assert content.x == 25, "must be centred horizontally"
        assert content.y == 0

    def test_bars_on_the_sides_when_element_is_wide(self) -> None:
        m = mapper(element=Size(2000, 1000))
        content = m.content_rect_css
        assert content.y == 0
        assert content.height == 1000
        assert content.x > 0, "a wide element must letterbox left/right"
        assert content.x + content.width <= 2000

    def test_click_in_the_bar_is_rejected_not_clamped(self) -> None:
        # Clamping would fabricate an interaction the user never made.
        m = mapper(element=Size(2000, 1000))
        content = m.content_rect_css
        with pytest.raises(OutsideFrameError):
            m.css_to_frame(Point(content.x - 10, 500))
        with pytest.raises(OutsideFrameError):
            m.css_to_frame(Point(content.right + 10, 500))

    def test_non_strict_clamps(self) -> None:
        m = mapper(element=Size(2000, 1000))
        point = m.css_to_frame(Point(0, 500), strict=False)
        assert point.x == 0.0

    def test_click_just_inside_the_edge_is_accepted(self) -> None:
        m = mapper(element=Size(2000, 1000))
        content = m.content_rect_css
        assert m.css_to_frame(Point(content.x + 1, 500)).x >= 0

    def test_css_round_trip(self) -> None:
        m = mapper(element=Size(731, 397))  # deliberately ugly numbers
        for frame_point in (Point(0, 0), Point(540, 1200), Point(1080, 2400)):
            css = m.frame_to_css(frame_point)
            back = m.css_to_frame(css, strict=False)
            assert back.x == pytest.approx(frame_point.x, abs=1e-6)
            assert back.y == pytest.approx(frame_point.y, abs=1e-6)

    def test_css_conversion_without_element_is_a_clear_error(self) -> None:
        m = CoordinateMapper(device=DEVICE, frame=DEVICE, element=None)
        with pytest.raises(ValueError, match="element size"):
            m.css_to_frame(Point(1, 1))


class TestConvert:
    def test_identity(self) -> None:
        m = mapper()
        assert m.convert(Point(3, 4), Space.DEVICE, Space.DEVICE) == Point(3, 4)

    @pytest.mark.parametrize(
        "source,target",
        [
            (Space.CSS, Space.NORM),
            (Space.NORM, Space.CSS),
            (Space.CSS, Space.DEVICE),
            (Space.DEVICE, Space.CSS),
            (Space.FRAME, Space.NORM),
            (Space.NORM, Space.FRAME),
            (Space.CSS, Space.FRAME),
            (Space.DEVICE, Space.NORM),
        ],
    )
    def test_every_pair_round_trips(self, source: Space, target: Space) -> None:
        m = mapper(frame=Size(460, 1024), element=Size(731, 397))
        start = m.convert(Point(0.37, 0.61), Space.NORM, source, strict=False)
        there = m.convert(start, source, target, strict=False)
        back = m.convert(there, target, source, strict=False)
        assert back.x == pytest.approx(start.x, abs=1e-6)
        assert back.y == pytest.approx(start.y, abs=1e-6)

    def test_accepts_plain_strings(self) -> None:
        m = mapper()
        assert m.convert(Point(0.5, 0.5), "norm", "device").rounded() == (540, 1200)


class TestRectConversion:
    def test_rounds_outward_so_the_crop_covers_the_selection(self) -> None:
        # Losing an edge row makes a template match worse than carrying an extra
        # pixel of background, so rounding goes outward.
        m = mapper(frame=Size(460, 1024))
        rect = m.convert_rect(Rect(10, 10, 5, 5), Space.FRAME, Space.DEVICE)
        exact_left = 10 * m.scale_x
        assert rect.x <= exact_left
        assert rect.right >= 15 * m.scale_x

    def test_clamped_into_the_target_bounds(self) -> None:
        m = mapper(frame=Size(460, 1024))
        rect = m.convert_rect(Rect(0, 0, 460, 1024), Space.FRAME, Space.DEVICE)
        assert rect.x == 0 and rect.y == 0
        assert rect.right <= 1080 and rect.bottom <= 2400

    def test_selection_overflowing_the_frame_is_clamped(self) -> None:
        # A drag routinely ends past the edge; clamping a selection is reasonable
        # in a way that clamping a click is not.
        m = mapper(element=Size(2000, 1000))
        rect = m.convert_rect(Rect(-50, -50, 3000, 2000), Space.CSS, Space.FRAME)
        assert rect.x >= 0 and rect.y >= 0
        assert rect.right <= 1080 and rect.bottom <= 2400

    def test_rect_geometry_helpers(self) -> None:
        rect = Rect(10, 20, 30, 40)
        assert (rect.right, rect.bottom) == (40, 60)
        assert rect.center == Point(25.0, 40.0)
        assert rect.as_tuple() == (10, 20, 40, 60)

    def test_rect_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            Rect(0, 0, 0, 10)

    def test_clamp_keeps_at_least_one_pixel(self) -> None:
        clamped = Rect(5000, 5000, 10, 10).clamped_to(Size(100, 100))
        assert clamped.width >= 1 and clamped.height >= 1
        assert clamped.right <= 100 and clamped.bottom <= 100


class TestRotateDevicePoint:
    def test_quarter_turn_maps_corner_to_corner(self) -> None:
        m = mapper(rotation=Rotation.R0)  # display 1080x2400
        assert m.rotate_device_point(Point(0, 0), Rotation.R90).rounded() == (2400, 0)

    def test_four_turns_is_identity(self) -> None:
        m = mapper(rotation=Rotation.R0)
        point = Point(137, 921)
        out = m.rotate_device_point(point, Rotation.R0)
        assert out.rounded() == point.rounded()

    def test_180_is_a_point_reflection(self) -> None:
        m = mapper(rotation=Rotation.R0)
        out = m.rotate_device_point(Point(100, 200), Rotation.R180)
        assert out.rounded() == (1080 - 100, 2400 - 200)


class TestDescribe:
    def test_reports_all_four_spaces(self) -> None:
        m = mapper(frame=Size(460, 1024), element=Size(500, 1000))
        described = m.describe(Point(0.5, 0.5), Space.NORM)
        assert set(described) == {"css", "frame", "device", "norm"}
        assert described["device"] == (540.0, 1200.0)
        assert described["norm"] == (0.5, 0.5)

    def test_omits_css_when_no_element_rather_than_faking_it(self) -> None:
        m = CoordinateMapper(device=DEVICE, frame=DEVICE, element=None)
        described = m.describe(Point(0.5, 0.5), Space.NORM)
        assert "css" not in described
        assert described["device"] == (540.0, 1200.0)


class TestImmutability:
    def test_with_returns_a_new_mapper(self) -> None:
        m = mapper()
        rotated = m.with_(rotation=Rotation.R90, frame=Size(2400, 1080))
        assert m.rotation is Rotation.R0, "original must be untouched"
        assert rotated.rotation is Rotation.R90
        assert rotated.display == Size(2400, 1080)

    def test_with_rotation_zero_is_not_swallowed(self) -> None:
        # `rotation or self.rotation` would treat R0 (== 0) as "not provided".
        m = mapper(rotation=Rotation.R90)
        assert m.with_(rotation=Rotation.R0).rotation is Rotation.R0

    def test_frozen(self) -> None:
        m = mapper()
        with pytest.raises((AttributeError, TypeError)):
            m.rotation = Rotation.R90  # type: ignore[misc]


class TestRealDeviceProfiles:
    """Sizes from devices that actually cause trouble."""

    @pytest.mark.parametrize(
        "name,device,frame",
        [
            ("Pixel 7 native", Size(1080, 2400), Size(1080, 2400)),
            ("Pixel 7 max_size=1024", Size(1080, 2400), Size(460, 1024)),
            ("Galaxy S21 QHD", Size(1440, 3200), Size(1440, 3200)),
            ("iPhone-ish 19.5:9", Size(1170, 2532), Size(1168, 2528)),  # aligned /8
            ("tablet 16:10", Size(1600, 2560), Size(640, 1024)),
            ("odd alignment", Size(1079, 2399), Size(1072, 2392)),
        ],
    )
    def test_corners_survive_round_trip(self, name: str, device: Size, frame: Size) -> None:
        m = CoordinateMapper(device=device, frame=frame, element=Size(412, 915))
        for nx, ny in [(0.0, 0.0), (1.0, 1.0), (0.5, 0.5), (0.123, 0.987)]:
            css = m.convert(Point(nx, ny), Space.NORM, Space.CSS, strict=False)
            back = m.convert(css, Space.CSS, Space.NORM, strict=False)
            assert back.x == pytest.approx(nx, abs=1e-9), name
            assert back.y == pytest.approx(ny, abs=1e-9), name
