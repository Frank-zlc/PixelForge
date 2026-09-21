"""OCR via Tesseract, with the parsing separated from the subprocess.

Adapted and generalised from AlbionHelper's ``recognition.py``. Two things
carried over because they were learned the hard way, and one thing dropped:

*Carried over.* Resolving the Tesseract binary explicitly, because a GUI-launched
process inherits a ``PATH`` without ``/opt/homebrew/bin`` or ``/usr/local/bin``
and OCR then fails only when the app is launched from the dock, never from a
terminal. And the word-level output shape -- text, confidence and bounding box
per word -- which is what lets OCR act as a *locator* rather than just a text
dump: knowing "总价" is at (412, 1180) is what makes it clickable.

*Dropped.* The page-classification rules. Deciding that a screen is a market buy
list from the words on it is business knowledge, and PixelForge carries none.
That logic stays in the project that owns it.

``parse_tsv`` is pure and therefore testable without the binary installed, which
matters because that is where the bugs are: Tesseract emits rows for page, block,
paragraph and line levels alongside the word rows, mixes ``-1`` confidences into
them, and represents "no text here" as an empty string rather than a missing row.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pixelforge.geometry.mapper import Point, Rect

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

__all__ = [
    "OcrError",
    "OcrResult",
    "RecognizedWord",
    "TesseractOcr",
    "find_tesseract",
    "parse_tsv",
]

# Tesseract's TSV has a `level` column; 5 is a word. The others are page, block,
# paragraph and line containers with no usable text.
_WORD_LEVEL = 5

_SEARCH_PATHS = (
    "/opt/homebrew/bin/tesseract",  # Apple silicon Homebrew
    "/usr/local/bin/tesseract",  # Intel Homebrew
    "/usr/bin/tesseract",
    "/snap/bin/tesseract",
)


class OcrError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecognizedWord:
    text: str
    confidence: float
    box: Rect

    @property
    def center(self) -> Point:
        return self.box.center


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    words: tuple[RecognizedWord, ...]
    mean_confidence: float | None
    language: str

    def find(
        self, needle: str, *, min_confidence: float = 60.0
    ) -> RecognizedWord | None:
        """First word containing ``needle``, ignoring case.

        Substring rather than equality because Tesseract routinely fuses a word
        with adjacent punctuation, and a confidence floor because low-confidence
        rows are frequently hallucinated glyphs from UI chrome.
        """
        lowered = needle.lower()
        for word in self.words:
            if word.confidence >= min_confidence and lowered in word.text.lower():
                return word
        return None

    def find_all(self, needle: str, *, min_confidence: float = 60.0) -> list[RecognizedWord]:
        lowered = needle.lower()
        return [
            word
            for word in self.words
            if word.confidence >= min_confidence and lowered in word.text.lower()
        ]


def find_tesseract() -> str | None:
    """Locate the binary, tolerating a GUI process's stunted PATH."""
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    if configured:
        return configured if Path(configured).is_file() else None
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in _SEARCH_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def parse_tsv(tsv: str, *, offset: tuple[int, int] = (0, 0)) -> list[RecognizedWord]:
    """Parse Tesseract TSV into words.

    ``offset`` shifts boxes back into full-image coordinates when OCR ran on a
    crop -- without it every match points into the crop and taps land in the
    top-left corner of the screen.

    Rows are skipped when they are container levels, carry a ``-1`` confidence,
    or hold only whitespace. Tesseract emits all three routinely.
    """
    lines = tsv.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    try:
        index = {
            name: header.index(name)
            for name in ("level", "left", "top", "width", "height", "conf", "text")
        }
    except ValueError as exc:
        raise OcrError(f"unexpected Tesseract TSV header: {header}") from exc

    offset_x, offset_y = offset
    words: list[RecognizedWord] = []
    for line in lines[1:]:
        columns = line.split("\t")
        if len(columns) <= index["text"]:
            continue
        try:
            if int(columns[index["level"]]) != _WORD_LEVEL:
                continue
            confidence = float(columns[index["conf"]])
            if confidence < 0:
                continue
            width = int(columns[index["width"]])
            height = int(columns[index["height"]])
            if width <= 0 or height <= 0:
                continue
            text = columns[index["text"]].strip()
            if not text:
                continue
            words.append(
                RecognizedWord(
                    text=text,
                    confidence=confidence,
                    box=Rect(
                        x=int(columns[index["left"]]) + offset_x,
                        y=int(columns[index["top"]]) + offset_y,
                        width=width,
                        height=height,
                    ),
                )
            )
        except ValueError:
            continue  # a malformed row must not discard the rest of the page
    return words


class TesseractOcr:
    """Runs Tesseract out of process.

    Out of process for two reasons: OCR is hundreds of milliseconds of pure CPU
    that would otherwise block the event loop and stall every device's video
    relay, and Tesseract occasionally segfaults on odd input -- as a subprocess
    that is a failed call rather than a dead server.
    """

    def __init__(
        self,
        *,
        command: str | None = None,
        language: str = "eng",
        tessdata_dir: Path | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._command = command or find_tesseract()
        self.language = language
        self._tessdata = tessdata_dir
        self._timeout = timeout_s

    @property
    def available(self) -> bool:
        return self._command is not None

    def require(self) -> str:
        if self._command is None:
            raise OcrError(
                "Tesseract not found. Install it (macOS: brew install tesseract "
                "tesseract-lang; Debian: apt install tesseract-ocr) or set "
                "TESSERACT_CMD to its path."
            )
        return self._command

    async def recognize(
        self,
        image: "np.ndarray",
        *,
        crop: Rect | None = None,
        language: str | None = None,
        psm: int = 11,
    ) -> OcrResult:
        """Recognise text, returning word boxes in *image* coordinates.

        ``psm=11`` ('sparse text') is the default because app screens are
        scattered labels and buttons rather than a page of prose; the default
        mode 3 assumes a document and merges unrelated controls into lines.
        """
        import cv2

        command = self.require()
        language = language or self.language

        offset = (0, 0)
        if crop is not None:
            box = crop.clamped_to(
                __import__("pixelforge.geometry.mapper", fromlist=["Size"]).Size(
                    image.shape[1], image.shape[0]
                )
            )
            image = image[box.y : box.bottom, box.x : box.right]
            offset = (box.x, box.y)

        ok, encoded = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        if not ok:
            raise OcrError("could not encode the image for OCR")

        argv = [command, "stdin", "stdout", "-l", language, "--psm", str(psm), "tsv"]
        env = dict(os.environ)
        if self._tessdata is not None:
            env["TESSDATA_PREFIX"] = str(self._tessdata)

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(encoded.tobytes()), timeout=self._timeout
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            process.kill()
            await process.wait()
            raise OcrError(f"Tesseract timed out after {self._timeout}s") from exc
        if process.returncode:
            raise OcrError(stderr.decode("utf-8", errors="replace").strip() or "OCR failed")

        words = tuple(parse_tsv(stdout.decode("utf-8", errors="replace"), offset=offset))
        mean = (
            sum(word.confidence for word in words) / len(words) if words else None
        )
        return OcrResult(
            text=" ".join(word.text for word in words),
            words=words,
            mean_confidence=mean,
            language=language,
        )
