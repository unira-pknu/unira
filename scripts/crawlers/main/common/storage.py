'Main website common storage.'

from __future__ import annotations
from pathlib import Path
from scripts.crawlers.common.logging import configure_crawler_logging, log_event
from scripts.crawlers.common.storage import (
    archive_document,
    get_dataset_paths,
    load_state_with_migration,
    save_state_atomic,
    write_document,
)
from typing import Any
import json
import logging


PROJECT_ROOT = Path(__file__).resolve().parents[4]


log, log_context = configure_crawler_logging("pknu_student_life", PROJECT_ROOT)


BASE_URL = "https://www.pknu.ac.kr"


GUIDE_URL = f"{BASE_URL}/main/434"


EBOOK_INDEX_URL = f"{BASE_URL}/ebook/col_life/kor/index.html"


EBOOK_BASE_URL = f"{BASE_URL}/ebook/col_life/kor/"


PATHS = get_dataset_paths(PROJECT_ROOT, "pknu_student_life")


OUTPUT_JSON = PATHS.json


OUTPUT_HTML = PATHS.html


OUTPUT_FILES = PATHS.files


OUTPUT_DELETED = PATHS.deleted


STATE_FILE = PATHS.state


LEGACY_STATE_FILES = (PROJECT_ROOT / "state_pknu_student_life.json",)


SUBCATEGORY_ACADEMIC_INFO = "학사정보"


def load_state() -> dict[str, Any]:
    state, origin = load_state_with_migration(PATHS, legacy_paths=LEGACY_STATE_FILES)
    log_event(log, logging.INFO, "state_loaded", path=STATE_FILE, entries=len(state["items"]), origin=origin)
    return state


def save_state(state: dict[str, Any]) -> None:
    save_state_atomic(STATE_FILE, state, "pknu_student_life")
    log_event(log, logging.INFO, "state_saved", path=STATE_FILE, entries=len(state.get("items", {})))


def save_json(doc: dict[str, Any], subcategory: str, slug: str, raw_html: str | None = None) -> Path:
    path, _ = write_document(PATHS, doc, subcategory, slug, raw_html)
    return path


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


SUBCATEGORY_STATIC_PAGE = "학사안내_페이지"


def retire_legacy_static_route(state: dict[str, Any], page_id: int) -> int:
    """Remove only a misrouted static state entry, archiving its old document if present."""
    url = f"{BASE_URL}/main/{page_id}"
    items = state.setdefault("items", {})
    retired = 0
    for key, record in list(items.items()):
        if (not isinstance(record, dict) or record.get("url") != url
                or key == f"file:{page_id}" or record.get("parser_version", 0) >= 2):
            continue
        slug = record.get("slug") or key
        if PATHS.document_json(SUBCATEGORY_STATIC_PAGE, slug).is_file():
            archive_document(PATHS, SUBCATEGORY_STATIC_PAGE, slug)
        del items[key]
        retired += 1
    return retired
