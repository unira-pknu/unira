"""Fill CE document priority scores from CE/rule content overlap."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:
    dict_row = None

    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value: Any) -> None:
            self.value = value

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ce.priority import (  # noqa: E402
    build_reference_feature_set,
    build_rule_feature_set,
    calculate_ce_priority,
)
from scripts.db import connect_postgres  # noqa: E402
from scripts.vectorization.datasets import get_vector_paths

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "update_ce_priorities.log"
DEFAULT_CE_INDEX = get_vector_paths(PROJECT_ROOT, "ce")["output_root"] / "index.jsonl"
DEFAULT_RULE_INDEX = get_vector_paths(PROJECT_ROOT, "rule")["output_root"] / "index.jsonl"
DEFAULT_MAIN_INDEXES = (
    get_vector_paths(PROJECT_ROOT, "pknu_main")["output_root"] / "index.jsonl",
    get_vector_paths(PROJECT_ROOT, "pknu_notice")["output_root"] / "index.jsonl",
    get_vector_paths(PROJECT_ROOT, "pknu_student_life")["output_root"] / "index.jsonl",
)

log = logging.getLogger(__name__)


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def connect() -> Any:
    if dict_row is None:
        raise RuntimeError("psycopg is required for database updates. Install backend requirements first.")
    return connect_postgres(row_factory=dict_row, prepare_threshold=None)


def iter_index_records(index_path: Path) -> Iterable[dict[str, Any]]:
    with index_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}: {exc}") from exc


def aggregate_source_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for record in records:
        metadata = record.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        source_slug = str(record.get("source_slug") or metadata.get("source_slug") or "")
        if not source_slug:
            continue
        source = sources.setdefault(
            source_slug,
            {
                "id": source_slug,
                "title": metadata.get("doc_title") or metadata.get("source_file") or source_slug,
                "metadata": metadata,
                "content_parts": [],
            },
        )
        source["content_parts"].append(str(record.get("text") or record.get("content") or ""))
    return [
        {
            "id": source["id"],
            "title": source["title"],
            "metadata": source["metadata"],
            "content": "\n\n".join(source["content_parts"]),
        }
        for source in sources.values()
    ]


def fetch_rule_contents(conn: Any) -> list[str]:
    query = """
        select c.content
        from public.rule_chunks as c
        join public.rule_sources as s on s.id = c.source_id
        where s.status = 'active'
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return [str(row["content"] or "") for row in cur.fetchall()]


def fetch_main_contents(conn: Any) -> list[str]:
    query = """
        select c.content
        from public.pknu_notice_chunks as c
        join public.pknu_notice_sources as s on s.id = c.source_id
        where s.status = 'active'
        union all
        select c.content
        from public.pknu_main_chunks as c
        join public.pknu_main_sources as s on s.id = c.source_id
        where s.status = 'active'
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return [str(row["content"] or "") for row in cur.fetchall()]


def fetch_ce_documents(conn: Any) -> list[dict[str, Any]]:
    query = """
        select
            s.id,
            s.title,
            s.metadata,
            coalesce(string_agg(c.content, E'\n\n' order by c.chunk_index), '') as content
        from public.rag_sources as s
        left join public.rag_chunks as c on c.source_id = s.id
        where s.status = 'active'
        group by s.id
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return list(cur.fetchall())


def build_updates(
    ce_documents: Iterable[dict[str, Any]],
    rule_features: set[str],
    main_features: set[str],
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    for doc in ce_documents:
        metadata = doc.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        score, details = calculate_ce_priority(
            str(doc["content"] or ""),
            rule_features,
            main_features=main_features,
            metadata=metadata,
            title=str(doc.get("title") or ""),
        )
        updates.append(
            {
                "id": doc["id"],
                "title": doc.get("title") or "",
                "priority_score": score,
                "priority_details": Jsonb(details),
            }
        )
    return updates


def log_preview(updates: list[dict[str, Any]], limit: int) -> None:
    preview = sorted(updates, key=lambda row: row["priority_score"], reverse=True)[:limit]
    for row in preview:
        log.info("[DRY-RUN] %.4f %s %s", row["priority_score"], row["id"], row.get("title") or "")


def update_ce_priorities(conn: Any, dry_run: bool, preview_limit: int) -> int:
    rule_contents = fetch_rule_contents(conn)
    rule_features = build_rule_feature_set(rule_contents)
    main_contents = fetch_main_contents(conn)
    main_features = build_reference_feature_set(main_contents)
    ce_documents = fetch_ce_documents(conn)
    log.info(
        "loaded rule_chunks=%d rule_features=%d main_chunks=%d main_features=%d ce_sources=%d",
        len(rule_contents),
        len(rule_features),
        len(main_contents),
        len(main_features),
        len(ce_documents),
    )

    updates = build_updates(ce_documents, rule_features, main_features)

    if dry_run:
        log_preview(updates, preview_limit)
        return len(updates)

    update_sql = """
        update public.rag_sources
        set
            priority_score = %(priority_score)s,
            priority_details = %(priority_details)s,
            priority_updated_at = now()
        where id = %(id)s
    """
    with conn.cursor() as cur:
        cur.executemany(update_sql, updates)
    conn.commit()
    return len(updates)


def preview_from_index(
    ce_index: Path,
    rule_index: Path,
    main_indexes: Iterable[Path],
    preview_limit: int,
) -> int:
    ce_documents = aggregate_source_records(iter_index_records(ce_index))
    rule_documents = aggregate_source_records(iter_index_records(rule_index))
    main_documents: list[dict[str, Any]] = []
    for index_path in main_indexes:
        if index_path.exists():
            main_documents.extend(aggregate_source_records(iter_index_records(index_path)))

    rule_features = build_rule_feature_set(str(doc.get("content") or "") for doc in rule_documents)
    main_features = build_reference_feature_set(str(doc.get("content") or "") for doc in main_documents)
    updates = build_updates(ce_documents, rule_features, main_features)
    log.info(
        "loaded local ce_sources=%d rule_sources=%d main_sources=%d rule_features=%d main_features=%d",
        len(ce_documents),
        len(rule_documents),
        len(main_documents),
        len(rule_features),
        len(main_features),
    )
    log_preview(updates, preview_limit)
    return len(updates)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Update CE document priority scores.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--from-index",
        action="store_true",
        help="Preview from local vectorized index files. Requires --dry-run.",
    )
    parser.add_argument("--index-path", type=Path, default=DEFAULT_CE_INDEX)
    parser.add_argument("--rule-index-path", type=Path, default=DEFAULT_RULE_INDEX)
    parser.add_argument("--preview-limit", type=int, default=10)
    args = parser.parse_args()

    if args.preview_limit <= 0:
        raise ValueError("--preview-limit must be positive")
    if args.from_index:
        if not args.dry_run:
            raise ValueError("--from-index is preview-only; pass --dry-run")
        count = preview_from_index(args.index_path, args.rule_index_path, DEFAULT_MAIN_INDEXES, args.preview_limit)
    else:
        with connect() as conn:
            count = update_ce_priorities(conn, args.dry_run, args.preview_limit)
    log.info("done: processed %d CE source priorities", count)


if __name__ == "__main__":
    main()
