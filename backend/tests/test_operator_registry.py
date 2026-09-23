"""Registered operators must be reproducible and safe for notebook execution."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from pixelforge.geometry.mapper import Rect
from pixelforge.image_lab.operators import OPERATORS, availability, run_operator
from pixelforge.vision.tool_catalog import demo_image, encode_data_url

# face_detect needs an OpenCV build that still ships CascadeClassifier and its
# Haar cascade data; OpenCV 5.x dropped that legacy API from the Python bindings
# entirely, so on an unpinned opencv-python-headless install (this project's
# pyproject only requires >=4.9) it reports pending_adapter, not ready. Covered
# on its own below by a test that exercises whichever state the environment is
# actually in, instead of assuming "ready" like every other built-in operator.
OPTIONAL_ADAPTER_OPERATORS = {"face_detect"}


@pytest.mark.parametrize("tool_id", sorted(set(OPERATORS) - OPTIONAL_ADAPTER_OPERATORS))
def test_registered_operator_contract(
    tool_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No input mutation, hidden disk output, or nondeterministic pixel output."""
    spec = OPERATORS[tool_id]
    assert spec.acceptance_ref.endswith("::test_registered_operator_contract")
    assert Path(__file__).is_file()
    assert availability(spec)[0] == "ready"

    def reject_write(*args: object, **kwargs: object) -> None:
        raise AssertionError("operator tried to write a file")

    monkeypatch.setattr(cv2, "imwrite", reject_write)
    monkeypatch.chdir(tmp_path)
    image = demo_image()
    before = image.copy()
    roi = Rect(18, 20, 142, 65) if spec.needs_roi else None
    first = run_operator(tool_id, image, roi=roi)
    second = run_operator(tool_id, image, roi=roi)
    assert np.array_equal(image, before)
    assert set(first.images) == set(spec.outputs)
    for port, pixels in first.images.items():
        assert np.array_equal(pixels, second.images[port])
    assert list(tmp_path.iterdir()) == []


def test_known_operator_effects() -> None:
    image = demo_image()
    crop = run_operator("crop", image, roi=Rect(18, 20, 142, 65)).images["image"]
    assert np.array_equal(crop, image[20:85, 18:160])
    gray = run_operator("grayscale", image).images["image"]
    assert np.array_equal(gray[:, :, 0], gray[:, :, 1])
    assert np.array_equal(gray[:, :, 1], gray[:, :, 2])
    edges = run_operator("edges", image).images["image"]
    assert np.count_nonzero(edges) > 0
    threshold = run_operator("threshold", image).images["image"]
    assert set(np.unique(threshold)).issubset({0, 255})
    enhanced = run_operator("text_enhance", image).images["image"]
    assert np.count_nonzero(enhanced) > 0


def test_color_mask_exposes_real_mask_and_correct_color_space() -> None:
    image = np.zeros((12, 18, 3), np.uint8)
    image[:, :6] = (255, 0, 0)
    image[:, 6:12] = (250, 220, 20)
    image[:, 12:] = (0, 0, 255)

    yellow = run_operator("color_mask", image, params={"color": "yellow"})
    red = run_operator("color_mask", image, params={"color": "red"})

    assert np.all(yellow.images["mask"][:, 6:12] == 255)
    assert np.all(yellow.images["mask"][:, :6] == 0)
    assert np.all(yellow.images["mask"][:, 12:] == 0)
    assert np.all(red.images["mask"][:, :6] == 255)
    assert np.all(red.images["mask"][:, 12:] == 0)
    assert np.array_equal(yellow.images["image"][:, 6:12], image[:, 6:12])


def test_operator_validates_params_and_roi() -> None:
    image = demo_image()
    with pytest.raises(ValueError, match="unknown parameters"):
        run_operator("threshold", image, params={"unknown": 1})
    with pytest.raises(ValueError, match="low must be less"):
        run_operator("edges", image, params={"low": 200, "high": 20})
    with pytest.raises(ValueError, match="requires an ROI"):
        run_operator("crop", image)
    with pytest.raises(ValueError, match="inside"):
        run_operator("crop", image, roi=Rect(355, 190, 20, 20))


def test_template_match_finds_the_real_template_and_scores_it() -> None:
    image = demo_image()
    template = image[20:85, 18:160]  # the "START" button, verbatim
    result = run_operator(
        "template_match", image, params={"template": encode_data_url(template), "threshold": 0.9}
    )
    assert result.regions[0]["matched"] is True
    assert result.regions[0]["x"] == 18 and result.regions[0]["y"] == 20
    assert result.metrics["score"] >= 0.9
    assert not np.array_equal(result.images["image"], image)  # marker was drawn


def test_template_match_reports_a_miss_without_raising() -> None:
    image = demo_image()
    unrelated = np.zeros((30, 30, 3), np.uint8)
    cv2.rectangle(unrelated, (5, 5), (25, 25), (10, 200, 10), -1)
    cv2.circle(unrelated, (15, 15), 5, (200, 10, 10), -1)  # has structure, matches nowhere in demo_image
    result = run_operator(
        "template_match", image, params={"template": encode_data_url(unrelated), "threshold": 0.9}
    )
    assert result.regions[0]["matched"] is False
    assert result.metrics["score"] < 0.9


def test_template_match_rejects_a_bad_template_param() -> None:
    image = demo_image()
    with pytest.raises(ValueError, match="base64"):
        run_operator("template_match", image, params={"template": "not-base64!!"})


def test_match_verify_scores_similarity_against_its_own_roi() -> None:
    image = demo_image()
    roi = Rect(18, 20, 142, 65)
    template = image[20:85, 18:160]

    matching = run_operator(
        "match_verify", image, roi=roi,
        params={"template": encode_data_url(template), "mode": "edge", "similarity": 0.5},
    )
    assert matching.text == "相似"
    assert matching.metrics["similarity"] > 0.9

    unrelated = np.zeros((65, 142, 3), np.uint8)
    mismatched = run_operator(
        "match_verify", image, roi=roi,
        params={"template": encode_data_url(unrelated), "mode": "edge", "similarity": 0.5},
    )
    assert mismatched.text == "不相似"
    assert mismatched.metrics["similarity"] < matching.metrics["similarity"]


def test_match_verify_color_mode_uses_the_color_param() -> None:
    image = np.zeros((40, 40, 3), np.uint8)
    image[:, :20] = (250, 220, 20)  # yellow half
    image[:, 20:] = (0, 0, 255)  # blue half
    roi = Rect(0, 0, 20, 40)
    template = image[:, :20]

    yellow = run_operator(
        "match_verify", image, roi=roi,
        params={"template": encode_data_url(template), "mode": "color", "color": "yellow"},
    )
    blue = run_operator(
        "match_verify", image, roi=roi,
        params={"template": encode_data_url(template), "mode": "color", "color": "blue"},
    )
    assert yellow.metrics["similarity"] > blue.metrics["similarity"]


def test_ocr_finds_demo_text_and_reports_word_boxes() -> None:
    image = demo_image()
    result = run_operator("ocr", image, params={"language": "eng"})
    assert result.metrics["word_count"] >= 1
    assert any("PIXEL" in region["text"].upper() or "FORGE" in region["text"].upper() for region in result.regions)
    assert all("confidence" in region and "text" in region for region in result.regions)
    assert not np.array_equal(result.images["image"], image)  # boxes were drawn


def test_ocr_min_confidence_filters_out_everything() -> None:
    image = demo_image()
    result = run_operator("ocr", image, params={"language": "eng", "min_confidence": 100})
    assert result.metrics["word_count"] == 0
    assert result.regions == ()
    assert np.array_equal(result.images["image"], image)  # nothing drawn


def test_ocr_reports_similarity_against_target_words() -> None:
    image = demo_image()
    close = run_operator("ocr", image, params={"language": "eng", "target_words": "PIXEL FORGE"})
    far = run_operator("ocr", image, params={"language": "eng", "target_words": "zzzzz"})
    assert "text_similarity" in close.metrics
    assert close.metrics["text_similarity"] > far.metrics["text_similarity"]


def test_ocr_raises_a_clean_error_when_tesseract_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import pixelforge.image_lab.operators as operators_module

    class _Unavailable:
        available = False

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(operators_module, "TesseractOcr", _Unavailable)
    with pytest.raises(ValueError, match="Tesseract"):
        run_operator("ocr", demo_image())


def test_text_regions_finds_colored_word_blobs_and_ocrs_them() -> None:
    image = demo_image()
    # "PIXEL FORGE" is rendered in white on the demo image's lower banner.
    result = run_operator(
        "text_regions", image,
        params={"color": "white", "s_min": 0, "v_min": 150, "min_height": 10},
    )
    assert result.metrics["blob_count"] >= 1
    assert any("PIXEL" in region["text"].upper() or "FORGE" in region["text"].upper() for region in result.regions)
    assert all({"x", "y", "width", "height", "gravity_x", "gravity_y", "text"} <= region.keys() for region in result.regions)
    assert not np.array_equal(result.images["image"], image)  # boxes were drawn


def test_text_regions_reports_no_blobs_for_a_color_not_present() -> None:
    image = demo_image()
    result = run_operator("text_regions", image, params={"color": "green"})
    assert result.metrics["blob_count"] == 0
    assert result.regions == ()
    assert np.array_equal(result.images["image"], image)  # nothing drawn


def test_text_regions_reports_similarity_against_target_words() -> None:
    image = demo_image()
    result = run_operator(
        "text_regions", image,
        params={"color": "white", "s_min": 0, "v_min": 150, "min_height": 10, "target_words": "PIXEL FORGE"},
    )
    assert "best_text_similarity" in result.metrics
    assert result.metrics["best_text_similarity"] > 0.5


def test_highlight_state_reports_color_coverage_and_a_mask() -> None:
    image = demo_image()
    lit = run_operator(
        "highlight_state", image, roi=Rect(18, 20, 142, 65), params={"color": "yellow"}
    )
    assert lit.text == "点亮"
    assert lit.metrics["coverage"] > 0.5
    assert lit.images["mask"].shape == (65, 142)
    assert set(np.unique(lit.images["mask"])).issubset({0, 255})

    unlit = run_operator(
        "highlight_state", image, roi=Rect(188, 20, 152, 65), params={"color": "yellow"}
    )
    assert unlit.text == "未点亮"
    assert unlit.metrics["coverage"] < lit.metrics["coverage"]


def test_template_match_expand_finds_a_drifted_target_by_expanding_the_search() -> None:
    image = demo_image()
    template = image[20:85, 18:160]  # the "START" button, centered near (89, 52)
    params = {
        "template": encode_data_url(template),
        "anchor_x": 120, "anchor_y": 52,
        "initial_radius": 10, "radius_step": 40, "max_expansions": 3,
    }
    result = run_operator("template_match_expand", image, params=params)
    assert result.regions[0]["matched"] is True
    assert result.regions[0]["x"] == 18 and result.regions[0]["y"] == 20

    # Without any expansions allowed, the same narrow ROI cannot fit the
    # template at all, so the search must report a miss rather than raising.
    no_expand = dict(params, max_expansions=0)
    missed = run_operator("template_match_expand", image, params=no_expand)
    assert missed.metrics["score"] < result.metrics["score"]


def test_line_detect_reports_detected_segments() -> None:
    image = demo_image()
    result = run_operator("line_detect", image)
    assert result.metrics["line_count"] > 0
    assert all({"x1", "y1", "x2", "y2"} == region.keys() for region in result.regions)
    assert not np.array_equal(result.images["image"], image)  # lines were drawn


def test_line_detect_validates_threshold_order() -> None:
    with pytest.raises(ValueError, match="low must be less"):
        run_operator("line_detect", demo_image(), params={"low": 200, "high": 20})


def test_face_detect_contract_or_clean_pending_state() -> None:
    """OpenCV 5.x removed CascadeClassifier from its Python bindings, so on an
    unpinned install this operator legitimately downgrades to pending_adapter
    (a clean ValueError, not a startup crash) rather than "ready". If a build
    *does* still carry the legacy Haar cascade API, the operator must still
    hold to the same no-mutation/reproducibility contract as every other
    operator in test_registered_operator_contract."""
    spec = OPERATORS["face_detect"]
    state, reason = availability(spec)
    if state != "ready":
        assert state == "pending_adapter", reason
        with pytest.raises(ValueError):
            run_operator("face_detect", demo_image())
        return
    image = demo_image()
    before = image.copy()
    first = run_operator("face_detect", image)
    second = run_operator("face_detect", image)
    assert np.array_equal(image, before)
    for port, pixels in first.images.items():
        assert np.array_equal(pixels, second.images[port])
