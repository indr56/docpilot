# Spec: scanned pages (Phase 3.5)

Status: built in Phase 3.5.

## 1. Goal

A scanned PDF, or a digital PDF with some scanned pages, becomes searchable and citable by page,
instead of failing (`no_extractable_text`) or silently losing those pages.

## 2. Design

- **Per page, not per document.** A page with a text layer keeps it (exact, free). Only a page with no
  extractable text is rendered (300 dpi) and read by OCR. A mixed PDF keeps its digital pages as they are.
- **Strategy pattern.** `app/services/ocr.py` defines `OcrEngine` (`read(image) -> OcrResult(text,
  confidence)`). `OCR_ENGINE` picks one: `tesseract` (built in, runs locally in the image) or `none`.
  A cloud engine (AWS Textract, Google Document AI, Azure Document Intelligence, or a multimodal LLM)
  is one more class registered in `ENGINES`; the parser, chunker, citations and agent don't change.
- **Confidence is kept.** Each OCR'd page records the engine's mean word confidence (0-1). Pages below
  `OCR_MIN_CONFIDENCE` (0.6) are listed in a `low_ocr_confidence` warning on the document, so the user
  knows which pages to check. Pages OCR couldn't read at all stay in the `pages_without_text` warning.
  The chunk text carries no marker, but the document's processing details record which pages were OCR'd.
- **Bounded.** At most `OCR_MAX_PAGES` (50) pages per document are OCR'd, each with a time limit.
- **Its own queue.** OCR is slow (around a second a page). When a PDF is enqueued, a quick check (does
  any page lack a text layer?) sends it to the `ocr` queue instead of `documents`, so scans don't
  delay ordinary uploads. Today one worker listens to both; under load, run a second worker with
  `-Q ocr`. Same task, same locks and retries.
- **OCR once.** Results are cached per file content, page, engine and dpi, so the agent's
  full-text and scan tools, summaries and comparisons, which re-read the file, don't OCR it again.

## 3. Not built (documented options)

- **Image and chart captions** with a vision model: useful for charts and diagrams, off by design
  until the provider interface takes images; it would be a second step after OCR, not a replacement.
- **Layout and tables from scans:** Tesseract returns lines, not table structure; Textract or
  Document AI would, through the same interface.
- **Handwriting:** poor with Tesseract; a cloud engine is the answer.

## 4. Tests and evals

- Unit: a mixed PDF keeps page 1's text layer and OCRs only page 2; an image-only PDF is read; a
  blank vector-only PDF still fails with `no_extractable_text`; the engine can be swapped; the
  confidence warning appears; the queue choice.
- Eval (`tasks` set): questions answerable only from scanned pages, cited to the right page.
