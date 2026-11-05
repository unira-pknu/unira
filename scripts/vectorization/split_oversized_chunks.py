"""거대 chunk를 외과적으로 분할/재임베딩/교체하는 1회용 스크립트.

DB에서 LENGTH(content) >= --threshold 인 chunk를 찾아 scripts.preprocessing.file_preprocessing.chunk_blocks
로 다시 자른 뒤 재임베딩, 원본 chunk를 트랜잭션 안에서 DELETE하고 새 sub-chunk를
INSERT 한다. sources 테이블은 건드리지 않는다.

기본은 dry-run. 실제 변경하려면 --apply 명시.
실제 변경 직전에 영향 받는 chunk row 전체를 backups/split_oversized/<dataset>_<ts>.json
으로 dump 한다 (--no-backup 으로만 끌 수 있음).

사용:
    # dry-run (변경 없음, 무엇을 어떻게 자를지 콘솔에 출력)
    python scripts/vectorization/split_oversized_chunks.py --dataset pknu_notice
    python scripts/vectorization/split_oversized_chunks.py --dataset pknu_main

    # 실제 적용 (백업 자동, threshold 5000 기본)
    python scripts/vectorization/split_oversized_chunks.py --dataset pknu_notice --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.db import connect_postgres  # noqa: E402
from scripts.preprocessing.file_preprocessing import (  # noqa: E402
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    chunk_blocks,
)
from scripts.vectorization.vectorization import stable_id  # noqa: E402
from scripts.vectorization.datasets import TABLE_FAMILIES  # noqa: E402

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "split_oversized_chunks.log"

DATASET_TABLES = {
    family: {"sources": family + "_sources", "chunks": family + "_chunks"}
    for family in TABLE_FAMILIES
}

DEFAULT_THRESHOLD = 5000
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backups" / "split_oversized"
from scripts.vectorization.common import EXPECTED_DIMENSIONS as EXPECTED_EMBEDDING_DIM, DEFAULT_SENTENCE_MODEL as DEFAULT_EMBEDDING_MODEL, load_sentence_model, encode_normalized

log = logging.getLogger("split_oversized_chunks")


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


def connect() -> psycopg.Connection:
    return connect_postgres(autocommit=False, connect_timeout_seconds=15)


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(float(v)) for v in values) + "]"


def fetch_oversized(
    conn: psycopg.Connection, chunks_table: str, sources_table: str, threshold: int
) -> list[dict[str, Any]]:
    query = sql.SQL(
        """
        SELECT
            c.id,
            c.source_id,
            c.chunk_id,
            c.chunk_index,
            c.content,
            c.content_hash,
            c.token_count,
            c.metadata,
            c.embedding_model,
            c.embedding_dim,
            s.source_slug,
            s.title AS source_title,
            s.url AS source_url,
            s.source_type
        FROM {chunks} c
        JOIN {sources} s ON s.id = c.source_id
        WHERE LENGTH(c.content) >= %s
        ORDER BY LENGTH(c.content) DESC
        """
    ).format(
        chunks=sql.Identifier("public", chunks_table),
        sources=sql.Identifier("public", sources_table),
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, (threshold,))
        return list(cur.fetchall())


def max_chunk_index_per_source(
    conn: psycopg.Connection, chunks_table: str, source_ids: list[str]
) -> dict[str, int]:
    if not source_ids:
        return {}
    query = sql.SQL(
        "SELECT source_id, MAX(chunk_index) AS mx FROM {chunks} WHERE source_id = ANY(%s) GROUP BY source_id"
    ).format(chunks=sql.Identifier("public", chunks_table))
    with conn.cursor() as cur:
        cur.execute(query, (source_ids,))
        return {row[0]: int(row[1]) for row in cur.fetchall()}


def split_one(
    row: dict[str, Any], chunk_size: int, overlap: int
) -> list[dict[str, Any]]:
    """원본 chunk row를 받아 chunk_blocks로 다시 잘라 sub-chunk dict 리스트를 만든다.

    임베딩은 아직 채우지 않는다 (dry-run에서는 임베딩 계산 자체를 건너뛴다).
    """
    blocks = [{"text": row["content"]}]
    sub = chunk_blocks(blocks, chunk_size=chunk_size, overlap=overlap)
    return sub


class EmbedderLazy:
    """필요할 때만 sentence-transformers 모델을 로드한다 (dry-run에서는 안 로드)."""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            log.info("Loading embedder %s ...", self._model_name)
            self._model = load_sentence_model(self._model_name)
        vectors = encode_normalized(self._model, texts)
        return [[round(float(v), 8) for v in row] for row in vectors]


def derive_embedding_model(rows: list[dict[str, Any]]) -> str:
    models = {r.get("embedding_model") for r in rows if r.get("embedding_model")}
    if len(models) == 1:
        only = next(iter(models))
        prefix = "sentence-transformers:"
        return only[len(prefix) :] if only.startswith(prefix) else only
    return DEFAULT_EMBEDDING_MODEL


def write_backup(rows: list[dict[str, Any]], backup_dir: Path, dataset: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backup_dir / f"{dataset}_oversized_{ts}.json"
    payload = []
    for r in rows:
        payload.append(
            {
                "id": r["id"],
                "source_id": r["source_id"],
                "chunk_id": r["chunk_id"],
                "chunk_index": r["chunk_index"],
                "content": r["content"],
                "content_hash": r["content_hash"],
                "token_count": r["token_count"],
                "metadata": r["metadata"],
                "embedding_model": r["embedding_model"],
                "embedding_dim": r["embedding_dim"],
                "source_slug": r["source_slug"],
                "source_title": r["source_title"],
                "source_url": r["source_url"],
                "source_type": r["source_type"],
            }
        )
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def apply_one_source(
    conn: psycopg.Connection,
    chunks_table: str,
    rows_for_source: list[dict[str, Any]],
    sub_chunks_per_row: list[list[dict[str, Any]]],
    base_index_for_source: int,
    embedder: EmbedderLazy,
    embedding_model_name: str,
) -> tuple[int, int]:
    """한 source의 거대 chunk들을 트랜잭션 안에서 교체. 반환: (삭제수, 삽입수)."""

    source_id = rows_for_source[0]["source_id"]
    source_slug = rows_for_source[0]["source_slug"]

    all_subs = [
        (parent_row, sub)
        for parent_row, subs in zip(rows_for_source, sub_chunks_per_row)
        for sub in subs
    ]
    if not all_subs:
        return (0, 0)

    texts_to_embed = [sub["text"] for _, sub in all_subs]
    vectors = embedder.encode(texts_to_embed)
    if any(len(v) != EXPECTED_EMBEDDING_DIM for v in vectors):
        raise RuntimeError(
            f"embedding dim mismatch for source {source_id}: expected {EXPECTED_EMBEDDING_DIM}"
        )

    now = datetime.now(timezone.utc)
    new_rows: list[dict[str, Any]] = []
    for offset, ((parent_row, sub), vec) in enumerate(zip(all_subs, vectors), start=1):
        new_index = base_index_for_source + offset
        new_id = stable_id(source_slug, new_index)
        text = sub["text"]

        new_rows.append(
            {
                "id": new_id,
                "source_id": source_id,
                "chunk_id": int(sub.get("chunk_id", offset)),
                "chunk_index": new_index,
                "content": text,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "token_count": len(text.split()),
                "metadata": Jsonb(parent_row["metadata"] or {}),
                "embedding": vector_literal(vec),
                "embedding_model": f"sentence-transformers:{embedding_model_name}",
                "embedding_dim": EXPECTED_EMBEDDING_DIM,
                "embedded_at": now,
            }
        )

    delete_sql = sql.SQL("DELETE FROM {chunks} WHERE id = ANY(%s)").format(
        chunks=sql.Identifier("public", chunks_table)
    )
    insert_sql = sql.SQL(
        """
        INSERT INTO {chunks} (
            id, source_id, chunk_id, chunk_index, content, content_hash,
            token_count, metadata, embedding, embedding_model, embedding_dim, embedded_at
        ) VALUES (
            %(id)s, %(source_id)s, %(chunk_id)s, %(chunk_index)s, %(content)s, %(content_hash)s,
            %(token_count)s, %(metadata)s, %(embedding)s::extensions.vector(384),
            %(embedding_model)s, %(embedding_dim)s, %(embedded_at)s
        )
        """
    ).format(chunks=sql.Identifier("public", chunks_table))

    parent_ids = [r["id"] for r in rows_for_source]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(delete_sql, (parent_ids,))
            deleted = cur.rowcount
            cur.executemany(insert_sql, new_rows)
    conn.commit()
    return (deleted, len(new_rows))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, choices=sorted(DATASET_TABLES))
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="실제 DELETE/INSERT 실행. 미지정 시 dry-run.",
    )
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="--apply 시 백업을 생략 (권장하지 않음).",
    )
    args = parser.parse_args()

    configure_logging()
    tables = DATASET_TABLES[args.dataset]
    sources_table = tables["sources"]
    chunks_table = tables["chunks"]
    mode = "APPLY (destructive)" if args.apply else "DRY-RUN (no DB changes)"

    log.info("=" * 72)
    log.info("split_oversized_chunks  dataset=%s  mode=%s", args.dataset, mode)
    log.info(
        "threshold=%d  chunk_size=%d  overlap=%d",
        args.threshold,
        args.chunk_size,
        args.overlap,
    )
    log.info("tables: public.%s, public.%s", sources_table, chunks_table)
    log.info("=" * 72)

    conn = connect()
    try:
        rows = fetch_oversized(conn, chunks_table, sources_table, args.threshold)
        log.info("Oversized chunks found: %d", len(rows))
        if not rows:
            log.info("Nothing to do.")
            return 0

        plans: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for r in rows:
            subs = split_one(r, args.chunk_size, args.overlap)
            plans.append((r, subs))

        per_source: dict[str, list[tuple[dict[str, Any], list[dict[str, Any]]]]] = {}
        for r, subs in plans:
            per_source.setdefault(r["source_id"], []).append((r, subs))

        log.info("")
        log.info("--- Plan (per oversized chunk) ---")
        total_delete = 0
        total_insert = 0
        for r, subs in plans:
            sizes = [len(s["text"]) for s in subs]
            size_summary = (
                f"n={len(sizes)} min={min(sizes)} max={max(sizes)} avg={sum(sizes)//len(sizes)}"
                if sizes
                else "n=0"
            )
            log.info(
                "  src=%s type=%s  chunk_id=%s idx=%s  cur_len=%d  →  subs: %s",
                r["source_id"][:12],
                r["source_type"],
                r["chunk_id"],
                r["chunk_index"],
                len(r["content"]),
                size_summary,
            )
            log.info(
                "    title=%s",
                (r["source_title"] or "")[:90],
            )
            total_delete += 1
            total_insert += len(subs)

        log.info("")
        log.info("--- Summary ---")
        log.info("affected sources: %d", len(per_source))
        log.info("chunks to DELETE: %d", total_delete)
        log.info("chunks to INSERT: %d", total_insert)

        if not args.apply:
            log.info("")
            log.info("Dry-run only. Re-run with --apply to execute.")
            return 0

        if not args.no_backup:
            backup_path = write_backup(rows, args.backup_dir, args.dataset)
            log.info("Backup written → %s", backup_path)
        else:
            log.warning("Backup skipped by --no-backup flag.")

        base_indexes = max_chunk_index_per_source(conn, chunks_table, list(per_source))
        embedding_model_name = derive_embedding_model(rows)
        embedder = EmbedderLazy(embedding_model_name)

        log.info("")
        log.info("--- Applying changes ---")
        deleted_total = 0
        inserted_total = 0
        for source_id, items in per_source.items():
            rows_for_source = [r for r, _ in items]
            subs_for_source = [s for _, s in items]
            base_idx = base_indexes.get(source_id, -1)
            deleted, inserted = apply_one_source(
                conn,
                chunks_table,
                rows_for_source,
                subs_for_source,
                base_idx,
                embedder,
                embedding_model_name,
            )
            deleted_total += deleted
            inserted_total += inserted
            log.info(
                "  source=%s  deleted=%d inserted=%d  (new indexes start at %d)",
                source_id[:12],
                deleted,
                inserted,
                base_idx + 1,
            )

        log.info("")
        log.info("Done. deleted=%d  inserted=%d", deleted_total, inserted_total)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
