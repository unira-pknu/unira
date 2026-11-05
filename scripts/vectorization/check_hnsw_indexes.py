"""Verify valid halfvec HNSW indexes and construction settings for each family."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.db import connect_postgres  # noqa: E402

from scripts.vectorization.datasets import database_tables
from scripts.vectorization.load_to_supabase import is_halfvec_hnsw_index
CHUNKS_TABLES = tuple(sorted({value["chunks"] for value in database_tables(PROJECT_ROOT).values()}))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    missing: list[str] = []
    with connect_postgres(autocommit=True) as conn:
        with conn.cursor() as cur:
            for table in CHUNKS_TABLES:
                cur.execute(
                    """
                    select x.relname, pg_catalog.pg_get_indexdef(x.oid), i.indisvalid,
                           a.amname, pg_catalog.pg_get_expr(i.indexprs, i.indrelid),
                           o.opcname, x.reloptions
                    from pg_catalog.pg_index i
                    join pg_catalog.pg_class x on x.oid=i.indexrelid
                    join pg_catalog.pg_class t on t.oid=i.indrelid
                    join pg_catalog.pg_namespace n on n.oid=t.relnamespace
                    join pg_catalog.pg_am a on a.oid=x.relam
                    left join pg_catalog.pg_opclass o on o.oid=i.indclass[0]
                    where n.nspname = 'public' and t.relname = %s and a.amname = 'hnsw'
                      and i.indpred is null
                    """,
                    (table,),
                )
                rows = cur.fetchall()
                cur.execute(
                    f"select count(*) from public.{table}"  # noqa: S608 (table from allowlist)
                )
                (row_count,) = cur.fetchone()
                found = False
                for indexname, indexdef, valid, *definition in rows:
                    expected = valid and is_halfvec_hnsw_index(*definition)
                    found = found or expected
                    status = "OK" if expected else "MISMATCH"
                    print(f"[{status}] {table:32s} rows={row_count:>7} index={indexname}")
                    print(f"       {indexdef}")
                if not found:
                    missing.append(table)
                    print(f"[MISS] {table:32s} rows={row_count:>7} -- NO VALID HALFVEC HNSW INDEX")
    if missing:
        print(f"\nMissing expected halfvec HNSW index on: {', '.join(missing)}")
        return 1
    print(f"\nAll {len(CHUNKS_TABLES)} chunks tables have valid halfvec HNSW indexes (m=16, ef_construction=64).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
