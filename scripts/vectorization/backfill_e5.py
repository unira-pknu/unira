"""Fill <family>_chunks_e5 with multilingual-e5-base vectors (migration 021).

Run where there is a GPU (the school server); the CPU works but is far slower:

    python -m scripts.vectorization.backfill_e5 --count-only          # how many are left
    python -m scripts.vectorization.backfill_e5 --families rule --limit 500   # short trial
    python -m scripts.vectorization.backfill_e5                       # everything

Only chunks without a current vector are embedded: no side row yet, or one computed
from older content (content_hash differs). The script can therefore be stopped and
started again at any time, and run after a crawl to cover new or edited chunks.
Each round of --round-size chunks is committed on its own.

On the school server the scheduled task unira-backfill-e5 runs it every 4 hours
(backend/deploy/run_backfill_e5.ps1), so chunks the daily crawls load get their e5
vectors without the crawl jobs (CPU-only GitHub runners) computing them.

The text embedded is the chunk content, the same text the current 384-dim vectors
were computed from (vectorization.py embeds record["text"], loaded as content).

Department copy (migration 026): rag_chunks_e5_dataset holds the rag e5 vectors of
each selected department on adjacent pages, so a department-filtered search reads
them in a few runs instead of one random read per chunk.

    python -m scripts.vectorization.backfill_e5 --build-dataset-copy            # every department up to 50000 chunks
    python -m scripts.vectorization.backfill_e5 --build-dataset-copy --datasets stat nulife

Building all departments empties the copy first and refills it one department
at a time (searches use the 025 paths meanwhile); naming departments replaces
only those. Afterwards the regular rounds keep the copy current for the
departments it holds. Run the build again after a department's chunks move or
when many new chunks were added, so its rows are adjacent again.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.vectorization.common import (  # noqa: E402
    E5_DIMENSIONS, E5_MODEL, E5_PASSAGE_PREFIX, validate_vectors,
)

log = logging.getLogger("backfill_e5")

# Smallest first, so a trial run finishes quickly and problems show up early.
FAMILIES = ("rule", "pknu_main", "pknu_syllabus", "pknu_notice", "rag")
MAX_SEQ_LENGTH = 512  # e5-base limit; longer chunks are cut, as in the benchmark
# One run at a time: the scheduled run must not start a second pass beside a long one.
LOCK_PATH = PROJECT_ROOT / "var" / "backfill_e5.lock"
DATASET_COPY_TABLE = "rag_chunks_e5_dataset"
# Larger departments use the HNSW path: they are 2% or more of rag, so it reaches
# match_count rows after a few hundred index rows (0.3-5 s cold, 2026-11-27), and
# copying them would double the size of the copy.
DATASET_COPY_MAX_CHUNKS = 50000

Encoder = Callable[[list[str]], Sequence[Sequence[float]]]


def tables(family: str) -> tuple[str, str]:
    if family not in FAMILIES:
        raise ValueError(f"unknown family: {family}")
    return f"{family}_chunks", f"{family}_chunks_e5"


def _pending_where(family: str) -> str:
    chunks, e5 = tables(family)
    return (
        f"from public.{chunks} c where not exists "
        f"(select 1 from public.{e5} e where e.id = c.id and e.content_hash = c.content_hash)"
    )


def pending_sql(family: str) -> str:
    # No ORDER BY on purpose: the chunk table is read once in storage order (a
    # sequential scan anti-joined to the side table). Walking it in id order read
    # one scattered heap/TOAST page per row; rag_chunks is 20 GB, far beyond the
    # database's memory, and that ran at 8-17 rows/s (2026-11-18).
    return f"select c.id, c.content_hash, c.content {_pending_where(family)}"


def count_sql(family: str) -> str:
    return f"select count(*) {_pending_where(family)}"


def merge_sql(family: str) -> str:
    # Joining the chunk table skips a chunk deleted while its vector was computed
    # (the foreign key would reject it). A chunk edited in the meantime is stored
    # with the old hash, is not searched, and is picked up again on the next run.
    chunks, e5 = tables(family)
    return (
        f"insert into public.{e5} (id, content_hash, embedding) "
        f"select s.id, s.content_hash, s.embedding from e5_stage s join public.{chunks} c on c.id = s.id "
        "on conflict (id) do update set content_hash = excluded.content_hash, "
        "embedding = excluded.embedding, embedded_at = now()"
    )


def dataset_copy_merge_sql() -> str:
    # Rows staged for rag, for departments the copy already holds (026). A chunk
    # whose department has no copy is searched through the 025 paths.
    return (
        f"insert into public.{DATASET_COPY_TABLE} (id, dataset, content_hash, embedding) "
        "select s.id, src.metadata ->> 'ingestion_dataset', s.content_hash, s.embedding "
        "from e5_stage s join public.rag_chunks c on c.id = s.id "
        "join public.rag_sources src on src.id = c.source_id "
        f"where exists (select 1 from public.{DATASET_COPY_TABLE} d "
        "where d.dataset = src.metadata ->> 'ingestion_dataset') "
        "on conflict (id) do update set dataset = excluded.dataset, "
        "content_hash = excluded.content_hash, embedding = excluded.embedding"
    )


DATASET_COUNTS_SQL = (
    "select src.metadata ->> 'ingestion_dataset', count(*) from public.rag_sources src "
    "join public.rag_chunks c on c.source_id = src.id "
    "where src.status = 'active' and src.metadata ? 'ingestion_dataset' "
    "group by 1 order by 1"
)

DATASET_COPY_INSERT_SQL = (
    f"insert into public.{DATASET_COPY_TABLE} (id, dataset, content_hash, embedding) "
    "select e.id, %(dataset)s, e.content_hash, e.embedding "
    "from public.rag_sources src join public.rag_chunks c on c.source_id = src.id "
    "join public.rag_chunks_e5 e on e.id = c.id and e.content_hash = c.content_hash "
    "where src.status = 'active' and src.metadata @> %(filter)s::jsonb "
    "on conflict (id) do update set dataset = excluded.dataset, "
    "content_hash = excluded.content_hash, embedding = excluded.embedding"
)


def select_copy_datasets(counts: dict[str, int], max_chunks: int, wanted: Sequence[str] | None = None) -> list[str]:
    """Departments to copy: the named ones, or every one up to max_chunks chunks."""
    if wanted:
        unknown = sorted(set(wanted) - set(counts))
        if unknown:
            raise ValueError("unknown datasets: " + ", ".join(unknown))
        return sorted(set(wanted))
    return sorted(name for name, total in counts.items() if name and total <= max_chunks)


def build_dataset_copy(conn, datasets: Sequence[str], *, replace_all: bool) -> int:
    """Fill the copy one department per transaction; returns rows written."""
    import json

    written = 0
    if replace_all:
        with conn.cursor() as cur:
            cur.execute(f"truncate public.{DATASET_COPY_TABLE}")
        conn.commit()
    for name in datasets:
        started = time.perf_counter()
        with conn.cursor() as cur:
            if not replace_all:
                cur.execute(f"delete from public.{DATASET_COPY_TABLE} where dataset = %(dataset)s", {"dataset": name})
            cur.execute(DATASET_COPY_INSERT_SQL, {"dataset": name, "filter": json.dumps({"ingestion_dataset": name})})
            rows = cur.rowcount
        conn.commit()
        written += max(rows, 0)
        log.info("copy %s: %d rows (%.0f s)", name, rows, time.perf_counter() - started)
    return written


def has_dataset_copy(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("select to_regclass(%s)", (f"public.{DATASET_COPY_TABLE}",))
        present = cur.fetchone()[0] is not None
    conn.commit()
    return present


def halfvec_literal(vector: Sequence[float]) -> str:
    # halfvec keeps about 3 significant digits; 5 is plenty and keeps rows small.
    return "[" + ",".join(f"{float(value):.5g}" for value in vector) + "]"


def passage_texts(contents: Sequence[str]) -> list[str]:
    return [E5_PASSAGE_PREFIX + (content or "") for content in contents]


def acquire_run_lock(path: Path):
    """Exclusive lock on a file, released when the process exits (also on a crash).
    Returns the open file, or None when another run holds it. A database advisory
    lock would not do: DATABASE_URL may point at the transaction pooler."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


WRITE_ATTEMPTS = 3


def write_round(conn, family: str, rows, vectors, *, dataset_copy: bool = False) -> None:
    """Stage one round and merge it, committing on `conn`.

    With dataset_copy (rag, migration 026 applied) the same staged rows also
    update the department copy, in the same transaction.

    The merge joins the chunk table, but a loader can still delete a chunk after
    the join saw it and before the foreign key check, so a round may fail with
    ForeignKeyViolation while a crawl runs. The rollback empties the stage; the
    retry's join no longer sees the deleted chunk."""
    from psycopg import errors

    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "create temporary table if not exists e5_stage "
                    "(id text, content_hash text, embedding extensions.halfvec(768)) on commit delete rows"
                )
                with cur.copy("copy e5_stage (id, content_hash, embedding) from stdin") as copy:
                    for (chunk_id, content_hash, _content), vector in zip(rows, vectors):
                        copy.write_row((chunk_id, content_hash, halfvec_literal(vector)))
                cur.execute(merge_sql(family))
                if dataset_copy and family == "rag":
                    cur.execute(dataset_copy_merge_sql())
            conn.commit()
            return
        except errors.ForeignKeyViolation:
            conn.rollback()
            if attempt == WRITE_ATTEMPTS:
                raise
            log.warning("%s: a chunk was deleted during the write, retrying the round (%d/%d)",
                        family, attempt, WRITE_ATTEMPTS)


def fill_family(conn, family: str, encode: Encoder, *, round_size: int, limit: int | None = None,
                total: int | None = None, read_conn=None, dataset_copy: bool = False) -> int:
    """Embed this family's pending chunks; returns how many were written.

    Pending rows stream from a server-side cursor on `read_conn` (one long read
    transaction); each round is written and committed on `conn`. Committing on
    the reading connection would close the cursor."""
    written, started = 0, time.perf_counter()
    if total is None:
        with conn.cursor() as cur:
            cur.execute(count_sql(family))
            total = cur.fetchone()[0]
        conn.commit()
    log.info("%s: %d chunks to embed", family, total)
    if limit is not None:
        total = min(total, limit)
    if not total:
        return 0
    reader = (read_conn or conn).cursor(name=f"e5_pending_{family}")
    reader.itersize = round_size
    reader.execute(pending_sql(family))
    while written < total:
        rows = reader.fetchmany(min(round_size, total - written))
        if not rows:
            break
        vectors = encode(passage_texts([row[2] for row in rows]))
        validate_vectors(vectors, len(rows), E5_DIMENSIONS)
        write_round(conn, family, rows, vectors, dataset_copy=dataset_copy)
        written += len(rows)
        elapsed = time.perf_counter() - started
        rate = written / elapsed if elapsed else 0.0
        eta = (total - written) / rate / 60 if rate else 0.0
        log.info("%s: %d/%d (%.0f/s, about %.0f min left)", family, written, total, rate, eta)
    return written


def load_encoder(device: str, batch_size: int) -> Encoder:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(E5_MODEL, device=device)
    model.max_seq_length = MAX_SEQ_LENGTH
    if device.startswith("cuda"):
        model.half()  # about twice as fast; the stored vectors are half precision anyway
    dimensions = model.get_sentence_embedding_dimension()
    if dimensions != E5_DIMENSIONS:
        raise ValueError(f"{E5_MODEL} gives {dimensions} dims, expected {E5_DIMENSIONS}")

    def encode(texts: list[str]):
        return model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                            show_progress_bar=False, convert_to_numpy=True).tolist()

    return encode


def default_device() -> str:
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--families", nargs="+", choices=FAMILIES, default=list(FAMILIES))
    parser.add_argument("--device", default=None, help="cuda or cpu (default: cuda when available)")
    parser.add_argument("--batch-size", type=int, default=64, help="texts per GPU batch")
    parser.add_argument("--round-size", type=int, default=1024, help="chunks per database round (one commit)")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many chunks per family")
    parser.add_argument("--count-only", action="store_true", help="only print how many chunks are left")
    parser.add_argument("--build-dataset-copy", action="store_true",
                        help="rebuild the rag department copy (migration 026) instead of embedding")
    parser.add_argument("--datasets", nargs="+", default=None, help="with --build-dataset-copy: only these departments")
    parser.add_argument("--max-dataset-chunks", type=int, default=DATASET_COPY_MAX_CHUNKS,
                        help="with --build-dataset-copy: skip departments with more chunks")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from scripts.db import connect_postgres

    lock = None if args.count_only else acquire_run_lock(LOCK_PATH)
    if not args.count_only and lock is None:
        log.info("another backfill_e5 is running; nothing to do")
        return 0
    with connect_postgres() as conn:
        # Rounds wait on HNSW inserts; the session default (2 min) is for searches.
        conn.execute("set statement_timeout = '15min'")
        conn.commit()
        if args.build_dataset_copy:
            if not has_dataset_copy(conn):
                log.error("public.%s is missing; apply migration 026 first", DATASET_COPY_TABLE)
                return 1
            counts = dict(conn.execute(DATASET_COUNTS_SQL).fetchall())
            conn.commit()
            datasets = select_copy_datasets(counts, args.max_dataset_chunks, args.datasets)
            log.info("copying %d departments, %d chunks", len(datasets), sum(counts[d] for d in datasets))
            written = build_dataset_copy(conn, datasets, replace_all=not args.datasets)
            log.info("department copy: %d rows", written)
            return 0
        pending = {family: conn.execute(count_sql(family)).fetchone()[0] for family in args.families}
        conn.commit()
        if args.count_only:
            for family, total in pending.items():
                print(f"{family}: {total}")
            return 0
        if not any(pending.values()):
            # The scheduled run (backend/deploy/run_backfill_e5.ps1) ends here on most
            # days, without loading the model.
            log.info("all e5 vectors are current")
            return 0
        # Rag rounds also refresh the department copy once migration 026 is applied.
        dataset_copy = bool(pending.get("rag")) and has_dataset_copy(conn)
        device = args.device or default_device()
        log.info("model %s on %s", E5_MODEL, device)
        encode = load_encoder(device, args.batch_size)
        # The reading transaction stays open for the whole family (hours for rag);
        # its statement only streams rows, so no timeout applies there.
        with connect_postgres() as read_conn:
            read_conn.execute("set statement_timeout = 0")
            read_conn.execute("set idle_in_transaction_session_timeout = 0")
            for family, total in pending.items():
                if total:
                    fill_family(conn, family, encode, round_size=args.round_size, limit=args.limit,
                                total=total, read_conn=read_conn, dataset_copy=dataset_copy)
                    read_conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
