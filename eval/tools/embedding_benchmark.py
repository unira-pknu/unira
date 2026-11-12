"""Compare embedding and reranker models on the project's own documents (read-only).

The 2026-11-12 measurement on CPU put the current embedding model
(paraphrase-multilingual-MiniLM-L12-v2) at 12% recall@10 for 64 questions whose
correct document is known: vector search alone rarely surfaced the right page
("도서관 운영 시간", department 교수진 pages). This tool measures candidate
models on the same corpus so a replacement is chosen by numbers.

It never writes to the database. Run it where a GPU is available; CPU works but
takes hours per large model.

    python -m eval.tools.embedding_benchmark corpus            # DB -> corpus.jsonl, queries.jsonl
    python -m eval.tools.embedding_benchmark run               # all models + rerankers, prints a table
    python -m eval.tools.embedding_benchmark run --models minilm bge-m3 --rerankers none

Output: var/embedding_bench/ (git-ignored): embeddings cache, results.json.
Scoring is per document: chunks of the same URL count once, so a model is not
rewarded for returning five chunks of one page.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = PROJECT_ROOT / "var" / "embedding_bench"

# key: (model id, query prefix, passage prefix)
EMBEDDING_MODELS = {
    "minilm": ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "", ""),  # current
    "e5-base": ("intfloat/multilingual-e5-base", "query: ", "passage: "),
    "e5-large": ("intfloat/multilingual-e5-large", "query: ", "passage: "),
    "bge-m3": ("BAAI/bge-m3", "", ""),
    "kure": ("nlpai-lab/KURE-v1", "", ""),
}
RERANKERS = {
    "mmarco": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",  # current
    "bge-reranker": "BAAI/bge-reranker-v2-m3",
}
RERANK_POOL = 30  # the backend reranks about 30 candidates

# Questions that failed in production on 2026-11-11 and 2026-11-12.
# (question, title fragments that count as correct, URL fragments that count as correct)
REAL_FAILURES = [
    ("도서관 운영 시간 알려줘", ["도서관 운영시간"], []),
    ("컴퓨터공학과 교수님 목록", [], ["ce.pknu.ac.kr/ce/1805"]),
    ("컴퓨터공학과 사무실이 어디야?", [], ["ce.pknu.ac.kr/ce/1807"]),
    ("전자공학과 건물이 어디야?", [], ["ee.pknu.ac.kr/ee/1664"]),
    ("부경대 포털 주소 알려줘", [], ["www.pknu.ac.kr/main/434"]),
    ("컴공 졸업요건", [], ["ce.pknu.ac.kr/ce/2889"]),
    ("중앙도서관 홈페이지 주소", [], ["www.pknu.ac.kr/main/290"]),
]


def _write_jsonl(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def build_corpus(out: Path, distractors: int, seed: int) -> None:
    """Rule and main-site chunks in full, every gold document, department guide
    pages, plus random notice/department chunks as distractors."""
    import psycopg
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / "backend" / ".env")
    cases = _read_jsonl(PROJECT_ROOT / "eval" / "cases" / "regression.jsonl")
    gold_urls = sorted({url for case in cases if isinstance(case.get("expected_source"), dict)
                        for url in case["expected_source"].get("allowed_urls") or []})
    rows: dict[str, dict] = {}
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("set transaction read only")
        conn.execute("set statement_timeout = '600s'")

        def take(sql: str, params=()) -> None:
            for chunk_id, url, title, content in conn.execute(sql, params).fetchall():
                rows[chunk_id] = {"id": chunk_id, "url": url or "", "title": title or "", "text": content or ""}

        join = "from {f}_chunks c join {f}_sources s on s.id = c.source_id where s.status = 'active'"
        for family in ("rule", "pknu_main"):
            take("select c.id, s.url, s.title, c.content " + join.format(f=family))
        for family in ("pknu_notice", "rag"):
            take("select c.id, s.url, s.title, c.content " + join.format(f=family) + " and s.url = any(%s)", (gold_urls,))
        # Department guide pages (faculty, location, requirements) carry no query string.
        take("select c.id, s.url, s.title, c.content " + join.format(f="rag") + " and s.url not like '%%?%%'")
        take("select c.id, s.url, s.title, c.content " + join.format(f="rag") + " and s.title like '%%도서관 운영시간%%'")
        take("select c.id, s.url, s.title, c.content " + join.format(f="pknu_notice") + " and s.title like '%%도서관%%운영%%'")
        for family, percent in (("pknu_notice", 3), ("rag", 0.3)):
            take(f"select c.id, s.url, s.title, c.content from {family}_chunks c tablesample system({percent}) repeatable({seed}) "
                 f"join {family}_sources s on s.id = c.source_id where s.status = 'active' limit %s", (distractors,))
    corpus = list(rows.values())
    urls = {row["url"] for row in corpus}
    queries = []
    for case in cases:
        source = case.get("expected_source")
        if not isinstance(source, dict):
            continue
        gold = [url for url in source.get("allowed_urls") or [] if url in urls]
        if gold:
            queries.append({"id": case["id"], "q": case["question"], "gold_urls": gold, "gold_titles": []})
    for index, (question, titles, url_parts) in enumerate(REAL_FAILURES):
        gold = sorted(url for url in urls if any(part in url for part in url_parts)) if url_parts else []
        if gold or (titles and any(any(t in row["title"] for t in titles) for row in corpus)):
            queries.append({"id": f"real_{index}", "q": question, "gold_urls": gold, "gold_titles": titles})
    out.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out / "corpus.jsonl", corpus)
    _write_jsonl(out / "queries.jsonl", queries)
    print(f"corpus={len(corpus)} chunks, queries={len(queries)} -> {out}")


def _device(requested: str) -> str:
    if requested != "auto":
        return requested
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


def embed(out: Path, key: str, device: str, max_seq: int, batch: int):
    import numpy as np
    from sentence_transformers import SentenceTransformer

    cache_q, cache_d = out / f"{key}_q.npy", out / f"{key}_d.npy"
    if cache_q.exists() and cache_d.exists():
        return np.load(cache_q), np.load(cache_d), None
    model_id, query_prefix, passage_prefix = EMBEDDING_MODELS[key]
    model = SentenceTransformer(model_id, device=device)
    if device == "cuda":
        model.half()
    model.max_seq_length = max_seq
    corpus, queries = _read_jsonl(out / "corpus.jsonl"), _read_jsonl(out / "queries.jsonl")
    started = time.time()
    q = model.encode([query_prefix + row["q"] for row in queries], normalize_embeddings=True, batch_size=batch)
    d = model.encode([passage_prefix + row["title"] + "\n" + row["text"] for row in corpus],
                     normalize_embeddings=True, batch_size=batch, show_progress_bar=True)
    seconds = time.time() - started
    np.save(cache_q, q.astype(np.float32))
    np.save(cache_d, d.astype(np.float32))
    del model
    if device == "cuda":
        import torch
        torch.cuda.empty_cache()
    return q, d, seconds


def _is_gold(row: dict, query: dict) -> bool:
    return row["url"] in query["gold_urls"] or any(t in row["title"] for t in query["gold_titles"])


def _rank_of_gold(chunk_order, corpus, query, limit=50):
    seen: list[str] = []
    for index in chunk_order:
        url = corpus[index]["url"]
        if url in seen:
            continue
        seen.append(url)
        if _is_gold(corpus[index], query):
            return len(seen)
        if len(seen) >= limit:
            return None
    return None


def _metrics(ranks: list) -> dict:
    n = len(ranks)
    return {
        "top1": sum(1 for r in ranks if r and r <= 1) / n,
        "top5": sum(1 for r in ranks if r and r <= 5) / n,
        "top10": sum(1 for r in ranks if r and r <= 10) / n,
        "mrr": sum(1 / r for r in ranks if r) / n,
    }


def run(out: Path, models: list[str], rerankers: list[str], device: str, max_seq: int, batch: int) -> None:
    import numpy as np

    device = _device(device)
    corpus, queries = _read_jsonl(out / "corpus.jsonl"), _read_jsonl(out / "queries.jsonl")
    print(f"device={device} corpus={len(corpus)} queries={len(queries)} max_seq={max_seq}")
    results = {"device": device, "corpus": len(corpus), "queries": len(queries), "embedding": {}, "rerank": {}}
    orders = {}
    for key in models:
        q, d, seconds = embed(out, key, device, max_seq, batch)
        sims = q @ d.T
        order = np.argsort(-sims, axis=1)[:, :400]
        orders[key] = order
        ranks = [_rank_of_gold(order[i], corpus, query) for i, query in enumerate(queries)]
        results["embedding"][key] = {**_metrics(ranks), "seconds": seconds, "dim": int(d.shape[1]),
                                     "missed": [queries[i]["id"] for i, r in enumerate(ranks) if not r or r > 10]}
        m = results["embedding"][key]
        print(f"[embed] {key:9} top1 {m['top1']:.0%} top5 {m['top5']:.0%} top10 {m['top10']:.0%} "
              f"MRR {m['mrr']:.3f} dim {m['dim']} " + (f"{seconds:.0f}s" if seconds else "(cache)"), flush=True)
    if rerankers and rerankers != ["none"] and orders:
        from sentence_transformers import CrossEncoder
        for rkey in rerankers:
            model = CrossEncoder(RERANKERS[rkey], device=device, max_length=max_seq)
            if device == "cuda":
                model.model.half()
            for ekey, order in orders.items():
                ranks = []
                for i, query in enumerate(queries):
                    pool = list(order[i][:RERANK_POOL])
                    scores = model.predict([(query["q"], corpus[j]["title"] + "\n" + corpus[j]["text"]) for j in pool],
                                           batch_size=batch)
                    reranked = [pool[k] for k in np.argsort(-np.asarray(scores))]
                    ranks.append(_rank_of_gold(reranked, corpus, query))
                name = f"{ekey}+{rkey}"
                results["rerank"][name] = _metrics(ranks)
                m = results["rerank"][name]
                print(f"[rerank] {name:22} top1 {m['top1']:.0%} top5 {m['top5']:.0%} top10 {m['top10']:.0%} MRR {m['mrr']:.3f}",
                      flush=True)
            del model
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved {out / 'results.json'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("corpus", "run"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--models", nargs="+", default=list(EMBEDDING_MODELS), choices=list(EMBEDDING_MODELS))
    parser.add_argument("--rerankers", nargs="+", default=list(RERANKERS), choices=[*RERANKERS, "none"])
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--max-seq", type=int, default=512)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--distractors", type=int, default=5000, help="random chunks per notice/department family")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    if args.command == "corpus":
        build_corpus(args.out, args.distractors, args.seed)
    else:
        if not (args.out / "corpus.jsonl").exists():
            print("corpus.jsonl not found: run the 'corpus' command first", file=sys.stderr)
            return 2
        run(args.out, args.models, args.rerankers, args.device, args.max_seq, args.batch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
