"""Registered operators must be reproducible and safe for notebook execution."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from pixelforge.geometry.mapper import Rect
from pixelforge.image_lab.operators import OPERATORS, availability, run_operator
from pixelforge.vision.tool_catalog import demo_image


@pytest.mark.parametrize("tool_id", sorted(OPERATORS))
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
