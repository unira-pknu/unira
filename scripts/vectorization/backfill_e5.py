"""Fill <family>_chunks_e5 with multilingual-e5-base vectors (migration 021).

Run where there is a GPU (the school server); the CPU works but is far slower:

    python -m scripts.vectorization.backfill_e5 --count-only          # how many are left
    python -m scripts.vectorization.backfill_e5 --families rule --limit 500   # short trial
    python -m scripts.vectorization.backfill_e5                       # everything

Only chunks without a current vector are embedded: no side row yet, or one computed
from older content (content_hash differs). The script can therefore be stopped and
started again at any time, and run after a crawl to cover new or edited chunks.
Each round of --round-size chunks is committed on its own.

The text embedded is the chunk content, the same text the current 384-dim vectors
were computed from (vectorization.py embeds record["text"], loaded as content).
"""

from __future__ import annotations

import argparse
import logging
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

Encoder = Callable[[list[str]], Sequence[Sequence[float]]]


def tables(family: str) -> tuple[str, str]:
    if family not in FAMILIES:
        raise ValueError(f"unknown family: {family}")
    return f"{family}_chunks", f"{family}_chunks_e5"


def pending_sql(family: str) -> str:
    chunks, e5 = tables(family)
    return (
        f"select c.id, c.content_hash, c.content from public.{chunks} c "
        f"left join public.{e5} e on e.id = c.id "
        "where c.id > %s and (e.id is null or e.content_hash <> c.content_hash) "
        "order by c.id limit %s"
    )


def count_sql(family: str) -> str:
    chunks, e5 = tables(family)
    return (
        f"select count(*) from public.{chunks} c left join public.{e5} e on e.id = c.id "
        "where e.id is null or e.content_hash <> c.content_hash"
    )


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


def halfvec_literal(vector: Sequence[float]) -> str:
    # halfvec keeps about 3 significant digits; 5 is plenty and keeps rows small.
    return "[" + ",".join(f"{float(value):.5g}" for value in vector) + "]"


def passage_texts(contents: Sequence[str]) -> list[str]:
    return [E5_PASSAGE_PREFIX + (content or "") for content in contents]


def fill_family(conn, family: str, encode: Encoder, *, round_size: int, limit: int | None = None) -> int:
    """Embed this family's pending chunks; returns how many were written."""
    written, last_id, started = 0, "", time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(count_sql(family))
        total = cur.fetchone()[0]
    conn.commit()
    log.info("%s: %d chunks to embed", family, total)
    if limit is not None:
        total = min(total, limit)
    while total and written < total:
        size = min(round_size, total - written)
        with conn.cursor() as cur:
            cur.execute(pending_sql(family), (last_id, size))
            rows = cur.fetchall()
        if not rows:
            break
        vectors = encode(passage_texts([row[2] for row in rows]))
        validate_vectors(vectors, len(rows), E5_DIMENSIONS)
        with conn.cursor() as cur:
            cur.execute(
                "create temporary table if not exists e5_stage "
                "(id text, content_hash text, embedding extensions.halfvec(768)) on commit delete rows"
            )
            with cur.copy("copy e5_stage (id, content_hash, embedding) from stdin") as copy:
                for (chunk_id, content_hash, _content), vector in zip(rows, vectors):
                    copy.write_row((chunk_id, content_hash, halfvec_literal(vector)))
            cur.execute(merge_sql(family))
        conn.commit()
        written += len(rows)
        last_id = rows[-1][0]
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
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from scripts.db import connect_postgres

    with connect_postgres() as conn:
        # Rounds wait on HNSW inserts; the session default (2 min) is for searches.
        conn.execute("set statement_timeout = '15min'")
        conn.commit()
        if args.count_only:
            for family in args.families:
                total = conn.execute(count_sql(family)).fetchone()[0]
                print(f"{family}: {total}")
            return 0
        device = args.device or default_device()
        log.info("model %s on %s", E5_MODEL, device)
        encode = load_encoder(device, args.batch_size)
        for family in args.families:
            fill_family(conn, family, encode, round_size=args.round_size, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
