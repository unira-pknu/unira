from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.preprocessing.text_cleaning import clean_extracted_text

SUPPORTED_EXTS = {
    ".json",
    ".pdf",
    ".docx",
    ".doc",
    ".hwp",
    ".hwpx",
    ".csv",
    ".xls",
    ".xlsx",
    ".pptx",
    ".txt",
}
DEFAULT_PDF_OCR_MODE = "auto"
DEFAULT_OCR_LANGUAGE = "kor+eng"
DEFAULT_OCR_DPI = 200
MIN_USEFUL_PDF_TEXT_CHARS = 80


def normalize_text(text: object) -> str:
    if text is None:
        return ""
    return clean_extracted_text(text).replace("\n", " ")


def is_likely_broken_korean_text(text: str) -> bool:
    normalized = normalize_text(text)
    if len(normalized) < MIN_USEFUL_PDF_TEXT_CHARS:
        return False
    letters = re.findall(r"[A-Za-z\uac00-\ud7a3\u4e00-\u9fff]", normalized)
    if not letters:
        return False
    hangul_count = len(re.findall(r"[\uac00-\ud7a3]", normalized))
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", normalized))
    replacement_count = normalized.count("\ufffd") + normalized.count("?")
    return (
        (cjk_count / len(letters) >= 0.15 and hangul_count / len(letters) <= 0.10)
        or replacement_count / max(1, len(normalized)) >= 0.08
        or normalized.count("??") >= 8
    )


def extract_csv_blocks(path: Path) -> list[dict]:
    import csv

    blocks: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(f, dialect)
        for row_idx, row in enumerate(reader, start=1):
            cells = [normalize_text(c) for c in row if normalize_text(c)]
            if not cells:
                continue
            blocks.append(
                {
                    "type": "table_row",
                    "style": "CSV",
                    "row": row_idx,
                    "text": " | ".join(cells),
                }
            )
    return blocks


def extract_txt_blocks(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = [normalize_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    blocks = []
    for idx, line in enumerate(lines, start=1):
        blocks.append(
            {
                "type": "paragraph",
                "style": "Text",
                "line": idx,
                "text": line,
            }
        )
    return blocks


def extract_json_blocks(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(doc, dict):
        raise ValueError("JSON root must be an object")

    blocks: list[dict] = []

    def append(block_type: str, style: str, text: object) -> None:
        value = normalize_text(str(text or ""))
        if value:
            blocks.append({"type": block_type, "style": style, "text": value})

    append("title", "PostTitle", doc.get("title", ""))
    append("metadata", "PostDate", doc.get("published_at") or doc.get("date", ""))
    append("metadata", "PostCategory", doc.get("category", ""))
    append("metadata", "PostSubcategory", doc.get("subcategory", ""))

    # Deliberately not routed through append()/normalize_text(): that collapses
    # every newline to a space, which erases the one-entry-per-line shape a
    # crawled directory/contact list has *before* chunk_blocks() ever runs, so
    # its dense-list detector never gets a chance to see it. clean_extracted_text
    # already preserves line structure (it's built for regulation articles).
    body_text = clean_extracted_text(str(doc.get("content") or doc.get("body") or ""))
    if body_text:
        blocks.append({"type": "body", "style": "PostBody", "text": body_text})

    attachments = doc.get("attachments") or []
    if isinstance(attachments, list):
        attachment_lines: list[str] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            name = normalize_text(str(attachment.get("name", "")))
            url = normalize_text(str(attachment.get("final_url") or attachment.get("url", "")))
            saved_path = normalize_text(str(attachment.get("saved_path", "")))
            parts = [part for part in (name, url, saved_path) if part]
            if parts:
                attachment_lines.append(" | ".join(parts))
        if attachment_lines:
            append("attachments", "PostAttachments", "\n".join(attachment_lines))

    return blocks


def extract_html_blocks(path: Path) -> list[dict]:
    try:
        from bs4 import BeautifulSoup
        from bs4.element import Tag
    except Exception as exc:
        raise RuntimeError(f"HTML parser import failed (beautifulsoup4 required): {exc}") from exc

    html = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe", "form"]):
        tag.decompose()

    def attr_text(tag: Tag) -> str:
        attrs: list[str] = []
        tag_id = tag.get("id")
        if tag_id:
            attrs.append(str(tag_id))
        classes = tag.get("class") or []
        attrs.extend(str(cls) for cls in classes)
        return " ".join(attrs).lower()

    def is_noise(tag: Tag) -> bool:
        if tag.name in {"nav", "header", "footer", "aside"}:
            return True
        attrs = attr_text(tag)
        noise_keywords = (
            "nav",
            "gnb",
            "lnb",
            "snb",
            "menu",
            "header",
            "footer",
            "breadcrumb",
            "quick",
            "search",
            "banner",
            "popup",
            "comment",
            "reply",
            "pagination",
            "print",
            "share",
            "zoom",
            "btn",
            "button",
        )
        return any(keyword in attrs for keyword in noise_keywords)

    def text_without_tables(tag: Tag) -> str:
        parts: list[str] = []
        for child in tag.children:
            if isinstance(child, str):
                parts.append(child)
                continue
            if not isinstance(child, Tag):
                continue
            if child.name == "table":
                continue
            parts.append(text_without_tables(child))
        return normalize_text(" ".join(parts))

    def element_score(tag: Tag) -> int:
        text = normalize_text(tag.get_text(" ", strip=True))
        if len(text) < 80:
            return -1
        attrs = attr_text(tag)
        score = len(text)
        if any(keyword in attrs for keyword in ("content", "cont", "board", "view", "article", "body", "main", "read", "detail")):
            score += 400
        if any(keyword in attrs for keyword in ("nav", "menu", "header", "footer", "banner", "search", "quick")):
            score -= 300
        return score

    body = soup.body or soup
    candidates: list[Tag] = [body]
    for tag in body.find_all(["main", "article", "section", "div", "td"]):
        if is_noise(tag):
            continue
        candidates.append(tag)

    root = max(candidates, key=element_score)

    blocks: list[dict] = []
    seen_texts: set[str] = set()
    ignored_texts = {
        "?뺣?",
        "異뺤냼",
        "?꾨┛??",
        "?몄뇙",
        "怨듭쑀",
        "紐⑸줉",
        "?ㅼ쓬湲",
        "?댁쟾湲",
    }

    def append_block(block_type: str, style: str, text: str) -> None:
        text = normalize_text(text)
        if not text or text in seen_texts or text in ignored_texts:
            return
        seen_texts.add(text)
        blocks.append({"type": block_type, "style": style, "text": text})

    title = normalize_text((soup.title.string if soup.title and soup.title.string else ""))
    if title:
        append_block("heading", "HTMLTitle", title)

    def table_to_text(table_tag: Tag) -> str:
        rows: list[str] = []
        for tr in table_tag.find_all("tr"):
            cells: list[str] = []
            for cell in tr.find_all(["th", "td"], recursive=False):
                cell_text = text_without_tables(cell)
                if cell_text:
                    cells.append(cell_text)
            if cells:
                rows.append(" | ".join(cells))
        return "\n".join(rows)

    def walk(tag: Tag) -> None:
        if is_noise(tag):
            return

        if tag.name == "table":
            table_text = table_to_text(tag)
            if table_text:
                append_block("table", "HTMLTable", table_text)
            return

        if tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            append_block("heading", tag.name.upper(), text_without_tables(tag))
        elif tag.name == "p":
            append_block("paragraph", "HTMLParagraph", text_without_tables(tag))
        elif tag.name == "li":
            append_block("list_item", "HTMLListItem", text_without_tables(tag))
        elif tag.name in {"blockquote", "pre"}:
            append_block("paragraph", tag.name.upper(), text_without_tables(tag))

        for child in tag.children:
            if isinstance(child, Tag):
                walk(child)

    walk(root)

    if blocks:
        return blocks

    fallback_lines = [
        normalize_text(line)
        for line in root.get_text("\n", strip=True).splitlines()
        if normalize_text(line)
    ]
    for line in fallback_lines:
        append_block("paragraph", "HTMLText", line)
    return blocks


def extract_blocks(
    path: Path,
    pdf_ocr_mode: str = DEFAULT_PDF_OCR_MODE,
    ocr_language: str = DEFAULT_OCR_LANGUAGE,
    ocr_dpi: int = DEFAULT_OCR_DPI,
) -> list[dict]:
    ext = path.suffix.lower()
    if ext == ".json":
        return extract_json_blocks(path)
    if ext == ".pdf":
        from scripts.preprocessing.extractors.pdf import extract_pdf_blocks

        return extract_pdf_blocks(
            path,
            ocr_mode=pdf_ocr_mode,
            ocr_language=ocr_language,
            ocr_dpi=ocr_dpi,
        )
    if ext in {".docx", ".doc"}:
        from scripts.preprocessing.extractors.docx import extract_docx_like_blocks

        return extract_docx_like_blocks(path)
    if ext in {".html", ".htm"}:
        return extract_html_blocks(path)
    if ext == ".csv":
        return extract_csv_blocks(path)
    if ext == ".xls":
        from scripts.preprocessing.extractors.xlsx import extract_xls_blocks

        return extract_xls_blocks(path)
    if ext == ".xlsx":
        from scripts.preprocessing.extractors.xlsx import extract_xlsx_blocks

        return extract_xlsx_blocks(path)
    if ext == ".pptx":
        from scripts.preprocessing.extractors.pptx import extract_pptx_blocks

        return extract_pptx_blocks(path, normalize_text)
    if ext == ".txt":
        return extract_txt_blocks(path)
    if ext == ".hwpx":
        from scripts.preprocessing.extractors.hwp import extract_hwpx_blocks

        return extract_hwpx_blocks(path)
    if ext == ".hwp":
        from scripts.preprocessing.extractors.hwp import extract_hwp_blocks

        return extract_hwp_blocks(path)
    raise ValueError(f"unsupported extension: {ext}")


__all__ = [
    "DEFAULT_OCR_DPI",
    "DEFAULT_OCR_LANGUAGE",
    "DEFAULT_PDF_OCR_MODE",
    "MIN_USEFUL_PDF_TEXT_CHARS",
    "SUPPORTED_EXTS",
    "extract_blocks",
    "extract_csv_blocks",
    "extract_html_blocks",
    "extract_json_blocks",
    "extract_txt_blocks",
    "is_likely_broken_korean_text",
    "normalize_text",
]
