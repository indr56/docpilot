"""OCR for PDF pages that have no text layer (specs/ocr.md). Strategy pattern: one interface,
engines chosen by OCR_ENGINE, so adding a cloud engine touches nothing else."""

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from PIL import Image

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class OcrResult:
    text: str
    confidence: float  # mean word confidence, 0-1 (0 when nothing was read)


class OcrEngine(Protocol):
    name: str

    def read(self, image: Image.Image) -> OcrResult: ...


class TesseractEngine:
    """Local Tesseract (tesseract-ocr in the image). Free, private, good on clean printed scans."""

    name = "tesseract"

    def __init__(self, languages: str, timeout_seconds: int):
        self.languages = languages
        self.timeout = timeout_seconds

    def read(self, image: Image.Image) -> OcrResult:
        import pytesseract

        data = pytesseract.image_to_data(
            image, lang=self.languages, output_type=pytesseract.Output.DICT, timeout=self.timeout
        )
        # Rebuild lines from the word boxes, so one call gives both the text and the confidences.
        lines: dict[tuple[int, int, int], list[str]] = {}
        confidences: list[float] = []
        for i, word in enumerate(data["text"]):
            conf = float(data["conf"][i])
            if not word.strip() or conf < 0:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append(word)
            confidences.append(conf / 100)
        text = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
        return OcrResult(text=text, confidence=round(sum(confidences) / len(confidences), 3) if confidences else 0.0)


ENGINES = {"tesseract": TesseractEngine}  # a cloud engine (Textract, Document AI, ...) registers here


@lru_cache
def get_ocr_engine() -> OcrEngine | None:
    settings = get_settings()
    if settings.ocr_engine == "none":
        return None
    engine = ENGINES.get(settings.ocr_engine)
    if engine is None:
        raise ValueError(f"Unknown OCR_ENGINE {settings.ocr_engine!r}; known: none, {', '.join(ENGINES)}")
    return engine(settings.ocr_languages, settings.ocr_page_timeout_seconds)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cache_key(engine: OcrEngine, digest: str, page: int, dpi: int) -> str:
    return f"ocr:{engine.name}:{dpi}:{digest}:{page}"


def pdf_needs_ocr(path: Path, max_pages: int) -> bool:
    """Quick check at enqueue time: does any of the first pages lack a text layer?"""
    import pdfplumber

    try:
        with pdfplumber.open(path) as pdf:
            return any(not page.chars for page in pdf.pages[:max_pages])
    except Exception:  # a broken file fails properly later, in the parse stage
        return False
