"""Layout-aware chunking and an isolated local vector store for OCR review data.

Reads human-reviewed body-image layout labels (``*.reviewed.jsonl``) plus the
optional table/diagram extraction JSONL files produced by the layout OCR
pipeline, turns each image into layout-specific chunks, and stores them in a
local, file-based vector store under ``files/_test_db/<name>/``.

The store never talks to Supabase: it is a directory holding
``records.jsonl`` + ``embeddings.npy`` + ``manifest.json``. Store names must
match ``test_layout_rag_YYYYMMDD[_suffix]`` so it cannot be confused with the
production ``rag_chunks`` table.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REVIEWED_ROOT = PROJECT_ROOT / "files" / "_reviewed" / "body_image_ocr"
TEST_DB_ROOT = PROJECT_ROOT / "files" / "_test_db"
STORE_NAME_PATTERN = re.compile(r"^test_layout_rag_\d{8}(?:_[A-Za-z0-9]+)?$")
STORE_MARKER = "campus-rag/layout_rag_test_store"

INGESTED_LAYOUTS = {"prose", "table", "diagram", "mixed"}
DEFAULT_CHUNK_CHARS = 500
DEFAULT_CHUNK_OVERLAP = 80


def default_store_name(today: datetime | None = None) -> str:
    return f"test_layout_rag_{(today or datetime.now()).strftime('%Y%m%d')}"


def store_path(name: str) -> Path:
    if not STORE_NAME_PATTERN.match(name):
        raise ValueError(f"test store name must match test_layout_rag_YYYYMMDD[_suffix]: {name!r}")
    return TEST_DB_ROOT / name


# --------------------------------------------------------------------------- #
# Input loading
# --------------------------------------------------------------------------- #

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number} is not valid JSON: {exc}") from exc
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("source_path") or "").replace("\\", "/"),
            str(row.get("image_sha256") or "").lower())


def load_extraction_index(path: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    return {_key(row): row for row in read_jsonl(path) if all(_key(row))}


# --------------------------------------------------------------------------- #
# Text rendering per layout type
# --------------------------------------------------------------------------- #

def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _paragraphs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [line for line in (_clean(part) for part in value.splitlines()) if line]
    if not isinstance(value, list):
        return []
    output = []
    for item in value:
        text = _clean(item.get("text")) if isinstance(item, dict) else _clean(item)
        if text:
            output.append(text)
    return output


def _cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        return _clean(cell.get("text") or cell.get("value") or cell.get("content"))
    return _clean(cell) if isinstance(cell, str) else _clean(str(cell)) if cell is not None else ""


def _table_grid(table: dict[str, Any]) -> list[list[str]]:
    """Return a dense row-major grid.

    Row-spanned text repeats on every row (each markdown row stays
    self-contained for retrieval); column-spanned text fills only its first cell.
    """
    positioned: list[tuple[int, int, int, int, str]] = []
    plain_rows: list[list[str]] = []
    rows = table.get("rows")
    cells = table.get("cells")
    if isinstance(rows, list):
        for row_number, row in enumerate(rows):
            row_cells = row.get("cells", []) if isinstance(row, dict) else row
            if not isinstance(row_cells, list):
                continue
            if all(isinstance(c, dict) and isinstance(c.get("row"), int)
                   and isinstance(c.get("column"), int) for c in row_cells) and row_cells:
                for c in row_cells:
                    positioned.append((c["row"], c["column"], int(c.get("rowspan") or 1),
                                       int(c.get("colspan") or 1), _cell_text(c)))
            else:
                plain_rows.append([_cell_text(c) for c in row_cells])
    elif isinstance(cells, list):
        for c in cells:
            if isinstance(c, dict) and isinstance(c.get("row"), int):
                column = c.get("column") if isinstance(c.get("column"), int) else 0
                positioned.append((c["row"], column, int(c.get("rowspan") or 1),
                                   int(c.get("colspan") or 1), _cell_text(c)))
    if positioned:
        height = max(r + rs for r, _, rs, _, _ in positioned)
        width = max(col + cs for _, col, _, cs, _ in positioned)
        grid = [["" for _ in range(width)] for _ in range(height)]
        for r, col, rs, cs, text in positioned:
            for dr in range(max(rs, 1)):
                grid[r + dr][col] = text
        return grid
    return plain_rows


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def table_to_markdown_rows(table: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """Return (caption, [header_line, separator_line], body_lines) in markdown."""
    caption = _clean(table.get("title") or table.get("caption"))
    grid = [row for row in _table_grid(table) if any(row)]
    if not grid:
        fallback = _clean(table.get("text"))
        return caption, [], [fallback] if fallback else []
    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    header = "| " + " | ".join(_md_escape(c) for c in grid[0]) + " |"
    separator = "|" + "---|" * width
    body = ["| " + " | ".join(_md_escape(c) for c in row) + " |" for row in grid[1:]]
    return caption, [header, separator], body


def diagram_to_sentences(diagram: dict[str, Any]) -> list[str]:
    """Rewrite nodes/edges as plain Korean sentences so they embed well."""
    if not isinstance(diagram, dict):
        return []
    sentences: list[str] = []
    title = _clean(diagram.get("diagram_title") or diagram.get("title"))
    nodes = [n for n in diagram.get("nodes") or [] if isinstance(n, dict)]
    labels = {str(n.get("id")): _clean(n.get("text")) for n in nodes if _clean(n.get("text"))}
    if title:
        sentences.append(f"이 도식은 '{title}'에 관한 내용이다.")
    if labels:
        sentences.append("도식의 구성 요소는 " + ", ".join(f"'{t}'" for t in labels.values()) + "이다.")

    def describe(edges: Any, verified: bool) -> None:
        for edge in edges if isinstance(edges, list) else []:
            if not isinstance(edge, dict):
                continue
            src = labels.get(str(edge.get("from")), _clean(str(edge.get("from") or "")))
            dst = labels.get(str(edge.get("to")), _clean(str(edge.get("to") or "")))
            if not (src and dst):
                continue
            relation = _clean(edge.get("label") or edge.get("relation") or "")
            sentence = f"'{src}' 다음 단계는 '{dst}'이다." if not relation else \
                f"'{src}'은(는) '{dst}'와(과) '{relation}' 관계이다."
            if not verified:
                sentence = sentence.rstrip(".") + "(관계는 검토 필요)."
            sentences.append(sentence)

    describe(diagram.get("edges"), True)
    describe(diagram.get("unverified_edges"), False)
    for block in diagram.get("unassigned_text_blocks") or []:
        text = _clean(block.get("text")) if isinstance(block, dict) else _clean(block)
        if text:
            sentences.append(f"도식에 함께 적힌 내용: {text}")
    return sentences


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def split_prose(text: str, max_chars: int = DEFAULT_CHUNK_CHARS,
                overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    pieces = [p.strip() for p in re.split(r"(?<=[.!?。])\s+|\n+", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        while len(piece) > max_chars:  # hard-wrap a single oversized sentence
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece[:max_chars])
            piece = piece[max_chars - overlap:]
        candidate = f"{current} {piece}".strip()
        if len(candidate) > max_chars and current:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail} {piece}".strip()
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def split_table(caption: str, header: list[str], body: list[str],
                max_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Split by rows, repeating caption + header so every chunk is a valid table."""
    prefix = ([caption] if caption else []) + header
    if not body:
        return ["\n".join(prefix)] if header else []
    chunks: list[str] = []
    current: list[str] = []
    for line in body:
        if current and len("\n".join(prefix + current + [line])) > max_chars:
            chunks.append("\n".join(prefix + current))
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(prefix + current))
    return chunks


def split_sentences(sentences: list[str], max_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Group diagram sentences; the first (title) sentence prefixes every chunk."""
    if not sentences:
        return []
    head = sentences[0] if sentences[0].startswith("이 도식은") else ""
    rest = sentences[1:] if head else sentences
    chunks: list[str] = []
    current = head
    for sentence in rest:
        candidate = f"{current} {sentence}".strip()
        if len(candidate) > max_chars and current and current != head:
            chunks.append(current)
            current = f"{head} {sentence}".strip()
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _parse_expected(value: Any) -> Any:
    """``expected_text_or_structure`` may be plain text or a JSON string."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "{[":
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
        return stripped
    return value


def _structure_parts(value: Any) -> tuple[list[str], list[dict[str, Any]], dict[str, Any] | None]:
    """Split a result/structure object into (paragraphs, tables, diagram)."""
    if isinstance(value, str):
        return _paragraphs(value), [], None
    if isinstance(value, list):
        return _paragraphs(value), [], None
    if not isinstance(value, dict):
        return [], [], None
    paragraphs = _paragraphs(value.get("paragraphs"))
    tables = [t for t in value.get("tables") or [] if isinstance(t, dict)]
    if not tables and ("rows" in value or "cells" in value):
        tables = [value]
    diagram = value.get("diagram")
    if not isinstance(diagram, dict) and any(k in value for k in ("nodes", "edges", "diagram_title")):
        diagram = value
    if isinstance(diagram, dict) and not (diagram.get("nodes") or diagram.get("edges")
                                          or diagram.get("unverified_edges")
                                          or diagram.get("unassigned_text_blocks")):
        diagram = None
    return paragraphs, tables, diagram


def build_image_chunks(review: dict[str, Any], extraction: dict[str, Any] | None,
                       *, max_chars: int = DEFAULT_CHUNK_CHARS,
                       overlap: int = DEFAULT_CHUNK_OVERLAP) -> tuple[list[dict[str, Any]], str | None]:
    """Chunk one reviewed image. Returns (chunks, skip_reason)."""
    layout = review.get("reviewer_layout_type") or review.get("suggested_layout_type") or "unknown"
    if layout not in INGESTED_LAYOUTS:
        return [], f"LAYOUT_NOT_INGESTED:{layout}"

    expected = _parse_expected(review.get("expected_text_or_structure"))
    if expected:  # the reviewer's own text wins over machine OCR
        content, origin = expected, "reviewer_expected_text_or_structure"
    elif extraction and isinstance(extraction.get("result"), dict):
        content, origin = extraction["result"], f"{extraction.get('extractor') or 'extraction'}_result"
    else:
        return [], "NO_TEXT_AVAILABLE"

    paragraphs, tables, diagram = _structure_parts(content)
    pieces: list[tuple[str, str]] = []  # (chunk_layout, text)
    if layout in {"table", "mixed"} or tables:
        for table in tables:
            caption, header, body = table_to_markdown_rows(table)
            pieces.extend(("table", c) for c in split_table(caption, header, body, max_chars))
    if layout in {"diagram", "mixed"} or diagram:
        sentences = diagram_to_sentences(diagram or {})
        pieces.extend(("diagram", c) for c in split_sentences(sentences, max_chars))
    if paragraphs:
        # A diagram/table reviewed with only plain text: keep the reviewer's layout label.
        chunk_layout = layout if layout != "mixed" else "prose"
        if layout == "diagram" and not diagram:
            paragraphs = ["도식 설명: " + p for p in paragraphs]
        pieces.extend((chunk_layout, c) for c in split_prose("\n".join(paragraphs), max_chars, overlap))
    if not pieces:
        return [], "NO_CHUNKABLE_TEXT"

    source_path, digest = _key(review)
    provenance = {
        "source_path": source_path,
        "image_sha256": digest,
        "image_url": review.get("image_url") or review.get("source_url"),
        "saved_path": review.get("saved_path"),
        "review_file": review.get("_review_file"),
        "reviewer_layout_type": review.get("reviewer_layout_type"),
        "suggested_layout_type": review.get("suggested_layout_type"),
        "reviewer_notes": review.get("reviewer_notes") or "",
        "content_origin": origin,
        "extractor": (extraction or {}).get("extractor"),
        "extractor_version": (extraction or {}).get("extractor_version"),
        "extraction_status": (extraction or {}).get("status"),
        "synthetic": bool(review.get("synthetic")),
    }
    chunks = []
    for index, (chunk_layout, text) in enumerate(pieces):
        chunks.append({
            "chunk_id": f"{digest[:16]}:{index}",
            "content": text,
            "layout_type": chunk_layout,
            "image_layout_type": layout,
            "chunk_index": index,
            "provenance": provenance,
        })
    return chunks, None


def collect_chunks(reviewed_files: Iterable[Path], table_index: dict, diagram_index: dict,
                   *, limit: int, max_chars: int = DEFAULT_CHUNK_CHARS,
                   overlap: int = DEFAULT_CHUNK_OVERLAP) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (chunks, per-image report). ``limit`` caps reviewed images processed."""
    chunks: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in reviewed_files:
        for row in read_jsonl(path):
            if len(report) >= limit:
                return chunks, report
            key = _key(row)
            if not all(key) or key in seen:
                continue
            seen.add(key)
            row = {**row, "_review_file": _relative(path)}
            layout = row.get("reviewer_layout_type")
            extraction = (table_index.get(key) if layout == "table" else diagram_index.get(key)) \
                or table_index.get(key) or diagram_index.get(key)
            image_chunks, reason = build_image_chunks(row, extraction, max_chars=max_chars, overlap=overlap)
            chunks.extend(image_chunks)
            report.append({"source_path": key[0], "image_sha256": key[1], "layout_type": layout,
                           "chunks": len(image_chunks), "skip_reason": reason})
    return chunks, report


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# --------------------------------------------------------------------------- #
# Local vector store
# --------------------------------------------------------------------------- #

class LocalVectorStore:
    """Flat cosine-similarity store persisted as numpy + JSONL files."""

    def __init__(self, path: Path, records: list[dict[str, Any]], embeddings: Any, manifest: dict[str, Any]):
        self.path = path
        self.records = records
        self.embeddings = embeddings
        self.manifest = manifest

    @classmethod
    def create(cls, name: str, records: list[dict[str, Any]], embeddings: Any, *,
               manifest_extra: dict[str, Any], replace: bool = False) -> "LocalVectorStore":
        import numpy as np

        path = store_path(name)
        if path.exists():
            manifest_file = path / "manifest.json"
            owned = manifest_file.is_file() and \
                json.loads(manifest_file.read_text(encoding="utf-8")).get("marker") == STORE_MARKER
            if not replace:
                raise FileExistsError(f"{path} already exists; pass --replace to rebuild this test store")
            if not owned:
                raise PermissionError(f"{path} exists but is not a layout_rag test store; refusing to replace")
        path.mkdir(parents=True, exist_ok=True)
        matrix = np.asarray(embeddings, dtype="float32")
        if matrix.shape[0] != len(records):
            raise ValueError("embedding count does not match record count")
        manifest = {
            "marker": STORE_MARKER,
            "name": name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "record_count": len(records),
            "dimensions": int(matrix.shape[1]) if matrix.size else 0,
            **manifest_extra,
        }
        np.save(path / "embeddings.npy", matrix)
        with (path / "records.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        (path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return cls(path, records, matrix, manifest)

    @classmethod
    def open(cls, name: str) -> "LocalVectorStore":
        import numpy as np

        path = store_path(name)
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("marker") != STORE_MARKER:
            raise PermissionError(f"{path} is not a layout_rag test store")
        return cls(path, read_jsonl(path / "records.jsonl"), np.load(path / "embeddings.npy"), manifest)

    def search(self, query_vector: Any, top_k: int = 5) -> list[dict[str, Any]]:
        import numpy as np

        if not self.records:
            return []
        scores = self.embeddings @ np.asarray(query_vector, dtype="float32")
        order = np.argsort(-scores)[:top_k]
        return [{**self.records[i], "similarity": float(scores[i]), "rank": rank}
                for rank, i in enumerate(order, start=1)]


def load_embedder(model_name: str | None = None):
    """Same local sentence-transformers model as the backend (no API cost)."""
    from scripts.vectorization.common import DEFAULT_SENTENCE_MODEL, load_sentence_model
    model = model_name or DEFAULT_SENTENCE_MODEL
    return model, load_sentence_model(model, device="cpu")


def embed_texts(model: Any, texts: list[str]) -> Any:
    from scripts.vectorization.common import encode_normalized
    return encode_normalized(model, texts)


def embedding_text(record: dict[str, Any]) -> str:
    """Text actually embedded: layout hint + content (content stays unchanged)."""
    hint = {"table": "[표]", "diagram": "[도식]", "prose": "[본문]"}.get(record["layout_type"], "")
    return f"{hint} {record['content']}".strip()
