from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.preprocessing.text_cleaning import clean_extracted_text
from scripts.crawlers.common.reader import infer_dataset_from_path, read_document
from scripts.crawlers.common.storage import department_legacy_roots, get_dataset_root
from scripts.preprocessing.extractors.common import (
    DEFAULT_OCR_DPI,
    DEFAULT_OCR_LANGUAGE,
    DEFAULT_PDF_OCR_MODE,
    SUPPORTED_EXTS as EXTRACTABLE_EXTS,
    extract_blocks,
    normalize_text,
)
from scripts.preprocessing.body_image_review import apply_body_image_reviews, body_image_input_mtime

log = logging.getLogger(__name__)

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "preprocessing.log"


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )

DEFAULT_CHUNK_SIZE = 900
DEFAULT_CHUNK_OVERLAP = 120
ARCHIVE_EXTS = {".zip"}
SUPPORTED_EXTS = EXTRACTABLE_EXTS | ARCHIVE_EXTS
MAX_ARCHIVE_MEMBERS = 300
MAX_ARCHIVE_MEMBER_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 500 * 1024 * 1024


def parse_file_exts(values: list[str] | None) -> set[str] | None:
    if not values:
        return None

    exts: set[str] = set()
    for value in values:
        for item in value.split(","):
            ext = item.strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = f".{ext}"
            exts.add(ext)
    return exts or None


def rel_project_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def make_slug(rel_path: str) -> str:
    return hashlib.md5(rel_path.encode("utf-8")).hexdigest()[:12]


def preserved_source_slug(source_path: str, output_path: Path) -> str:
    """Keep chunk identity when a migrated source is preprocessed again."""
    candidates = [(output_path, source_path)]
    parts = output_path.parts
    dataset = infer_dataset_from_path(output_path)
    if dataset and "files" in parts:
        project_root = Path(*parts[:parts.index("files")])
        current_root = get_dataset_root(project_root, dataset)
        if output_path.is_relative_to(current_root):
            relative_output = output_path.relative_to(current_root)
            relative_prefix = current_root.relative_to(project_root).as_posix() + "/"
            for root in department_legacy_roots(project_root, dataset):
                old_prefix = root.relative_to(project_root).as_posix() + "/"
                previous_source = source_path.replace(relative_prefix, old_prefix, 1)
                previous_source = previous_source.replace(current_root.as_posix() + "/", root.as_posix() + "/", 1)
                candidates.append((root / relative_output, previous_source))
    for candidate, previous_source in candidates:
        if not candidate.is_file():
            continue
        try:
            previous = json.loads(candidate.read_text(encoding="utf-8"))
            if previous.get("source_path") == previous_source and previous.get("slug"):
                return str(previous["slug"])
        except (OSError, ValueError, AttributeError):
            pass
    return make_slug(source_path)


def ensure_output_path(
    input_file: Path,
    input_root: Path,
    output_root: Path,
    *,
    layout: str = "by_ext",
) -> Path:
    rel = input_file.resolve().relative_to(input_root.resolve())
    if layout == "flat":
        return (output_root / rel).with_suffix(".json")
    if layout == "by_ext":
        ext_dir = input_file.suffix.lower().lstrip(".") or "unknown"
        return (output_root / ext_dir / rel).with_suffix(".json")
    raise ValueError(f"unsupported layout: {layout}")


def decoded_archive_member_name(info: zipfile.ZipInfo) -> str:
    """Recover legacy Korean ZIP names while preserving explicitly marked UTF-8."""
    if info.flag_bits & 0x800 or info.filename.isascii():
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for encoding in ("utf-8", "cp949"):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding == "utf-8" or re.search(r"[\uac00-\ud7a3]", decoded):
            return decoded
    return info.filename


def is_safe_archive_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or normalized.endswith("/"):
        return False
    if path.is_absolute() or any(part in {"", ".", ".."} or ":" in part or "\x00" in part for part in path.parts):
        return False
    if any(part == "__MACOSX" for part in path.parts):
        return False
    if path.name in {".DS_Store", "Thumbs.db"}:
        return False
    return True


def archive_member_output_path(
    archive_file: Path,
    input_root: Path,
    output_root: Path,
    member_name: str,
    *,
    layout: str = "by_ext",
) -> Path:
    archive_rel = archive_file.resolve().relative_to(input_root.resolve())
    member_rel = PurePosixPath(member_name.replace("\\", "/"))
    archive_stem_rel = archive_rel.with_suffix("")
    if layout == "flat":
        return (output_root / archive_stem_rel / Path(*member_rel.parts)).with_suffix(".json")
    if layout == "by_ext":
        return (output_root / "zip" / archive_stem_rel / Path(*member_rel.parts)).with_suffix(".json")
    raise ValueError(f"unsupported layout: {layout}")


def load_attachment_index(output_json_root: Path, project_root: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    if not output_json_root.exists():
        return index

    for jf in output_json_root.rglob("*.json"):
        try:
            payload = json.loads(jf.read_text(encoding="utf-8"))
            doc = read_document(
                payload, dataset=infer_dataset_from_path(jf), project_root=project_root
            )
        except Exception:
            continue

        base_doc_info = {
            "schema_version": doc.get("schema_version", ""),
            "document_id": doc.get("id", ""),
            "source_dataset": doc.get("source_dataset", ""),
            "source_id": doc.get("source_id", ""),
            "doc_title": doc.get("title", ""),
            "doc_url": doc.get("url", ""),
            "category": doc.get("category", ""),
            "subcategory": doc.get("subcategory", ""),
        }
        for a in doc.get("attachments", []) or []:
            saved_path = a.get("saved_path", "")
            if not saved_path:
                continue
            saved_rel = rel_project_path(project_root / saved_path, project_root)
            index[saved_rel] = {
                **base_doc_info,
                "attachment_name": a.get("name", ""),
                "attachment_url": a.get("final_url") or a.get("url", ""),
                "source_page_url": a.get("source_page_url", ""),
                "source_site": a.get("source_site", ""),
                "downloaded_from_url": a.get("final_url") or a.get("downloaded_from_url", ""),
                "attachment_id": a.get("id", ""),
                "attachment_sha256": a.get("sha256", ""),
                "content_type": a.get("content_type", ""),
                "_source_json_mtime": jf.stat().st_mtime,
            }
    return index


def extract_crawled_json_provenance(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        doc = read_document(
            payload, dataset=infer_dataset_from_path(path), project_root=PROJECT_ROOT
        )
    except Exception:
        return {}
    if not isinstance(doc, dict):
        return {}

    attachments = [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "url": str(item.get("final_url") or item.get("url") or ""),
            "sha256": item.get("sha256"),
        }
        for item in (doc.get("attachments") or [])
        if isinstance(item, dict) and item.get("url")
    ]

    return {
        "schema_version": doc.get("schema_version", ""),
        "document_id": doc.get("id", ""),
        "source_dataset": doc.get("source_dataset", ""),
        "source_id": doc.get("source_id", ""),
        "content_hash": doc.get("content_hash", ""),
        "doc_title": doc.get("title", ""),
        "doc_url": doc.get("url", ""),
        "category": doc.get("category", ""),
        "subcategory": doc.get("subcategory", ""),
        "doc_type": doc.get("type", "") or "post",
        "source_kind": "post",
        "published_at": doc.get("published_at"),
        "updated_at": doc.get("updated_at"),
        "effective_at": doc.get("effective_at"),
        "is_notice": doc.get("is_notice", False),
        "source_page_url": doc.get("url", ""),
        "source_site": doc.get("source_site", ""),
        "crawl": doc.get("crawl", {}),
        "attachments": attachments,
    }


def build_metadata_fallback_blocks(
    input_file: Path,
    rel: str,
    rel_in_input: str,
    provenance: dict,
) -> list[dict]:
    fields = [
        ("문서 파일명", input_file.name),
        ("문서 경로", rel_in_input),
        ("문서 제목", provenance.get("doc_title", "")),
        ("게시글 URL", provenance.get("doc_url", "")),
        ("카테고리", provenance.get("category", "")),
        ("하위 카테고리", provenance.get("subcategory", "")),
        ("첨부파일명", provenance.get("attachment_name", "")),
        ("첨부파일 URL", provenance.get("attachment_url", "")),
        ("출처 페이지", provenance.get("source_page_url", "")),
        ("출처 사이트", provenance.get("source_site", "")),
        ("저장 경로", rel),
    ]
    lines = [f"{label}: {value}" for label, value in fields if normalize_text(str(value))]
    if not lines:
        return []
    return [{"type": "metadata_fallback", "style": "Metadata", "text": "\n".join(lines)}]


def public_provenance(provenance: dict) -> dict:
    return {key: value for key, value in provenance.items() if not str(key).startswith("_")}


_OVERSIZED_BOUNDARY_RE = re.compile(r"[\n。．\.!?！？]")


def _split_oversized_text(text: str, max_size: int, overlap: int) -> list[str]:
    """단일 텍스트가 max_size를 넘으면 문장/공백 경계 기준 슬라이딩 분할.

    경계를 못 찾으면 문자 단위로 자른다. overlap은 인접 조각이 겹치도록 유지한다.
    """
    if len(text) <= max_size:
        return [text]
    if max_size <= overlap or max_size <= 0:
        step = max(max_size, 1)
        return [text[i : i + step] for i in range(0, len(text), step)]

    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_size, len(text))
        if end < len(text):
            # 윈도우 뒤쪽 20% 안에서 문장/문단 경계를 찾아 그 지점에서 자른다.
            search_from = start + int(max_size * 0.8)
            tail = text[search_from:end]
            matches = list(_OVERSIZED_BOUNDARY_RE.finditer(tail))
            if matches:
                end = search_from + matches[-1].end()
            else:
                ws_pos = text.rfind(" ", search_from, end)
                if ws_pos > search_from:
                    end = ws_pos + 1
        pieces.append(text[start:end])
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return pieces


# A "dense list" paragraph is a directory-style block extracted from a PDF/HTML
# table as one run of text (e.g. "학과별/부서별 연락처" -- dozens of
# "department: phone number" lines back to back). Packed whole, it easily
# stays under DEFAULT_CHUNK_SIZE as ONE topically-unfocused chunk mixing 50
# unrelated departments -- fine by raw character count, bad for retrieval,
# since a query about one specific department has to compete semantically
# against 49 others in the same chunk. Detect this shape and split it into
# small item-groups regardless of whether it would otherwise fit whole.
DENSE_LIST_GROUP_SIZE = 8
DENSE_LIST_MIN_LINES = 6
DENSE_LIST_MIN_RATIO = 0.6
_DENSE_LIST_DIGIT_RUN_RE = re.compile(r"\d{2,4}[-~]\d{2,4}")
_DENSE_LIST_LABEL_RE = re.compile(r"^[^\s:：]{1,20}[:：]\s*\S")


def _is_dense_list_line(line: str) -> bool:
    """One directory row: short, and either 'label: value' or has a
    hyphenated digit run (phone extension, room number, date range, etc.)."""
    if not line or len(line) > 60:
        return False
    if _DENSE_LIST_DIGIT_RUN_RE.search(line):
        return True
    return bool(_DENSE_LIST_LABEL_RE.match(line))


def _is_dense_list_text(text: str) -> bool:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) < DENSE_LIST_MIN_LINES:
        return False
    matches = sum(1 for ln in lines if _is_dense_list_line(ln))
    return (matches / len(lines)) >= DENSE_LIST_MIN_RATIO


def _split_dense_list_text(text: str, group_size: int = DENSE_LIST_GROUP_SIZE) -> list[str]:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return []
    groups = [lines[i : i + group_size] for i in range(0, len(lines), group_size)]
    # A tiny orphan tail group (e.g. one leftover line) tends to fall under
    # retrieval's own too-short/weak-chunk filter -- fold it back into the
    # previous group instead of shipping a standalone group that never
    # surfaces in search anyway.
    min_last_group = max(1, group_size // 2)
    if len(groups) > 1 and len(groups[-1]) < min_last_group:
        groups[-2].extend(groups.pop())
    return ["\n".join(group) for group in groups]


def chunk_blocks(
    blocks: list[dict],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[dict]:
    # paragraph 단위 청킹이라, 단일 block의 text가 chunk_size를 넘으면
    # 아래 누적 루프의 size 체크가 동작하지 않아 거대 chunk가 생긴다.
    # 미리 펼쳐서 모든 텍스트가 chunk_size 이하가 되도록 한다.
    # standalone=True인 조각은 다른 block과 합쳐지지 않고 그 자체로 하나의
    # chunk가 된다 (dense list를 부서별 소그룹으로 쪼갠 결과).
    #
    # dense-list 판별은 반드시 normalize_text() 이전의 원본 텍스트로 해야 한다
    # -- normalize_text가 줄바꿈을 공백으로 접어버려서, 정규화 후에는 한 줄에
    # 한 항목씩 늘어선 디렉터리 구조 자체가 사라진다.
    texts: list[str] = []
    standalone: list[bool] = []
    for b in blocks:
        original = str(b.get("text", ""))
        if _is_dense_list_text(original):
            for group in _split_dense_list_text(original):
                normalized_group = normalize_text(group)
                if not normalized_group:
                    continue
                pieces = (
                    [normalized_group]
                    if len(normalized_group) <= chunk_size
                    else _split_oversized_text(normalized_group, chunk_size, overlap)
                )
                for piece in pieces:
                    texts.append(piece)
                    standalone.append(True)
            continue

        t = normalize_text(original)
        if not t:
            continue
        if len(t) <= chunk_size:
            texts.append(t)
            standalone.append(False)
        else:
            for piece in _split_oversized_text(t, chunk_size, overlap):
                texts.append(piece)
                standalone.append(False)

    if not texts:
        return []

    chunks: list[dict] = []
    current: list[str] = []
    current_len = 0
    idx = 1

    for text, is_standalone in zip(texts, standalone):
        if is_standalone:
            if current:
                chunk_text = "\n".join(current)
                chunks.append(
                    {
                        "chunk_id": idx,
                        "text": chunk_text,
                        "num_lines": len(current),
                        "num_chars": len(chunk_text),
                    }
                )
                idx += 1
                current = []
                current_len = 0
            chunks.append(
                {
                    "chunk_id": idx,
                    "text": text,
                    "num_lines": text.count("\n") + 1,
                    "num_chars": len(text),
                }
            )
            idx += 1
            continue

        add_len = len(text) + (1 if current else 0)
        if current and (current_len + add_len > chunk_size):
            chunk_text = "\n".join(current)
            chunks.append(
                {
                    "chunk_id": idx,
                    "text": chunk_text,
                    "num_lines": len(current),
                    "num_chars": len(chunk_text),
                }
            )
            idx += 1

            if overlap > 0:
                tail = chunk_text[-overlap:]
                current = [tail] if tail.strip() else []
                current_len = len(tail) if current else 0
            else:
                current = []
                current_len = 0

        current.append(text)
        current_len += len(text) + (1 if len(current) > 1 else 0)

    if current:
        chunk_text = "\n".join(current)
        chunks.append(
            {
                "chunk_id": idx,
                "text": chunk_text,
                "num_lines": len(current),
                "num_chars": len(chunk_text),
            }
        )
    return chunks


def save_preprocessed_file(
    input_file: Path,
    input_root: Path,
    output_root: Path,
    project_root: Path,
    attachment_index: dict[str, dict],
    chunk_size: int,
    chunk_overlap: int,
    pdf_ocr_mode: str,
    ocr_language: str,
    ocr_dpi: int,
    layout: str = "by_ext",
) -> tuple[bool, str]:
    rel = rel_project_path(input_file, project_root)
    ext = input_file.suffix.lower()
    if ext in ARCHIVE_EXTS:
        return save_preprocessed_archive(
            archive_file=input_file,
            input_root=input_root,
            output_root=output_root,
            project_root=project_root,
            attachment_index=attachment_index,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            pdf_ocr_mode=pdf_ocr_mode,
            ocr_language=ocr_language,
            ocr_dpi=ocr_dpi,
            layout=layout,
        )
    if ext not in SUPPORTED_EXTS:
        return False, "unsupported"

    rel_in_input = rel_project_path(input_file, input_root)
    provenance = attachment_index.get(rel, {})
    if ext == ".json":
        provenance = {**extract_crawled_json_provenance(input_file), **provenance}
    elif not provenance.get("source_kind"):
        provenance = {**provenance, "source_kind": "attachment"}
    provenance = public_provenance(provenance)
    blocks = extract_blocks(
        input_file,
        pdf_ocr_mode=pdf_ocr_mode,
        ocr_language=ocr_language,
        ocr_dpi=ocr_dpi,
    )
    body_image_provenance: list[dict] = []
    body_image_review_warnings: list[str] = []
    if ext == ".json":
        blocks, body_image_provenance, body_image_review_warnings = apply_body_image_reviews(
            input_file, blocks, project_root=project_root
        )
    for block in blocks:
        block["text"] = clean_extracted_text(block.get("text", ""))
    blocks = [b for b in blocks if normalize_text(b.get("text", ""))]
    chunks = chunk_blocks(blocks, chunk_size=chunk_size, overlap=chunk_overlap)
    used_metadata_fallback = False
    if not chunks:
        fallback_blocks = build_metadata_fallback_blocks(input_file, rel, rel_in_input, provenance)
        fallback_chunks = chunk_blocks(fallback_blocks, chunk_size=chunk_size, overlap=0)
        if fallback_chunks:
            blocks = fallback_blocks
            chunks = fallback_chunks
            used_metadata_fallback = True

    out_path = ensure_output_path(input_file, input_root, output_root, layout=layout)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "slug": preserved_source_slug(rel, out_path),
        "source_file": input_file.name,
        "source_path": rel,
        "source_relative_to_input": rel_in_input,
        "source_ext": ext,
        "processed_at": datetime.now().isoformat(),
        "num_blocks": len(blocks),
        "total_chars": sum(len(b["text"]) for b in blocks),
        "num_chunks": len(chunks),
        "used_metadata_fallback": used_metadata_fallback,
        "provenance": provenance,
        "blocks": blocks,
        "chunks": chunks,
    }
    if body_image_provenance:
        result["body_image_provenance"] = body_image_provenance
    if body_image_review_warnings:
        result["body_image_review_warnings"] = body_image_review_warnings

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, out_path.as_posix()


def save_preprocessed_archive_member(
    archive_file: Path,
    member_file: Path,
    member_name: str,
    input_root: Path,
    output_root: Path,
    project_root: Path,
    archive_provenance: dict,
    chunk_size: int,
    chunk_overlap: int,
    pdf_ocr_mode: str,
    ocr_language: str,
    ocr_dpi: int,
    layout: str = "by_ext",
) -> tuple[bool, str]:
    archive_rel = rel_project_path(archive_file, project_root)
    archive_rel_in_input = rel_project_path(archive_file, input_root)
    member_ext = member_file.suffix.lower()
    if member_ext not in EXTRACTABLE_EXTS:
        return False, "unsupported archive member"

    member_source = f"{archive_rel}!/{member_name}"
    member_source_in_input = f"{archive_rel_in_input}!/{member_name}"
    provenance = {
        **archive_provenance,
        "source_kind": "archive_member",
        "archive_path": archive_rel,
        "archive_relative_to_input": archive_rel_in_input,
        "archive_member_path": member_name,
        "archive_file_name": archive_file.name,
    }
    provenance = public_provenance(provenance)

    blocks = extract_blocks(
        member_file,
        pdf_ocr_mode=pdf_ocr_mode,
        ocr_language=ocr_language,
        ocr_dpi=ocr_dpi,
    )
    for block in blocks:
        block["text"] = clean_extracted_text(block.get("text", ""))
    blocks = [b for b in blocks if normalize_text(b.get("text", ""))]
    chunks = chunk_blocks(blocks, chunk_size=chunk_size, overlap=chunk_overlap)
    used_metadata_fallback = False
    if not chunks:
        fallback_blocks = build_metadata_fallback_blocks(member_file, member_source, member_source_in_input, provenance)
        fallback_chunks = chunk_blocks(fallback_blocks, chunk_size=chunk_size, overlap=0)
        if fallback_chunks:
            blocks = fallback_blocks
            chunks = fallback_chunks
            used_metadata_fallback = True

    out_path = archive_member_output_path(archive_file, input_root, output_root, member_name, layout=layout)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "slug": preserved_source_slug(member_source, out_path),
        "source_file": PurePosixPath(member_name.replace("\\", "/")).name,
        "source_path": member_source,
        "source_relative_to_input": member_source_in_input,
        "source_ext": member_ext,
        "processed_at": datetime.now().isoformat(),
        "num_blocks": len(blocks),
        "total_chars": sum(len(b["text"]) for b in blocks),
        "num_chunks": len(chunks),
        "used_metadata_fallback": used_metadata_fallback,
        "provenance": provenance,
        "blocks": blocks,
        "chunks": chunks,
    }

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, out_path.as_posix()


def save_preprocessed_archive(
    archive_file: Path,
    input_root: Path,
    output_root: Path,
    project_root: Path,
    attachment_index: dict[str, dict],
    chunk_size: int,
    chunk_overlap: int,
    pdf_ocr_mode: str,
    ocr_language: str,
    ocr_dpi: int,
    layout: str = "by_ext",
) -> tuple[bool, str]:
    archive_rel = rel_project_path(archive_file, project_root)
    archive_provenance = public_provenance(attachment_index.get(archive_rel, {}))
    if not archive_provenance.get("source_kind"):
        archive_provenance = {**archive_provenance, "source_kind": "attachment_archive"}

    try:
        zf = zipfile.ZipFile(archive_file)
    except zipfile.BadZipFile:
        return False, "bad zip file"

    ok = 0
    skipped = 0
    failed = 0
    total_size = 0
    member_count = 0
    with zf, tempfile.TemporaryDirectory(prefix="campus_rag_zip_") as temp_dir:
        temp_root = Path(temp_dir)
        for info in zf.infolist():
            if info.is_dir():
                continue
            member_name = decoded_archive_member_name(info).replace("\\", "/")
            if not is_safe_archive_member(member_name):
                skipped += 1
                log.info("[SKIP:zip] %s!/%s (unsafe archive member)", archive_rel, member_name)
                continue
            member_ext = PurePosixPath(member_name).suffix.lower()
            if member_ext not in EXTRACTABLE_EXTS:
                skipped += 1
                continue
            member_count += 1
            if member_count > MAX_ARCHIVE_MEMBERS:
                skipped += 1
                log.warning("[SKIP:zip] %s (archive member limit exceeded: %d)", archive_rel, MAX_ARCHIVE_MEMBERS)
                break
            if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                skipped += 1
                log.warning("[SKIP:zip] %s!/%s (member too large: %d bytes)", archive_rel, member_name, info.file_size)
                continue
            total_size += info.file_size
            if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                skipped += 1
                log.warning("[SKIP:zip] %s (archive total size limit exceeded: %d bytes)", archive_rel, MAX_ARCHIVE_TOTAL_BYTES)
                break

            member_path = PurePosixPath(member_name)
            temp_member = temp_root / Path(*member_path.parts)
            temp_member.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, temp_member.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                saved, info_text = save_preprocessed_archive_member(
                    archive_file=archive_file,
                    member_file=temp_member,
                    member_name=member_name,
                    input_root=input_root,
                    output_root=output_root,
                    project_root=project_root,
                    archive_provenance={**archive_provenance, "archive_member_original_name": info.filename},
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    pdf_ocr_mode=pdf_ocr_mode,
                    ocr_language=ocr_language,
                    ocr_dpi=ocr_dpi,
                    layout=layout,
                )
                if saved:
                    ok += 1
                    log.info("[OK:zip] %s!/%s -> %s", archive_rel, member_name, info_text)
                    # Remove only a verified old result after its corrected replacement exists.
                    original_name = info.filename.replace("\\", "/")
                    if member_name != original_name and is_safe_archive_member(original_name):
                        old_output = archive_member_output_path(archive_file, input_root, output_root, original_name, layout=layout)
                        new_output = Path(info_text)
                        if old_output != new_output and old_output.is_file() and new_output.is_file():
                            try:
                                previous = json.loads(old_output.read_text(encoding="utf-8"))
                                if previous.get("source_path") == f"{archive_rel}!/{original_name}":
                                    old_output.unlink()
                                    log.info("[RENAME:zip] %s -> %s", old_output, new_output)
                                    parent = old_output.parent
                                    boundary = output_root.resolve()
                                    while parent.resolve() != boundary and parent.resolve().is_relative_to(boundary):
                                        try:
                                            parent.rmdir()
                                        except OSError:
                                            break
                                        parent = parent.parent
                            except (OSError, ValueError):
                                log.warning("[RENAME:zip] Could not remove old result: %s", old_output)
                else:
                    skipped += 1
                    log.info("[SKIP:zip] %s!/%s (%s)", archive_rel, member_name, info_text)
            except RuntimeError as exc:
                failed += 1
                message = str(exc)
                if "password required" in message.lower() or "encrypted" in message.lower():
                    log.warning("[FAIL:zip] %s!/%s (encrypted member)", archive_rel, member_name)
                else:
                    log.warning("[FAIL:zip] %s!/%s (%s)", archive_rel, member_name, exc)
            except Exception as exc:
                failed += 1
                log.warning("[FAIL:zip] %s!/%s (%s)", archive_rel, member_name, exc)

    if ok:
        return True, f"archive members processed: ok={ok}, skipped={skipped}, failed={failed}"
    return False, f"no supported archive members processed: skipped={skipped}, failed={failed}"


def iter_target_files(input_root: Path, file_exts: set[str] | None = None) -> list[Path]:
    if not input_root.exists():
        return []
    allowed_exts = file_exts or SUPPORTED_EXTS
    files = []
    for p in input_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in allowed_exts:
            files.append(p)
    return sorted(files)


def is_preprocessed_current(
    input_file: Path,
    input_root: Path,
    output_root: Path,
    project_root: Path,
    attachment_index: dict[str, dict],
    *,
    layout: str,
) -> bool:
    if input_file.suffix.lower() in ARCHIVE_EXTS:
        return False
    try:
        out_path = ensure_output_path(input_file, input_root, output_root, layout=layout)
    except ValueError:
        return False
    rel = rel_project_path(input_file, project_root)
    provenance_mtime = float(attachment_index.get(rel, {}).get("_source_json_mtime") or 0.0)
    newest_input_mtime = max(
        input_file.stat().st_mtime,
        provenance_mtime,
        body_image_input_mtime(input_file, project_root=project_root),
    )
    return out_path.exists() and out_path.stat().st_mtime >= newest_input_mtime


def iter_failed_files_from_log(
    log_path: Path,
    input_root: Path,
    project_root: Path,
    file_exts: set[str] | None = None,
) -> list[Path]:
    if not log_path.exists():
        return []

    statuses: dict[Path, str] = {}
    pattern = re.compile(r"\[(OK|SKIP|FAIL)\]\s+(.+?)(?:\s+->|\s+\(|$)")
    allowed_exts = file_exts or SUPPORTED_EXTS
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.search(line)
        if not match:
            continue
        status, path_text = match.group(1), match.group(2).strip()
        path = Path(path_text)
        if not path.is_absolute():
            path = project_root / path
        path = path.resolve()
        if not path.exists() or path.suffix.lower() not in allowed_exts:
            continue
        try:
            path.relative_to(input_root)
        except ValueError:
            continue
        statuses[path] = status

    return sorted(path for path, status in statuses.items() if status == "FAIL")


def run_batch(
    input_root: Path,
    output_root: Path,
    output_json_root: Path,
    project_root: Path,
    failed_from_log: Path | None,
    dry_run: bool,
    chunk_size: int,
    chunk_overlap: int,
    pdf_ocr_mode: str,
    ocr_language: str,
    ocr_dpi: int,
    layout: str = "by_ext",
    file_exts: set[str] | None = None,
    changed_only: bool = False,
    target_files: list[Path] | None = None,
) -> None:
    input_root = input_root.resolve()
    output_root = output_root.resolve()
    output_json_root = output_json_root.resolve()
    project_root = project_root.resolve()

    attachment_index = load_attachment_index(output_json_root, project_root)
    if target_files is not None:
        allowed_exts = file_exts or SUPPORTED_EXTS
        files = []
        for path in target_files:
            resolved = path.resolve()
            if not resolved.exists() or resolved.suffix.lower() not in allowed_exts:
                continue
            try:
                resolved.relative_to(input_root)
            except ValueError:
                continue
            files.append(resolved)
        files = sorted(dict.fromkeys(files))
    else:
        files = (
            iter_failed_files_from_log(failed_from_log, input_root, project_root, file_exts=file_exts)
            if failed_from_log
            else iter_target_files(input_root, file_exts=file_exts)
        )
    if not files:
        if failed_from_log:
            log.info("No failed files found in %s", failed_from_log)
        else:
            log.info("처리 대상 파일이 없습니다: %s", input_root)
        return

    log.info("처리 대상: %d개", len(files))
    ok = 0
    skipped = 0
    unchanged = 0
    failed = 0

    for position, file_path in enumerate(files, 1):
        rel = rel_project_path(file_path, project_root)
        log.info("[PROGRESS] %d/%d (%.1f%%) ok=%d failed=%d skipped=%d unchanged=%d root=%s next=%s",
                 position - 1, len(files), 100 * (position - 1) / len(files),
                 ok, failed, skipped, unchanged, input_root, file_path.name)
        if changed_only and is_preprocessed_current(
            file_path,
            input_root,
            output_root,
            project_root,
            attachment_index,
            layout=layout,
        ):
            unchanged += 1
            log.info("[SKIP:unchanged] %s", rel)
            continue
        if dry_run:
            log.info("[DRY-RUN] %s", rel)
            continue
        try:
            saved, info = save_preprocessed_file(
                input_file=file_path,
                input_root=input_root,
                output_root=output_root,
                project_root=project_root,
                attachment_index=attachment_index,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                pdf_ocr_mode=pdf_ocr_mode,
                ocr_language=ocr_language,
                ocr_dpi=ocr_dpi,
                layout=layout,
            )
            if saved:
                ok += 1
                log.info("[OK] %s -> %s", rel, info)
            else:
                skipped += 1
                log.info("[SKIP] %s (%s)", rel, info)
        except Exception as exc:
            failed += 1
            log.warning("[FAIL] %s (%s)", rel, exc)

    log.info("[PROGRESS] %d/%d (100%%) ok=%d failed=%d skipped=%d unchanged=%d root=%s",
             len(files), len(files), ok, failed, skipped, unchanged, input_root)
    if dry_run:
        log.info("DRY-RUN 완료")
    else:
        log.info(
            "완료: 성공=%d, 변경없음=%d, 건너뜀=%d, 실패=%d",
            ok,
            unchanged,
            skipped,
            failed,
        )


def main() -> None:
    """Use the dataset-neutral CLI; no department-specific default paths."""
    from scripts.preprocessing.preprocess_files import main as preprocess_main

    preprocess_main()


if __name__ == "__main__":
    main()
