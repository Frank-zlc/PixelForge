"""Template matching, OCR parsing and the black-screen diagnosis."""

from __future__ import annotations

import shutil
import struct

import cv2
import numpy as np
import pytest

from pixelforge.device.capture import Capture, CaptureError, CaptureMode, decode_raw
from pixelforge.device.secure_probe import BlackScreenCause, diagnose_black_screen
from pixelforge.geometry.mapper import Rect, Size
from pixelforge.vision.matching import diff_ratio, match_template
from pixelforge.vision.ocr import OcrError, TesseractOcr, find_tesseract, parse_tsv


def textured_button(width: int = 100, height: int = 60) -> np.ndarray:
    """A button with real internal structure, like any actual UI control."""
    button = np.full((height, width, 3), 200, np.uint8)
    button[4 : height - 4, 4 : width - 4] = 60
    button[height // 3 : 2 * height // 3, width // 5 : 4 * width // 5] = 235
    return button


@pytest.fixture
def screen() -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.integers(0, 255, (600, 400, 3), dtype=np.uint8)


class TestTemplateMatching:
    def test_exact_match_reports_the_right_position(self, screen: np.ndarray) -> None:
        button = textured_button()
        screen[300:360, 150:250] = button
        result = match_template(screen, button)
        assert result.found
        assert (result.box.x, result.box.y) == (150, 300)
        assert result.score > 0.99

    def test_roi_result_is_in_full_image_coordinates(self, screen: np.ndarray) -> None:
        # Forgetting to add the ROI offset back is a classic bug: every tap then
        # lands near the top-left corner.
        button = textured_button()
        screen[300:360, 150:250] = button
        result = match_template(screen, button, roi=Rect(100, 250, 250, 200))
        assert (result.box.x, result.box.y) == (150, 300)

    def test_multi_scale_finds_a_resized_template(self, screen: np.ndarray) -> None:
        button = textured_button()
        screen[300:360, 150:250] = button
        enlarged = cv2.resize(button, (120, 72))
        result = match_template(screen, enlarged)
        assert result.found
        assert result.scale < 1.0, "should match by shrinking the oversized template"

    def test_single_scale_can_be_forced(self, screen: np.ndarray) -> None:
        button = textured_button()
        screen[300:360, 150:250] = button
        enlarged = cv2.resize(button, (130, 78))
        assert not match_template(screen, enlarged, scales=(1.0,)).found

    def test_absent_template_is_not_found(self, screen: np.ndarray) -> None:
        rng = np.random.default_rng(99)
        needle = rng.integers(0, 255, (40, 40, 3), dtype=np.uint8)
        assert not match_template(screen, needle).found

    def test_uniform_template_is_refused_with_an_explanation(self, screen: np.ndarray) -> None:
        """A flat crop matches every region of that colour equally.

        TM_CCOEFF_NORMED subtracts the mean, so on a uniform template the
        correlation is undefined and OpenCV returns a confident-looking 1.0 at
        position (0,0) -- a false positive that is hard to trace back. Refusing it
        at the point of use is the honest behaviour.
        """
        with pytest.raises(ValueError, match="nearly uniform"):
            match_template(screen, np.full((30, 30, 3), 128, np.uint8))

    def test_mask_excludes_changing_pixels(self, screen: np.ndarray) -> None:
        # The real case: a button whose badge changes between runs.
        button = textured_button()
        screen[300:360, 150:250] = button
        template = button.copy()
        template[10:25, 70:95] = 0  # where the badge will be
        mask = np.full(template.shape[:2], 255, np.uint8)
        mask[10:25, 70:95] = 0
        result = match_template(screen, template, mask=mask, threshold=0.9)
        assert result.found
        assert result.metric == "TM_CCORR_NORMED", "masks need a correlation metric"

    def test_explain_reports_the_near_miss(self, screen: np.ndarray) -> None:
        # 'best 0.62' vs 'best 0.89' point at different fixes, so the number has
        # to survive into the message.
        rng = np.random.default_rng(5)
        result = match_template(screen, rng.integers(0, 255, (40, 40, 3), dtype=np.uint8))
        assert "no match" in result.explain()
        assert f"{result.score:.3f}" in result.explain()

    def test_empty_input_is_rejected(self, screen: np.ndarray) -> None:
        with pytest.raises(ValueError, match="empty image"):
            match_template(screen, np.zeros((0, 0, 3), np.uint8))


class TestStability:
    def test_identical_frames_have_no_difference(self, screen: np.ndarray) -> None:
        assert diff_ratio(screen, screen) == 0.0

    def test_motion_is_detected(self, screen: np.ndarray) -> None:
        assert diff_ratio(screen, np.roll(screen, 40, axis=0)) > 0.5

    def test_encoder_noise_is_ignored(self, screen: np.ndarray) -> None:
        # A +/-3 wobble is compression noise, not a moving UI.
        noisy = np.clip(screen.astype(np.int16) + 3, 0, 255).astype(np.uint8)
        assert diff_ratio(screen, noisy) < 0.01

    def test_mismatched_shapes_count_as_fully_changed(self, screen: np.ndarray) -> None:
        assert diff_ratio(screen, screen[:100]) == 1.0


class TestOcrParsing:
    HEADER = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext"
    )

    def test_keeps_only_word_rows(self) -> None:
        tsv = "\n".join([
            self.HEADER,
            "1\t1\t0\t0\t0\t0\t0\t0\t400\t600\t-1\t",          # page container
            "4\t1\t1\t1\t1\t0\t10\t20\t100\t18\t-1\t",          # line container
            "5\t1\t1\t1\t1\t1\t10\t20\t60\t18\t93.5\tTotal",
        ])
        words = parse_tsv(tsv)
        assert [word.text for word in words] == ["Total"]

    def test_drops_negative_confidence_and_blank_text(self) -> None:
        tsv = "\n".join([
            self.HEADER,
            "5\t1\t1\t1\t1\t1\t10\t20\t60\t18\t-1\tghost",
            "5\t1\t1\t1\t1\t2\t80\t20\t40\t18\t88.0\t   ",
            "5\t1\t1\t1\t1\t3\t10\t50\t70\t18\t75.2\tConfirm",
        ])
        assert [word.text for word in parse_tsv(tsv)] == ["Confirm"]

    def test_crop_offset_is_added_back(self) -> None:
        # Without this every match points into the crop, and taps land in the
        # screen's top-left corner.
        tsv = "\n".join([self.HEADER, "5\t1\t1\t1\t1\t1\t10\t20\t60\t18\t93.5\tTotal"])
        (word,) = parse_tsv(tsv, offset=(100, 200))
        assert (word.box.x, word.box.y) == (110, 220)

    def test_malformed_row_does_not_discard_the_page(self) -> None:
        tsv = "\n".join([
            self.HEADER,
            "5\t1\t1\t1\t1\t1\tNOT_A_NUMBER\t20\t60\t18\t93.5\tbad",
            "5\t1\t1\t1\t1\t2\t10\t50\t70\t18\t75.2\tgood",
        ])
        assert [word.text for word in parse_tsv(tsv)] == ["good"]

    def test_unexpected_header_is_an_error(self) -> None:
        with pytest.raises(OcrError, match="TSV header"):
            parse_tsv("wrong\theader\n")

    def test_empty_input(self) -> None:
        assert parse_tsv("") == []


@pytest.mark.skipif(find_tesseract() is None, reason="Tesseract not installed")
class TestOcrEndToEnd:
    def _image(self) -> np.ndarray:
        image = np.full((200, 400, 3), 255, np.uint8)
        cv2.putText(image, "Confirm", (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2)
        cv2.putText(image, "Total 128", (30, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2)
        return image

    async def test_recognises_and_locates_words(self) -> None:
        result = await TesseractOcr().recognize(self._image())
        assert "Confirm" in result.text
        located = result.find("Confirm")
        assert located is not None
        # Location is the point: OCR acts as a locator, not a text dump.
        assert 20 < located.center.x < 200
        assert 20 < located.center.y < 90

    async def test_crop_offsets_survive_a_real_run(self) -> None:
        result = await TesseractOcr().recognize(self._image(), crop=Rect(0, 100, 400, 100))
        assert result.words
        assert all(word.box.y >= 100 for word in result.words)

    async def test_confidence_floor_filters_matches(self) -> None:
        result = await TesseractOcr().recognize(self._image())
        assert result.find("Confirm", min_confidence=99.9) is None


class TestRawCapture:
    def _payload(self, header_fields: int) -> bytes:
        pixels = np.arange(4 * 3 * 4, dtype=np.uint8).reshape(3, 4, 4)
        header = struct.pack("<III", 4, 3, 1)
        if header_fields == 4:
            header += struct.pack("<I", 0)
        return header + pixels.tobytes()

    @pytest.mark.parametrize("fields", [3, 4])
    def test_both_header_layouts(self, fields: int) -> None:
        # 3 or 4 uint32 depending on Android version; probing which one accounts
        # for the payload beats reading the version and branching on it.
        width, height, pixels = decode_raw(self._payload(fields))
        assert (width, height) == (4, 3)
        assert pixels.shape == (3, 4, 4)

    def test_garbage_is_rejected_clearly(self) -> None:
        with pytest.raises(CaptureError, match="no known header layout"):
            decode_raw(b"error: device offline\n")

    def test_alpha_is_dropped(self) -> None:
        capture = Capture(
            data=self._payload(3), size=Size(4, 3), mode=CaptureMode.RAW,
            captured_at=0.0, serial="X",
        )
        assert capture.to_array().shape == (3, 4, 3)

    def test_crop_is_clamped(self) -> None:
        capture = Capture(
            data=self._payload(3), size=Size(4, 3), mode=CaptureMode.RAW,
            captured_at=0.0, serial="X",
        )
        assert capture.crop(Rect(2, 2, 100, 100)).shape[:2] == (1, 2)


class TestSecureProbe:
    def test_black_with_a_tree_is_flag_secure(self) -> None:
        """Black alone is ambiguous; black plus a populated tree is not.

        Something is definitely being composited, and we are definitely not
        allowed to see it.
        """
        result = diagnose_black_screen(np.zeros((200, 100, 3), np.uint8), node_count=12)
        assert result.cause is BlackScreenCause.FLAG_SECURE
        assert not result.capture_usable
        assert "FLAG_SECURE" in result.message()
        assert "a11y selectors" in result.message()

    def test_black_with_no_tree_is_a_dark_display(self) -> None:
        result = diagnose_black_screen(
            np.zeros((200, 100, 3), np.uint8), node_count=0, screen_on=False
        )
        assert result.cause is BlackScreenCause.DISPLAY_OFF

    def test_normal_capture_is_not_black(self) -> None:
        rng = np.random.default_rng(1)
        result = diagnose_black_screen(
            rng.integers(0, 255, (200, 100, 3), dtype=np.uint8), node_count=5
        )
        assert result.cause is BlackScreenCause.NOT_BLACK
        assert result.capture_usable
        assert result.message() == ""

    def test_a_genuinely_dark_but_textured_screen_is_not_black(self) -> None:
        image = np.full((200, 100, 3), 2, np.uint8)
        image[::10] = 90  # faint content, e.g. a dark-mode page
        assert diagnose_black_screen(image, node_count=8).cause is BlackScreenCause.NOT_BLACK
