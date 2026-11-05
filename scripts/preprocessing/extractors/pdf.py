from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from scripts.preprocessing.extractors.common import (
    DEFAULT_OCR_DPI,
    DEFAULT_OCR_LANGUAGE,
    DEFAULT_PDF_OCR_MODE,
    MIN_USEFUL_PDF_TEXT_CHARS,
    is_likely_broken_korean_text,
    normalize_text,
)

log = logging.getLogger(__name__)

def extract_pdf_text_blocks(path: Path) -> list[dict]:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"PDF parser import failed (PyMuPDF/fitz required): {exc}") from exc

    def is_likely_page_number(line: str) -> bool:
        line = line.strip()
        return bool(re.fullmatch(r"-?\s*\d+\s*-?", line))

    def detect_repeated_headers_footers(
        page_lines_list: list[list[str]], min_repeat: int = 3
    ) -> set[str]:
        candidates: list[str] = []
        for lines in page_lines_list:
            if not lines:
                continue
            top = lines[:2]
            bottom = lines[-2:] if len(lines) >= 2 else lines
            for line in top + bottom:
                line = normalize_text(line)
                if line:
                    candidates.append(line)
        counter = Counter(candidates)
        return {line for line, count in counter.items() if count >= min_repeat}

    def should_merge(prev_line: str, curr_line: str) -> bool:
        prev_line = prev_line.strip()
        curr_line = curr_line.strip()
        if not prev_line or not curr_line:
            return False
        if prev_line.endswith((".", "!", "?", ":", ";")):
            return False
        if len(prev_line) < 20:
            return False
        if re.match(r"^[a-z0-9(\[\-]", curr_line):
            return True
        return True

    def merge_lines_into_paragraphs(lines: list[str]) -> list[str]:
        paragraphs: list[str] = []
        buffer: list[str] = []
        for line in lines:
            line = normalize_text(line)
            if not line:
                if buffer:
                    paragraphs.append(" ".join(buffer).strip())
                    buffer = []
                continue
            if is_likely_page_number(line):
                continue
            if not buffer:
                buffer.append(line)
                continue
            prev_line = buffer[-1]
            if should_merge(prev_line, line):
                buffer.append(line)
            else:
                paragraphs.append(" ".join(buffer).strip())
                buffer = [line]
        if buffer:
            paragraphs.append(" ".join(buffer).strip())
        return paragraphs

    def remove_consecutive_duplicate_paragraphs(paragraphs: list[str]) -> list[str]:
        cleaned: list[str] = []
        prev = None
        for para in paragraphs:
            para = normalize_text(para)
            if not para:
                continue
            if para != prev:
                cleaned.append(para)
            prev = para
        return cleaned

    doc = fitz.open(str(path))
    all_page_lines: list[list[str]] = []
    for page in doc:
        text = page.get_text("text")
        lines = [normalize_text(line) for line in text.splitlines()]
        lines = [line for line in lines if line]
        all_page_lines.append(lines)

    repeated_headers_footers = detect_repeated_headers_footers(all_page_lines)
    blocks: list[dict] = []
    for page_num, lines in enumerate(all_page_lines, start=1):
        cleaned_lines: list[str] = []
        for line in lines:
            line = normalize_text(line)
            if not line:
                continue
            if line in repeated_headers_footers:
                continue
            if is_likely_page_number(line):
                continue
            cleaned_lines.append(line)

        paragraphs = merge_lines_into_paragraphs(cleaned_lines)
        paragraphs = remove_consecutive_duplicate_paragraphs(paragraphs)
        for para in paragraphs:
            blocks.append(
                {
                    "type": "paragraph",
                    "style": "Normal",
                    "page": page_num,
                    "text": para,
                }
            )

    doc.close()
    return blocks


def extract_pdf_ocr_blocks(
    path: Path,
    ocr_language: str = DEFAULT_OCR_LANGUAGE,
    ocr_dpi: int = DEFAULT_OCR_DPI,
) -> list[dict]:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"PDF parser import failed (PyMuPDF/fitz required): {exc}") from exc

    tesseract = shutil.which("tesseract")
    if not tesseract:
        log.warning("[OCR-SKIP] tesseract command not found: %s", path)
        return []

    blocks: list[dict] = []
    doc = fitz.open(str(path))
    temp_dir = Path(tempfile.mkdtemp(prefix="campus_rag_ocr_"))
    try:
        zoom = max(72, ocr_dpi) / 72
        matrix = fitz.Matrix(zoom, zoom)
        for page_num, page in enumerate(doc, start=1):
            image_path = temp_dir / f"page-{page_num:04d}.png"
            page.get_pixmap(matrix=matrix, alpha=False).save(str(image_path))
            proc = subprocess.run(
                [tesseract, str(image_path), "stdout", "-l", ocr_language, "--psm", "6"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=False,
            )
            if proc.returncode != 0:
                err = normalize_text(proc.stderr) or "unknown error"
                log.warning("[OCR-FAIL] %s page %d (%s)", path, page_num, err)
                continue
            text = normalize_text(proc.stdout)
            if text:
                blocks.append(
                    {
                        "type": "ocr_paragraph",
                        "style": "Tesseract",
                        "page": page_num,
                        "text": text,
                    }
                )
    finally:
        doc.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
    return blocks


def extract_pdf_blocks(
    path: Path,
    ocr_mode: str = DEFAULT_PDF_OCR_MODE,
    ocr_language: str = DEFAULT_OCR_LANGUAGE,
    ocr_dpi: int = DEFAULT_OCR_DPI,
) -> list[dict]:
    blocks = extract_pdf_text_blocks(path)
    text = "\n".join(normalize_text(b.get("text", "")) for b in blocks)
    needs_ocr = (
        ocr_mode == "always"
        or (
            ocr_mode == "auto"
            and (
                len(normalize_text(text)) < MIN_USEFUL_PDF_TEXT_CHARS
                or is_likely_broken_korean_text(text)
            )
        )
    )
    if not needs_ocr:
        return blocks
    reason = "empty/short text" if len(normalize_text(text)) < MIN_USEFUL_PDF_TEXT_CHARS else "broken text"
    log.info("[OCR] %s (%s)", path, reason)
    ocr_blocks = extract_pdf_ocr_blocks(path, ocr_language=ocr_language, ocr_dpi=ocr_dpi)
    return ocr_blocks or blocks

__all__ = ["extract_pdf_blocks", "extract_pdf_ocr_blocks", "extract_pdf_text_blocks"]
