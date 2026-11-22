"""Scanned pages (specs/ocr.md): per-page OCR, the engine strategy, confidence warnings, the OCR
queue and the OCR cache. Uses the real Tesseract on the generated sample scans."""

import uuid
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.services import ocr
from app.services.parsing import ParseError, parse_document
from app.workers import tasks
from tests.conftest import SAMPLES, upload

MIXED = SAMPLES / "leeds_site_inspection_mixed.pdf"
SCANNED = SAMPLES / "kestrel_delivery_note_scanned.pdf"
VECTOR_ONLY = SAMPLES / "edge-cases" / "scanned_no_text.pdf"


class CountingEngine:
    name = "counting"

    def __init__(self, text: str = "Scanned words read by a stub engine, page text.", confidence: float = 0.9):
        self.text, self.confidence, self.calls = text, confidence, 0

    def read(self, image: Image.Image) -> ocr.OcrResult:
        self.calls += 1
        return ocr.OcrResult(self.text, self.confidence)


def use_engine(monkeypatch: pytest.MonkeyPatch, engine: object | None) -> None:
    monkeypatch.setattr(ocr, "get_ocr_engine", lambda: engine)


def test_mixed_pdf_keeps_the_text_layer_and_ocrs_only_the_scanned_page() -> None:
    parsed = parse_document(MIXED, ".pdf")
    page1, page2 = parsed.pages
    assert page1.number == 1 and page1.ocr_confidence is None and "Requires improvement" in page1.text
    assert page2.number == 2 and "7.8" in page2.text and "Varga" in page2.text
    assert page2.ocr_confidence is not None and page2.ocr_confidence > 0.6
    assert parsed.pages_without_text == [] and set(parsed.ocr_pages) == {2}


def test_an_image_only_pdf_is_read() -> None:
    text = parse_document(SCANNED, ".pdf").pages[0].text
    assert "DN-58213" in text and "Kestrel Packaging" in text and "9,870" in text


def test_drawings_only_still_fail_clearly() -> None:
    with pytest.raises(ParseError) as caught:
        parse_document(VECTOR_ONLY, ".pdf")
    assert caught.value.code == "no_extractable_text"


def test_the_engine_is_swappable_and_none_turns_ocr_off(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = CountingEngine()
    use_engine(monkeypatch, stub)
    assert parse_document(SCANNED, ".pdf").pages[0].text.startswith("Scanned words")
    assert stub.calls == 1
    use_engine(monkeypatch, None)
    with pytest.raises(ParseError):
        parse_document(SCANNED, ".pdf")


def test_ocr_results_are_cached_per_file_and_page(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = CountingEngine()
    use_engine(monkeypatch, stub)
    parse_document(SCANNED, ".pdf")
    parse_document(SCANNED, ".pdf")  # e.g. the agent's get_document_text re-reading the file
    assert stub.calls == 1


async def test_low_confidence_pages_are_flagged_on_the_document(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_engine(monkeypatch, CountingEngine(confidence=0.41))
    data = SCANNED.read_bytes() + b"\n% " + uuid.uuid4().hex.encode()  # a new file, so not a duplicate
    response = await upload(client, "faint_scan.pdf", data)
    doc = (await client.get(f"/documents/{response.json()['document']['id']}")).json()
    assert doc["status"] == "ready"
    codes = {w["code"]: w["message"] for w in doc["warnings"]}
    assert "low_ocr_confidence" in codes and "Page 1" in codes["low_ocr_confidence"]


async def test_scanned_document_is_searchable_and_cited_by_page(client: httpx.AsyncClient) -> None:
    response = await upload(client, MIXED.name, MIXED.read_bytes())
    doc_id = response.json()["document"]["id"]
    body = (
        await client.post("/agent/runs", json={"goal": "What was the cold room temperature?", "document_ids": [doc_id]})
    ).json()
    assert body["status"] == "completed"
    assert any(c["page"] == 2 for c in body["citations"]), body["citations"]


async def test_scanned_pdfs_go_to_the_ocr_queue(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    scanned = (await upload(client, "routing_scan.pdf", SCANNED.read_bytes() + b"\n% r1")).json()["document"]["id"]
    digital = (await upload(client, "routing_text.md", b"# Notes\n\nA plain digital document with text.")).json()
    assert tasks._needs_ocr(scanned) is True
    assert tasks._needs_ocr(digital["document"]["id"]) is False
    use_engine(monkeypatch, None)  # OCR off: nothing is routed to the OCR queue
    assert tasks._needs_ocr(scanned) is False

    sent: list[dict[str, object]] = []
    monkeypatch.setattr(tasks.process_document, "apply_async", lambda args, **kw: sent.append(kw))
    use_engine(monkeypatch, CountingEngine())
    tasks.enqueue_document(scanned)
    assert sent == [{"queue": "ocr"}]
