"""Version-aware readers and loss-aware legacy crawler document conversion."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from scripts.crawlers.common.schema import (
    SCHEMA_VERSION, STANDARD_DOCUMENT_TYPES, apply_common_schema, normalize_attachment,
    normalize_content, url_source_id, validate_common_document,
)


LEGACY_FIELDS = {
    "date", "crawled_at", "html_text", "page_content", "html_text_source",
    "attachment_texts", "file_preview_texts",
}
INTEGER_FIELDS = {"notice_no", "year", "media_id", "bbs_id", "board_seq", "post_no", "miss_count"}
RULE_TYPE_MAP = {"hak": "law", "gyu": "regulation", "bylaw": "bylaw", "guideline": "guideline"}
SOURCE_DATASET_ALIASES = {"rule": "pknu_rule"}


def infer_dataset_from_path(path: Path) -> str | None:
    parts = path.resolve().parts
    folded = [part.casefold() for part in parts]
    if "files" in folded:
        index = folded.index("files")
        if index + 1 < len(parts):
            if folded[index + 1] == "preprocessed":
                return parts[index + 2] if index + 2 < len(parts) else None
            if folded[index + 1] == "department":
                from scripts.crawlers.common.storage import department_storage_paths
                tail = parts[index + 2:]
                for dataset, storage_path in department_storage_paths().items():
                    if tail[:len(storage_path)] == storage_path:
                        return dataset
                for marker in ("output", "preprocessed", "vectorized"):
                    if marker in tail and tail.index(marker) > 0:
                        return tail[tail.index(marker) - 1]
                return tail[0] if tail else None
            if parts[index + 1] == "pknu_main":
                tail = parts[index + 2:]
                if "_derived" in tail:
                    stage = tail.index("_derived")
                    if stage + 1 < len(tail):
                        return tail[stage + 1]
                if len(tail) >= 2 and tail[:2] == ("커뮤니티", "공지사항"):
                    return "pknu_notice"
                if tail and tail[0] == "대학생활":
                    return "pknu_student_life"
            return parts[index + 1]
    return None


def absolute_source_site(value: object, document_url: object) -> str:
    for candidate in (value, document_url):
        text = str(candidate or "").strip()
        parts = urlsplit(text)
        if parts.scheme in {"http", "https"} and parts.netloc:
            return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    raise ValueError("legacy document has no absolute source URL")


def _coerce_integers(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {name: _coerce_integers(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_coerce_integers(item) for item in value]
    if key in INTEGER_FIELDS and isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return value


def _legacy_text_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _merge_legacy_attachment_texts(doc: dict[str, Any], attachments: list[dict[str, Any]]) -> None:
    previews = _legacy_text_items(doc.get("file_preview_texts")) or _legacy_text_items(doc.get("attachment_texts"))
    for preview in previews:
        content = str(preview.get("text") or preview.get("content") or "")
        if not content:
            continue
        preview_path = str(preview.get("saved_path") or "")
        preview_name = str(preview.get("name") or "")
        target = next((
            item for item in attachments
            if (preview_path and str(item.get("saved_path") or "") == preview_path)
            or (preview_name and str(item.get("name") or "") == preview_name)
        ), None)
        if target is None:
            continue
        target["text"] = {
            "status": "success", "content": content, "characters": len(content),
            "extractor": "legacy_rule_preprocessing", "error": None,
        }


def _normalize_legacy_attachments(
    doc: dict[str, Any], project_root: Path
) -> list[dict[str, Any]]:
    raw_attachments = doc.get("attachments") if isinstance(doc.get("attachments"), list) else []
    attachments: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_attachments, start=1):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        saved_path = item.get("saved_path")
        if saved_path and Path(str(saved_path)).is_absolute():
            try:
                item["saved_path"] = Path(str(saved_path)).resolve().relative_to(project_root.resolve()).as_posix()
            except ValueError:
                item.setdefault("metadata", {})["legacy_saved_path"] = str(saved_path)
                item["saved_path"] = None
                item["downloaded"] = False
        attachments.append(normalize_attachment(item, index=index, project_root=project_root))
    _merge_legacy_attachment_texts(doc, attachments)
    return attachments


def _legacy_document_type(doc: dict[str, Any], dataset: str) -> str:
    raw_type = str(doc.get("type") or "").strip()
    if dataset in {"rule", "pknu_rule"}:
        if raw_type in RULE_TYPE_MAP:
            return RULE_TYPE_MAP[raw_type]
        if raw_type == "bylaw_guideline":
            return "bylaw" if str(doc.get("bbs_id")) == "1" else "guideline"
    if raw_type in STANDARD_DOCUMENT_TYPES:
        return raw_type
    if dataset == "pknu_student_life":
        return "guide"
    if raw_type in {"static", "page", "intro", "curriculum"}:
        return "static_page"
    if raw_type == "resource":
        return "notice"
    return "notice"


def convert_legacy_document(
    payload: dict[str, Any], *, dataset: str, project_root: Path,
    remove_redundant: bool = False,
) -> dict[str, Any]:
    dataset = SOURCE_DATASET_ALIASES.get(dataset, dataset)
    doc = _coerce_integers(copy.deepcopy(payload))
    source_id = doc.get("source_id") or doc.get("no") or doc.get("slug") or url_source_id(doc.get("url"))
    if str(source_id).startswith(f"{dataset}:"):
        source_id = str(source_id).split(":", 1)[1]
    document_url = doc.get("url") or doc.get("source_page_url")
    source_site = absolute_source_site(doc.get("source_site"), document_url)
    raw_type = doc.get("type")
    metadata = dict(doc.get("metadata") or {})
    metadata.setdefault("legacy_fields", sorted(key for key in LEGACY_FIELDS if key in doc))
    if raw_type and raw_type not in STANDARD_DOCUMENT_TYPES:
        metadata.setdefault("source_type", raw_type)
    content = doc.get("content")
    if content is None:
        content = doc.get("page_content") or doc.get("html_text") or ""
    doc["content"] = str(content)
    doc["attachments"] = _normalize_legacy_attachments(doc, project_root)
    converted = apply_common_schema(
        doc, source_dataset=dataset, source_id=source_id, source_site=source_site,
        document_type=_legacy_document_type(doc, dataset),
        content_source=str(doc.get("content_source") or doc.get("html_text_source") or "legacy_document"),
        published_at=doc.get("published_at") or doc.get("date"),
        updated_at=doc.get("updated_at"), effective_at=doc.get("effective_at"),
        author=doc.get("author"), metadata=metadata,
        crawled_at=doc.get("crawled_at") or (doc.get("crawl") or {}).get("crawled_at"),
    )
    if remove_redundant:
        remove_redundant_legacy_fields(converted)
    errors = validate_common_document(converted, project_root)
    if errors:
        raise ValueError("converted document is invalid: " + "; ".join(errors))
    return converted


def remove_redundant_legacy_fields(doc: dict[str, Any]) -> list[str]:
    removed: list[str] = []
    if "published_at" in doc and "date" in doc:
        doc.pop("date", None)
        removed.append("date")
    if (doc.get("crawl") or {}).get("crawled_at") and "crawled_at" in doc:
        doc.pop("crawled_at", None)
        removed.append("crawled_at")
    normalized_content = normalize_content(doc.get("content"))
    for key in ("html_text", "page_content"):
        legacy_text = normalize_content(doc.get(key))
        if key in doc and (not legacy_text or legacy_text in normalized_content):
            doc.pop(key, None)
            removed.append(key)
    if "html_text_source" in doc and doc.get("content_source"):
        doc.pop("html_text_source", None)
        removed.append("html_text_source")
    for key in ("attachment_texts", "file_preview_texts"):
        items = _legacy_text_items(doc.get(key))
        if key in doc and all(
            not normalize_content(item.get("text") or item.get("content"))
            or normalize_content(item.get("text") or item.get("content")) in normalized_content
            for item in items
        ):
            doc.pop(key, None)
            removed.append(key)
    for attachment in doc.get("attachments") or []:
        if isinstance(attachment, dict) and "final_url" in attachment and "downloaded_from_url" in attachment:
            attachment.pop("downloaded_from_url", None)
    return removed


def read_document(
    payload: dict[str, Any], *, dataset: str | None = None,
    project_root: Path, remove_redundant: bool = False,
) -> dict[str, Any]:
    version = payload.get("schema_version")
    resolved_dataset = str(payload.get("source_dataset") or dataset or "").strip()
    if not resolved_dataset:
        raise ValueError("dataset is required for legacy documents")
    if version not in {None, "", SCHEMA_VERSION}:
        raise ValueError(f"unsupported crawler schema_version: {version}")
    if version == SCHEMA_VERSION:
        doc = _coerce_integers(copy.deepcopy(payload))
        if remove_redundant:
            remove_redundant_legacy_fields(doc)
        errors = validate_common_document(doc, project_root)
        if errors:
            raise ValueError("invalid schema 1.0 document: " + "; ".join(errors))
        return doc
    return convert_legacy_document(
        payload, dataset=resolved_dataset, project_root=project_root,
        remove_redundant=remove_redundant,
    )
