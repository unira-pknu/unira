"""Fit EMBEDDING_SIMILARITY_FLOOR / _CEILING for a second embedding (read-only).

Retrieval mixes cosine similarity with priority weights and a synthetic score for
keyword-only hits (0.30-0.75); all of that was tuned on MiniLM's spread. e5 packs
its top-30 scores into a narrow band (about 0.82-0.87), so the backend maps them
with (s - floor) / (ceiling - floor). This fits that line so e5's score
percentiles match MiniLM's on the same questions and tables.

    python -m eval.tools.similarity_scale                      # all five families
    python -m eval.tools.similarity_scale --families rule pknu_main

It calls the existing and the _e5 match functions in a read-only transaction;
only rows that already have e5 vectors count, so run it after backfill_e5.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FAMILIES = ("rule", "pknu_main", "pknu_syllabus", "pknu_notice", "rag")
PERCENTILES = (10, 25, 50, 75, 90)
OLD_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def fit_scale(old: "np.ndarray", new: "np.ndarray") -> tuple[float, float]:
    """floor, ceiling such that percentile((new - floor) / (ceiling - floor)) ~ percentile(old)."""
    import numpy as np

    k, b = np.polyfit([np.percentile(new, p) for p in PERCENTILES],
                      [np.percentile(old, p) for p in PERCENTILES], 1)
    floor = -b / k
    return float(floor), float(floor + 1 / k)


def _literal(vector) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in vector) + "]"


def main(argv: list[str] | None = None) -> int:
    import numpy as np
    import psycopg
    from dotenv import dotenv_values
    from sentence_transformers import SentenceTransformer

    from scripts.vectorization.common import E5_DIMENSIONS, E5_MODEL, E5_QUERY_PREFIX, EXPECTED_DIMENSIONS

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--families", nargs="+", choices=FAMILIES, default=list(FAMILIES))
    parser.add_argument("--cases", default=str(PROJECT_ROOT / "eval/cases/regression.jsonl"))
    parser.add_argument("--top", type=int, default=30, help="hits per question and table (the backend fetches 30)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    questions = [json.loads(line)["question"] for line in open(args.cases, encoding="utf-8") if line.strip()]
    old_q = SentenceTransformer(OLD_MODEL, device="cpu").encode(questions, normalize_embeddings=True)
    new_q = SentenceTransformer(E5_MODEL, device="cpu").encode(
        [E5_QUERY_PREFIX + q for q in questions], normalize_embeddings=True)
    url = dotenv_values(PROJECT_ROOT / "backend/.env")["DATABASE_URL"]
    old, new = [], []
    with psycopg.connect(url) as conn:
        conn.execute("set transaction read only")
        conn.execute("set statement_timeout = '60s'")
        for family in args.families:
            for i in range(len(questions)):
                for out, fn, vec, dim in ((old, f"match_{family}_documents", old_q[i], EXPECTED_DIMENSIONS),
                                          (new, f"match_{family}_documents_e5", new_q[i], E5_DIMENSIONS)):
                    out += [r[0] for r in conn.execute(
                        f"select similarity from public.{fn}(%s::extensions.vector({dim}), %s, 0.0, '{{}}'::jsonb)",
                        (_literal(vec), args.top))]
            print(f"{family}: done", flush=True)
    old, new = np.array(old), np.array(new)
    floor, ceiling = fit_scale(old, new)
    scaled = np.minimum(1.0, (new - floor) / (ceiling - floor))
    show = lambda a: " ".join(f"{np.percentile(a, p):.3f}" for p in PERCENTILES)
    print(f"hits: MiniLM {len(old)}, e5 {len(new)}   percentiles {PERCENTILES}")
    print(f"MiniLM        {show(old)}")
    print(f"e5            {show(new)}")
    print(f"e5 rescaled   {show(scaled)}")
    print(f"EMBEDDING_SIMILARITY_FLOOR={floor:.3f}\nEMBEDDING_SIMILARITY_CEILING={ceiling:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
