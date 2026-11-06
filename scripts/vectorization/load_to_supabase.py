"""Load vectorized RAG chunks into Supabase PostgreSQL with pgvector.

Expected input:
    files/vectorized/ce/index.jsonl

Required environment:
    DATABASE_URL, SUPABASE_POOLER_URL, SUPABASE_DATABASE_URL, or SUPABASE_DB_URL

Alternative PG environment variables are also supported:
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.db import connect_postgres  # noqa: E402
from scripts.preprocessing.text_cleaning import compact_metadata, has_searchable_body
from scripts.vectorization.common import EXPECTED_DIMENSIONS, DEFAULT_SENTENCE_MODEL, validate_vectors
from scripts.vectorization.datasets import (TABLE_FAMILIES, database_tables, dataset_paths,
                                           dataset_families, department_attributes, table_family)
DATASET_TABLES = database_tables(PROJECT_ROOT)
DEFAULT_DATASET = "ce"
DEFAULT_EMBEDDING_MODEL = f"sentence-transformers:{DEFAULT_SENTENCE_MODEL}"
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "load_to_supabase.log"

log = logging.getLogger(__name__)


class NoSearchContent(ValueError):
    """A record contains no body text or only extraction-fallback metadata."""


@lru_cache(maxsize=8192)
def _cached_body_status(path: str, size: int, mtime_ns: int) -> bool:
    return has_searchable_body(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def has_record_body(record: dict) -> bool:
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Record metadata must be an object")
    if not str(record.get("text") or record.get("content") or "").strip():
        return False
    if metadata.get("used_metadata_fallback") is True or metadata.get("metadata_only") is True:
        return False
    # Older vector files did not carry the extraction fallback flag.
    declared = metadata.get("preprocessed_path")
    if declared:
        path = Path(declared)
        path = (path if path.is_absolute() else PROJECT_ROOT / path).resolve()
        if not path.is_relative_to((PROJECT_ROOT / "files/preprocessed").resolve()):
            raise ValueError("Preprocessed provenance path is outside files/preprocessed")
        stat = path.stat()
        return _cached_body_status(str(path), stat.st_size, stat.st_mtime_ns)
    return True


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


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


def connect() -> psycopg.Connection:
    return connect_postgres(prepare_threshold=None)


def validate_table_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid table name: {value}")
    return value


def resolve_tables(
    dataset: str,
    sources_table: str | None,
    chunks_table: str | None,
) -> tuple[str, str]:
    defaults = DATASET_TABLES[dataset]
    return (
        validate_table_name(sources_table or defaults["sources"]),
        validate_table_name(chunks_table or defaults["chunks"]),
    )


def resolve_index_path(dataset: str, index_path: Path | None) -> Path:
    if index_path is not None:
        return index_path
    return DATASET_TABLES[dataset]["index"]


def ensure_schema_ready(conn: psycopg.Connection, sources_table: str, chunks_table: str) -> None:
    log.info("Checking Supabase schema")
    sql = """
        select
            to_regclass(%s) is not null as has_sources,
            to_regclass(%s) is not null as has_chunks,
            current_setting('transaction_read_only') = 'on' as is_read_only,
            pg_is_in_recovery() as is_replica
    """
    with conn.cursor() as cur:
        cur.execute(sql, (f"public.{sources_table}", f"public.{chunks_table}"))
        has_sources, has_chunks, is_read_only, is_replica = cur.fetchone()

    if is_read_only or is_replica:
        raise psycopg.errors.ReadOnlySqlTransaction(
            "Ingestion requires a writable primary connection. Check Supabase disk usage "
            "and connection settings before resuming; read-only protection was not overridden."
        )

    missing = []
    if not has_sources:
        missing.append(f"public.{sources_table}")
    if not has_chunks:
        missing.append(f"public.{chunks_table}")
    if missing:
        raise RuntimeError(
            "Supabase schema is not ready. Missing tables: "
            f"{', '.join(missing)}. Apply the matching migration under supabase/migrations "
            "before running scripts/vectorization/load_to_supabase.py."
        )


def iter_records(index_path: Path) -> Iterable[dict[str, Any]]:
    with index_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}: {exc}") from exc


def load_embedding_model(index_path: Path) -> str:
    manifest_path = index_path.with_name("manifest.json")
    if not manifest_path.exists():
        return DEFAULT_EMBEDDING_MODEL

    with manifest_path.open("r", encoding="utf-8-sig") as handle:
        manifest = json.load(handle)
    return str(manifest.get("embedding_backend") or DEFAULT_EMBEDDING_MODEL)


def preflight_index(index_path: Path, dataset: str) -> str:
    """Validate the complete index before opening a database connection."""
    manifest_path = index_path.with_name("manifest.json")
    if not manifest_path.is_file():
        raise ValueError("Completed manifest.json is required before ingestion")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if index_path.stat().st_mtime_ns > manifest_path.stat().st_mtime_ns:
        raise ValueError("Index is newer than its completed manifest")
    if manifest.get("dataset") != dataset:
        raise ValueError("Manifest dataset does not match ingestion dataset")
    if manifest.get("num_files_failed") != 0 or manifest.get("failures"):
        raise ValueError("Resolve vectorization failures before ingestion")
    if manifest.get("embedding_dimensions") != EXPECTED_DIMENSIONS:
        raise ValueError("Manifest embedding dimensions do not match the database")
    if not manifest.get("vectorized_at"):
        raise ValueError("Manifest has no completion timestamp")
    declared = manifest.get("index_path")
    if not declared:
        raise ValueError("Manifest index_path is required")
    declared_path = Path(declared)
    if not declared_path.is_absolute():
        declared_path = PROJECT_ROOT / declared_path
    if declared_path.resolve() != index_path.resolve():
        raise ValueError("Manifest index_path does not match selected index")
    model = manifest.get("embedding_backend")
    if model != DEFAULT_EMBEDDING_MODEL:
        raise ValueError("Manifest model differs from the configured search embedding model")
    signature = (index_path.stat().st_size, index_path.stat().st_mtime_ns)
    count = 0
    excluded = 0
    keys: set[tuple[str, int]] = set()
    for record in iter_records(index_path):
        count += 1
        metadata = record.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("Index metadata must be an object")
        table_family(dataset, metadata)
        source_id, _ = record_identity(record, dataset)
        key = (source_id, int(record["chunk_index"]))
        if key in keys:
            raise ValueError("Duplicate source/chunk position in index")
        keys.add(key)
        if not has_record_body(record):
            excluded += 1
            continue
        row = prepare_row(record, model, dataset)
    if count == 0 or count != manifest.get("num_chunks"):
        raise ValueError("Index record count does not match nonempty completed manifest")
    if signature != (index_path.stat().st_size, index_path.stat().st_mtime_ns):
        raise ValueError("Index changed during preflight")
    log.info("[%s] preflight records=%d excluded_empty_or_fallback=%d", dataset, count, excluded)
    return model


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def record_identity(record: dict, dataset: str) -> tuple[str, str]:
    original = record.get("source_slug")
    if not isinstance(original, str) or not original.strip():
        raise ValueError("Nonempty source_slug is required")
    source_id = "src_" + sha256_text(json.dumps([dataset, original], ensure_ascii=False))
    chunk_id = "chunk_" + sha256_text(json.dumps([source_id, int(record["chunk_index"])]))
    return source_id, chunk_id


def remove_nul_bytes(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [remove_nul_bytes(item) for item in value]
    if isinstance(value, dict):
        return {
            remove_nul_bytes(key): remove_nul_bytes(item)
            for key, item in value.items()
        }
    return value


def infer_source_type(metadata: dict[str, Any]) -> str:
    source_ext = str(metadata.get("source_ext") or "").lower().lstrip(".")
    if source_ext:
        return source_ext
    if metadata.get("attachment_url"):
        return "attachment"
    if metadata.get("doc_url") or metadata.get("source_page_url"):
        return "web"
    return "unknown"


def normalize_vector_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Prefer schema 1.0 metadata while accepting transition-period aliases."""
    normalized = dict(metadata)
    normalized["published_at"] = normalized.get("published_at") or normalized.get("date")
    crawl = normalized.get("crawl") if isinstance(normalized.get("crawl"), dict) else {}
    if not crawl and normalized.get("crawled_at"):
        crawl = {"crawled_at": normalized.get("crawled_at")}
    normalized["crawl"] = crawl
    normalized.pop("date", None)
    normalized.pop("crawled_at", None)
    for key in ("num_chars", "indexed_num_chars", "num_lines", "year", "notice_no"):
        value = normalized.get(key)
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            normalized[key] = int(value)
    attachments = normalized.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            for key in ("size_bytes",):
                value = attachment.get(key)
                if isinstance(value, str) and value.isdigit():
                    attachment[key] = int(value)
    return compact_metadata(normalized)


def prepare_row(record: dict[str, Any], embedding_model: str, dataset: str | None = None) -> dict[str, dict[str, Any]]:
    if not embedding_model.startswith("sentence-transformers:"):
        raise ValueError("DB ingestion requires semantic embeddings; hash vectors are for local tests only")
    if record.get("embedding_model", embedding_model) != embedding_model:
        raise ValueError("Record embedding model differs from the index manifest")
    if not has_record_body(record):
        raise NoSearchContent("Empty body or metadata-only extraction fallback")
    embedding = record.get("embedding")
    if not isinstance(embedding, list):
        raise ValueError(f"Record {record.get('id')} has no embedding list")
    if len(embedding) != EXPECTED_DIMENSIONS:
        raise ValueError(
            f"Record {record.get('id')} has embedding dimension {len(embedding)}, "
            f"expected {EXPECTED_DIMENSIONS}. Re-run: python scripts/vectorization/vectorization.py --backend sentence-transformers"
        )
    validate_vectors([embedding], 1, EXPECTED_DIMENSIONS)

    content = remove_nul_bytes(record.get("text") or record.get("content") or "")
    if not content:
        raise ValueError(f"Record {record.get('id')} has empty text/content")

    metadata = remove_nul_bytes(record.get("metadata") or {})
    if not isinstance(metadata, dict):
        raise ValueError(f"Record {record.get('id')} has non-object metadata")
    metadata = normalize_vector_metadata(metadata)

    dataset = dataset or metadata.get("source_dataset")
    if not isinstance(dataset, str) or not dataset.strip():
        raise ValueError("Ingestion dataset is required for source identity")
    original_slug = record.get("source_slug")
    if not isinstance(original_slug, str) or not original_slug.strip():
        raise ValueError("Nonempty source_slug is required")
    metadata["ingestion_dataset"] = dataset
    metadata.setdefault("source_dataset", dataset)
    metadata["original_source_slug"] = original_slug
    family = table_family(dataset, metadata)
    attributes = department_attributes(dataset) if family == "rag" else {}
    metadata.update(compact_metadata(attributes))
    if family == "pknu_syllabus":
        metadata["category"] = metadata.get("category") or "강의계획서"
    # Namespace both UNIQUE source_slug and source IDs across shared tables.
    source_slug, chunk_key = record_identity(record, dataset)
    now = datetime.now(timezone.utc)
    source_metadata = {key: value for key, value in metadata.items()
                       if key not in {"num_chars", "indexed_num_chars", "num_lines",
                                      "quality_flags", "quality_context_prefix", "structured_table"}}

    return {
        "source": {
            "id": source_slug,
            "source_slug": source_slug,
            "source_type": infer_source_type(metadata),
            "title": metadata.get("doc_title") or metadata.get("source_file") or None,
            "url": metadata.get("doc_url") or metadata.get("attachment_url") or None,
            "parent_url": metadata.get("source_page_url") or None,
            "file_path": metadata.get("source_path") or None,
            "file_ext": metadata.get("source_ext") or None,
            "category": metadata.get("category") or None,
            "subcategory": metadata.get("subcategory") or None,
            "metadata": Jsonb(source_metadata),
            "processed_at": now,
            **attributes,
        },
        "chunk": {
            "id": chunk_key,
            "source_id": source_slug,
            "chunk_id": int(record["chunk_id"]),
            "chunk_index": int(record["chunk_index"]),
            "content": str(content),
            "content_hash": sha256_text(str(content)),
            "token_count": len(str(content).split()),
            "metadata": Jsonb(metadata),
            "embedding": vector_literal(embedding),
            "embedding_model": embedding_model,
            "embedding_dim": EXPECTED_DIMENSIONS,
            "embedded_at": now,
        },
    }


SOURCE_COLUMNS = (
    "id", "source_slug", "source_type", "title", "url", "parent_url", "file_path",
    "file_ext", "category", "subcategory", "metadata", "processed_at",
)
DEPARTMENT_COLUMNS = ("department_id", "department_name", "college_name",
                      "department_homepage", "department_majors")
CHUNK_COLUMNS = (
    "id", "source_id", "chunk_id", "chunk_index", "content", "content_hash",
    "token_count", "metadata", "embedding", "embedding_model", "embedding_dim", "embedded_at",
)


def copy_upsert(cur, table: str, stage: str, columns: tuple[str, ...],
                rows: list[dict], conflict: tuple[str, ...], timestamp: str) -> int:
    """COPY bounded batches to connection-local staging, then perform one upsert."""
    target = sql.Identifier("public", validate_table_name(table))
    temporary = sql.Identifier("pg_temp", stage)
    names = sql.SQL(", ").join(map(sql.Identifier, columns))
    cur.execute(sql.SQL("""
        create temporary table if not exists {} on commit delete rows
        as select {} from {} with no data
    """).format(sql.Identifier(stage), names, target))
    cur.execute(sql.SQL("truncate {}").format(temporary))
    with cur.copy(sql.SQL("copy {} ({}) from stdin").format(temporary, names)) as copy:
        for row in rows:
            copy.write_row(tuple(row[column] for column in columns))
    compared = [column for column in columns if column != timestamp]
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = excluded.{}").format(sql.Identifier(column), sql.Identifier(column))
        for column in columns if column not in conflict
    )
    current_values = sql.SQL(", ").join(sql.Identifier("current_row", column) for column in compared)
    incoming_values = sql.SQL(", ").join(sql.Identifier("excluded", column) for column in compared)
    keys = sql.SQL(", ").join(map(sql.Identifier, conflict))
    cur.execute(sql.SQL("""
        insert into {} as current_row ({})
        select {} from {} where true order by {}
        on conflict ({}) do update set {}
        where ({}) is distinct from ({})
    """).format(target, names, names, temporary, keys, keys, assignments,
                 current_values, incoming_values))
    return cur.rowcount


def flush_batch(
    conn: psycopg.Connection,
    rows: list[dict[str, dict[str, Any]]],
    sources_table: str,
    chunks_table: str,
    *,
    replace_sources: bool = False,
    replaced_source_ids: set[str] | None = None,
) -> None:
    if not rows:
        return
    sources = {row["source"]["id"]: row["source"] for row in rows}
    chunks = [row["chunk"] for row in rows]
    replacement_ids = []
    with conn.cursor() as cur:
        source_columns = SOURCE_COLUMNS + DEPARTMENT_COLUMNS if sources_table == "rag_sources" else SOURCE_COLUMNS
        source_changes = copy_upsert(cur, sources_table, "rag_ingest_sources", source_columns,
                                     list(sources.values()), ("id",), "processed_at")
        if replace_sources:
            replaced_source_ids = replaced_source_ids if replaced_source_ids is not None else set()
            replacement_ids = [source for source in sources if source not in replaced_source_ids]
            if replacement_ids:
                cur.execute(sql.SQL("delete from {} where source_id = any(%s)").format(
                    sql.Identifier("public", chunks_table)), (replacement_ids,))
        chunk_changes = copy_upsert(cur, chunks_table, "rag_ingest_chunks", CHUNK_COLUMNS,
                                    chunks, ("source_id", "chunk_index"), "embedded_at")
    if replace_sources and replaced_source_ids is not None:
        replaced_source_ids.update(replacement_ids)
    log.info("COPY batch records=%d changed_sources=%d changed_chunks=%d",
             len(rows), source_changes, chunk_changes)


def delete_excluded_chunks(conn, chunks_table: str, chunk_ids: list[str]) -> int:
    """Remove only stable IDs proven empty/fallback in this dataset's current input."""
    if not chunk_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(sql.SQL("delete from {} where id = any(%s)").format(
            sql.Identifier("public", validate_table_name(chunks_table))), (chunk_ids,))
        removed = cur.rowcount
    log.info("Excluded body cleanup: removed_chunks=%d", removed)
    return removed


def is_halfvec_hnsw_index(method, expression, opclass, options, predicate=None) -> bool:
    """Match the index expression and the unchanged HNSW construction settings."""
    expression = re.sub(r'[\s"()]', '', expression or '')
    settings = dict(option.split('=', 1) for option in (options or []))
    return (
        method == "hnsw"
        and predicate is None
        and opclass == "halfvec_cosine_ops"
        and expression in {
            f"embedding::halfvec{EXPECTED_DIMENSIONS}",
            f"embedding::extensions.halfvec{EXPECTED_DIMENSIONS}",
        }
        and settings.get("m", str(HNSW_M)) == str(HNSW_M)
        and settings.get("ef_construction", str(HNSW_EF_CONSTRUCTION)) == str(HNSW_EF_CONSTRUCTION)
    )


def manage_search_indexes(conn, sources_table: str, chunks_table: str, *,
                          create: bool, timeout_seconds: int = 1800) -> None:
    """Explicit maintenance phase; never remove constraints or uniqueness indexes."""
    if timeout_seconds < 0:
        raise ValueError("Index timeout must be nonnegative (0 disables this statement timeout)")
    specs = [
        (sources_table, sources_table + "_metadata_gin_idx", "gin", "metadata"),
        (chunks_table, chunks_table + "_metadata_gin_idx", "gin", "metadata"),
        (chunks_table, chunks_table + "_tsv_idx", "gin", "content_tsv"),
        (chunks_table, chunks_table + "_embedding_hnsw_idx", "hnsw",
         f"(embedding::extensions.halfvec({EXPECTED_DIMENSIONS})) extensions.halfvec_cosine_ops"),
    ]
    for table, name, method, expression in specs:
        validate_table_name(table)
        with conn.cursor() as cur:
            cur.execute("set local lock_timeout = '10s'")
            cur.execute("SET LOCAL maintenance_work_mem = '512MB'")
            cur.execute("select set_config('statement_timeout', %s, true)",
                        (str(timeout_seconds * 1000),))
            cur.execute("""
                select t.relname, i.indisunique, i.indisvalid,
                       a.amname, pg_catalog.pg_get_expr(i.indexprs, i.indrelid),
                       o.opcname, x.reloptions, i.indpred
                from pg_catalog.pg_index i
                join pg_catalog.pg_class x on x.oid=i.indexrelid
                join pg_catalog.pg_class t on t.oid=i.indrelid
                join pg_catalog.pg_namespace n on n.oid=x.relnamespace
                join pg_catalog.pg_am a on a.oid=x.relam
                left join pg_catalog.pg_opclass o on o.oid=i.indclass[0]
                where n.nspname='public' and x.relname=%s
            """, (name,))
            existing = cur.fetchone()
            if existing and (existing[0] != table or existing[1]):
                raise ValueError(f"Refusing to alter unexpected or unique index: {name}")
            if create:
                if existing and not existing[2]:
                    raise ValueError(f"Invalid index requires inspection before rebuild: {name}")
                if existing and method == "hnsw" and not is_halfvec_hnsw_index(*existing[3:]):
                    raise ValueError(
                        f"Existing index {name} does not match halfvec({EXPECTED_DIMENSIONS}) "
                        f"HNSW (m={HNSW_M}, ef_construction={HNSW_EF_CONSTRUCTION}). "
                        "Stop active index builds and explicitly remove this index during "
                        "maintenance before running --finalize-indexes again."
                    )
                options = (sql.SQL(" with (m = {}, ef_construction = {})").format(
                    sql.Literal(HNSW_M), sql.Literal(HNSW_EF_CONSTRUCTION))
                    if method == "hnsw" else sql.SQL(""))
                log.info("[INDEX-START] %s method=%s", name, method)
                cur.execute(sql.SQL("create index if not exists {} on {} using {} ({}){}").format(
                    sql.Identifier(name), sql.Identifier("public", table),
                    sql.SQL(method), sql.SQL(expression), options))
            elif existing:
                cur.execute(sql.SQL("drop index {} restrict").format(sql.Identifier("public", name)))
        conn.commit()
        log.info("[INDEX-%s] %s", "READY" if create else "DEFERRED", name)
    if create:
        with conn.cursor() as cur:
            cur.execute("select set_config('statement_timeout', %s, true)",
                        (str(timeout_seconds * 1000),))
            for table in (sources_table, chunks_table):
                cur.execute(sql.SQL("analyze {}").format(sql.Identifier("public", table)))
        conn.commit()


def deduplicate_sources(
    conn: psycopg.Connection,
    sources_table: str,
    chunks_table: str,
    dataset: str,
) -> int:
    """Keep at most one active source row per (url, title, attachment name),
    deleting the rest.

    Root cause this guards against: each crawler generates a source's `id`
    (slug) independently, and for several crawlers that slug is not fully
    stable across re-crawls of the *same logical document* (e.g. a PDF
    attachment whose upload path embeds a fresh random UUID each time the
    site re-serves it). Since load()'s upsert is keyed on `id`
    (`on conflict (id)`), an unstable slug means every re-crawl inserts a
    brand new source row instead of updating the existing one, and inactive
    duplicates silently pile up (18,477 rows found in production on
    2026-09-15 across all four datasets before a one-off cleanup).

    Grouping is (url, title) rather than url alone: some landing pages
    (e.g. www.pknu.ac.kr/main/434) legitimately host several differently
    titled documents at the same URL, so deduping on url alone would wrongly
    collapse genuinely different documents together (this actually happened
    during the manual 2026-09-15 cleanup and had to be corrected).

    The attachment name (metadata.attachment_name, '' for the notice body
    itself) is part of the key too: a notice's body and each of its
    attachments all share the notice's url and title, so (url, title) alone
    kept only the biggest of them and deleted the rest every run. Found
    2026-10-14: 2,963 of 6,635 pknu_notice and 1,622 of 3,201 ce notices had
    lost their body text (no notice kept both a body and an attachment) --
    e.g. "2026-2학기 수강신청 변경기간 안내" kept only a menu-guide PDF and
    lost the body with the actual dates. The attachment *name* is what the
    site shows, so it stays stable when the upload path (and so the slug)
    changes between crawls -- the duplication this function exists for.

    "Best" within a (url, title) group is the row with the most chunks
    (a proxy for the most complete crawl/extraction), tie-broken by id for
    determinism. This runs after every load() call so the invariant holds
    continuously rather than needing another one-off cleanup later.

    The losers are hard-deleted (their chunks go with them via
    `on delete cascade`). They used to be kept as status='inactive', which on
    2026-09-30 had grown to 31-76% of every chunk table: dead rows the
    lexical/vector RPCs still have to read past (bigger tables -> more disk
    reads -> the 8s statement timeouts), and no one ever read the history.
    Rows that were already inactive before this change are not touched here;
    they are removed by the separate one-off cleanup script
    (supabase/migrations/014_delete_inactive_sources.sql).
    """
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                with candidates as materialized (
                    select s.id, s.url, s.title,
                           coalesce(s.metadata->>'attachment_name', '') as attachment_name,
                           s.metadata->>'source_path' as source_path,
                           coalesce(s.metadata->>'archive_member_path', '') as archive_member_path,
                           count(*) over (
                               partition by s.url, s.title,
                                            coalesce(s.metadata->>'attachment_name', ''),
                                            s.metadata->>'source_path',
                                            coalesce(s.metadata->>'archive_member_path', '')
                           ) as duplicate_count
                    from {sources_table} s
                    where s.status = 'active' and s.url is not null and s.url <> ''
                      and s.metadata->>'ingestion_dataset' = %s
                      and nullif(s.metadata->>'source_path', '') is not null
                ),
                duplicates as materialized (
                    select * from candidates where duplicate_count > 1
                ),
                ranked as (
                    select d.id,
                           row_number() over (
                               partition by d.url, d.title, d.attachment_name,
                                            d.source_path, d.archive_member_path
                               order by cc.chunk_count desc, d.id desc
                           ) as rn
                    from duplicates d
                    cross join lateral (
                        select count(*) as chunk_count
                        from {chunks_table} c where c.source_id = d.id
                    ) cc
                )
                delete from {sources_table} t
                using ranked r
                where t.id = r.id and r.rn > 1
                """
            ).format(
                sources_table=sql.Identifier("public", sources_table),
                chunks_table=sql.Identifier("public", chunks_table),
            ),
            (dataset,),
        )
        deleted = cur.rowcount
    if deleted:
        log.info(
            "deduplicate_sources: deleted %d redundant (url, title) duplicate(s) and their chunks from %s",
            deleted,
            sources_table,
        )
    return deleted


def refresh_priority_scores(dataset: str) -> None:
    """Recompute priority_score for every active source in this dataset.

    A freshly upserted source has no priority_score until someone separately
    runs the dataset's update_priorities script -- until then it defaults to
    0.0, which drags final_score down enough that even a strong lexical/exact
    keyword match can miss the top-k cut entirely (this happened to a brand
    new CE department contact page: it ranked #1 by lexical match but was
    absent from the final results because every other candidate had a
    precomputed priority_score and it did not). Each dataset's scoring
    formula lives in its own update_priorities module -- they pull in
    different reference corpora (rule text only, or rule+main text for ce) --
    so dispatch to those instead of reimplementing the formula here. This
    always does a full recompute (not just the newly loaded rows), matching
    how those scripts are normally run by hand; on the datasets exercised so
    far that has taken well under a minute.
    """
    from psycopg.rows import dict_row as _dict_row

    family = dataset if dataset in TABLE_FAMILIES else table_family(dataset)
    if family == "rag":
        # Scores every active rag_sources row, so it covers all departments.
        from scripts.ce.update_priorities import update_ce_priorities

        with connect_postgres(row_factory=_dict_row, prepare_threshold=None) as conn:
            count = update_ce_priorities(conn, False, 10)
    elif family == "rule":
        from scripts.rule.update_priorities import update_rule_priorities

        with connect_postgres(row_factory=_dict_row, prepare_threshold=None) as conn:
            count = update_rule_priorities(conn, False, 10)
    elif family in ("pknu_notice", "pknu_main", "pknu_syllabus"):
        from scripts.rag.update_main_priorities import fetch_rule_contents, update_dataset_priorities
        from scripts.rag.priority import build_rule_feature_set

        with connect_postgres(row_factory=_dict_row, prepare_threshold=None) as conn:
            rule_features = build_rule_feature_set(fetch_rule_contents(conn))
            count = update_dataset_priorities(conn, family, rule_features, False, 10)
    else:
        log.warning(
            "refresh_priority_scores: no priority model wired for dataset=%s, skipping",
            dataset,
        )
        return

    log.info(
        "refresh_priority_scores: recomputed priority_score for %d %s source(s)",
        count,
        dataset,
    )


def load(
    index_path: Path,
    batch_size: int,
    dataset: str,
    sources_table: str | None = None,
    chunks_table: str | None = None,
    replace_sources: bool = False,
    refresh_priorities: bool = True,
) -> int:
    if not index_path.exists():
        raise FileNotFoundError(f"Index file not found: {index_path}")

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    embedding_model = preflight_index(index_path, dataset)
    families = dataset_families(dataset)
    if len(families) > 1 and (sources_table or chunks_table):
        raise ValueError("Mixed main-site input cannot override table names")
    targets = {family: (
        validate_table_name(sources_table or family + "_sources"),
        validate_table_name(chunks_table or family + "_chunks"),
    ) for family in families}
    log.info("Loading Supabase rows from %s; target families=%s", index_path, families)
    log.info("embedding model: %s", embedding_model)
    total = 0
    excluded = 0
    batches = {family: [] for family in families}
    excluded_ids = {family: [] for family in families}
    replaced_source_ids = {family: set() for family in families}
    pending = 0
    with connect() as conn:
        for source_target, chunk_target in targets.values():
            ensure_schema_ready(conn, source_target, chunk_target)

        def commit_batch():
            nonlocal total, pending
            accepted = sum(len(rows) for rows in batches.values())
            for family, (source_target, chunk_target) in targets.items():
                flush_batch(conn, batches[family], source_target, chunk_target,
                            replace_sources=replace_sources,
                            replaced_source_ids=replaced_source_ids[family])
                delete_excluded_chunks(conn, chunk_target, excluded_ids[family])
            conn.commit()
            total += accepted
            for family in families:
                batches[family] = []
                excluded_ids[family] = []
            pending = 0
            log.info("[%s] upserted %d rows", dataset, total)

        for record in iter_records(index_path):
            family = table_family(dataset, record.get("metadata") or {})
            if not has_record_body(record):
                excluded += 1
                excluded_ids[family].append(record_identity(record, dataset)[1])
            else:
                batches[family].append(prepare_row(record, embedding_model, dataset))
            pending += 1
            if pending >= batch_size:
                commit_batch()
        if pending:
            commit_batch()
        for source_target, chunk_target in targets.values():
            deduplicate_sources(conn, source_target, chunk_target, dataset)
        conn.commit()

    log.info("[%s] loaded=%d excluded_empty_or_fallback=%d", dataset, total, excluded)
    if refresh_priorities:
        for family in families:
            refresh_priority_scores(family)

    return total


def load_all(batch_size: int = 200, replace_sources: bool = False) -> int:
    """Validate every collected dataset before writing; refresh each DB family once."""
    selected = []
    families = {}
    for dataset, paths in dataset_paths(PROJECT_ROOT).items():
        if not paths["input_root"].is_dir():
            continue
        if dataset not in DATASET_TABLES:
            raise ValueError(f"Dataset has no database table mapping: {dataset}")
        for family in dataset_families(dataset):
            families.setdefault(family, family)
        index = resolve_index_path(dataset, None)
        preflight_index(index, dataset)
        selected.append((dataset, index))
        log.info("[PREFLIGHT-OK] %s", dataset)
    if not selected:
        raise ValueError("No collected datasets found")
    # Regulations provide reference text for the other priority models.
    selected.sort(key=lambda item: (item[0] != "rule", item[0]))
    total = 0
    for position, (dataset, index) in enumerate(selected, 1):
        log.info("[DATASET-START %d/%d] %s", position, len(selected), dataset)
        count = load(index, batch_size, dataset, replace_sources=replace_sources,
                     refresh_priorities=False)
        total += count
        log.info("[DATASET-DONE %d/%d] %s rows=%d total=%d", position, len(selected), dataset, count, total)
    for dataset in families.values():
        refresh_priority_scores(dataset)
    log.info("[ALL-DONE] datasets=%d rows=%d", len(selected), total)
    return total


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Load RAG chunks into Supabase PostgreSQL.")
    parser.add_argument("--index", type=Path, default=None)
    parser.add_argument("--dataset", choices=["all", *sorted(DATASET_TABLES)], default=DEFAULT_DATASET)
    parser.add_argument("--sources-table", help="Override source table name in public schema.")
    parser.add_argument("--chunks-table", help="Override chunk table name in public schema.")
    parser.add_argument("--batch-size", type=int, default=200)
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument("--prepare-bulk", action="store_true",
                             help="Explicitly remove only named search indexes before bulk ingestion; keep rows and constraints.")
    maintenance.add_argument("--finalize-indexes", action="store_true",
                             help="Build search indexes once after all ingestion terminals complete, then ANALYZE.")
    parser.add_argument("--index-timeout-seconds", type=int, default=1800,
                        help="Timeout for each index-maintenance statement; 0 disables it for this transaction only.")
    parser.add_argument(
        "--replace-sources",
        action="store_true",
        help="Delete existing chunks for sources present in the index before inserting current chunks.",
    )
    args = parser.parse_args()

    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.prepare_bulk or args.finalize_indexes:
        if args.index or args.replace_sources:
            parser.error("Index maintenance cannot use --index or --replace-sources")
        if args.index_timeout_seconds < 0:
            parser.error("--index-timeout-seconds must be nonnegative (0 disables this statement timeout)")
        if args.dataset == "all":
            if args.sources_table or args.chunks_table:
                parser.error("--dataset all cannot override table names")
            targets = sorted({(v["sources"], v["chunks"]) for v in DATASET_TABLES.values()})
        else:
            families = dataset_families(args.dataset)
            if len(families) > 1:
                if args.sources_table or args.chunks_table:
                    parser.error("Mixed main-site input cannot override table names")
                targets = [(family + "_sources", family + "_chunks") for family in families]
            else:
                targets = [resolve_tables(args.dataset, args.sources_table, args.chunks_table)]
        with connect() as conn:
            conn.add_notice_handler(
                lambda notice: log.info("[INDEX-NOTICE] %s", notice.message_primary))
            for sources, chunks in targets:
                ensure_schema_ready(conn, sources, chunks)
                manage_search_indexes(conn, sources, chunks, create=args.finalize_indexes,
                                      timeout_seconds=args.index_timeout_seconds)
        return
    if args.dataset == "all":
        if args.index or args.sources_table or args.chunks_table:
            parser.error("--dataset all cannot use index or table overrides")
        load_all(args.batch_size, args.replace_sources)
        return

    index_path = resolve_index_path(args.dataset, args.index)
    count = load(
        index_path,
        args.batch_size,
        args.dataset,
        args.sources_table,
        args.chunks_table,
        args.replace_sources,
    )
    log.info("done: upserted %d rows", count)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("Ingestion failed: type=%s sqlstate=%s",
                      type(exc).__name__, getattr(exc, "sqlstate", None))
        raise SystemExit(1)
