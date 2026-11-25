"""Compare retrieval between two search settings on the regression questions.

Runs the same retrieval /ask uses (rule-based plan, no GPT sub-questions, no
answer generation) once per setting and reports how often an allowed source
URL appears among the final top-k sources. Reads the database only; costs no
OpenAI tokens. Run from the repository root:

    python -m eval.tools.retrieval_compare
    python -m eval.tools.retrieval_compare --limit 10 --only e5

Settings come from backend/.env; each run below overrides only the search
fields. Results go to var/retrieval_compare/ (git-ignored).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

RUNS = {
    "minilm": {
        "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "expected_dimensions": 384,
        "embedding_query_prefix": "",
        "vector_rpc_suffix": "",
        "embedding_similarity_floor": 0.0,
        "embedding_similarity_ceiling": 1.0,
        "reranker_model": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
    },
    "e5": {
        "embedding_model": "intfloat/multilingual-e5-base",
        "expected_dimensions": 768,
        "embedding_query_prefix": "query: ",
        "vector_rpc_suffix": "_e5",
        "embedding_similarity_floor": 0.719,
        "embedding_similarity_ceiling": 0.924,
        "reranker_model": "BAAI/bge-reranker-v2-m3",
    },
}


def url_key(url: str) -> str:
    parts = urlsplit(unquote(url.strip()))
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


def rank_of_gold(sources, allowed: set[str]) -> int | None:
    """1-based rank of the first source whose URL (or an attachment's) is allowed."""
    for rank, source in enumerate(sources, 1):
        urls = [source.uri] + [getattr(a, "url", "") or "" for a in source.attachments]
        if any(url and url_key(url) in allowed for url in urls):
            return rank
    return None


def build_state(overrides: dict, device: str):
    from app.config import Settings
    from app.deps import AppState
    from app.embeddings import Embedder
    from app.reranking import CrossEncoderReranker
    from app.supabase_client import ThreadLocalSupabase

    settings = Settings(**overrides, embedding_device=device, reranker_device=device)
    embedder = Embedder(model_name=settings.embedding_model, device=settings.embedding_device,
                        expected_dimensions=settings.expected_dimensions,
                        query_prefix=settings.embedding_query_prefix)
    reranker = (CrossEncoderReranker(model_name=settings.reranker_model, device=settings.reranker_device,
                                     quantize=settings.reranker_quantize)
                if settings.reranker_enabled else None)
    supabase = ThreadLocalSupabase(settings.supabase_url, settings.supabase_service_role_key)
    return AppState(settings=settings, embedder=embedder, reranker=reranker, supabase=supabase,
                    openai=None, system_prompt="")


def run_one(name: str, overrides: dict, cases: list[dict], device: str, out_dir: Path) -> list[dict]:
    from app.routers.ask import _retrieve

    state = build_state(overrides, device)
    results = []
    for number, case in enumerate(cases, 1):
        allowed = {url_key(u) for u in case["expected_source"].get("allowed_urls", []) if u}
        started = time.perf_counter()
        error = None
        try:
            _rows, sources = _retrieve(state=state, user_id="retrieval-compare", question=case["question"],
                                       chat_history=[], sub_questions=[])
        except Exception as exc:  # one failing question must not end the run
            sources, error = [], f"{type(exc).__name__}: {exc}"
        seconds = time.perf_counter() - started
        rank = rank_of_gold(sources, allowed) if allowed else None
        results.append({"id": case["id"], "rank": rank, "seconds": round(seconds, 2), "error": error,
                        "has_gold": bool(allowed), "top": [s.uri for s in sources[:5]]})
        print(f"[{name}] {number}/{len(cases)} {case['id']} rank={rank} {seconds:.1f}s"
              + (f" ERROR {error[:80]}" if error else ""), flush=True)
    (out_dir / f"{name}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return results


def summary(results: list[dict]) -> dict:
    gold = [r for r in results if r["has_gold"] and not r["error"]]
    ranks = [r["rank"] for r in gold]

    def hit(k):
        return sum(1 for rank in ranks if rank and rank <= k) / len(ranks) if ranks else 0.0
    return {
        "questions": len(gold),
        "errors": sum(1 for r in results if r["error"]),
        "hit@1": hit(1), "hit@3": hit(3), "hit@10": hit(10),
        "mrr": sum(1 / rank for rank in ranks if rank) / len(ranks) if ranks else 0.0,
        "median_s": statistics.median(r["seconds"] for r in results) if results else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", default=str(ROOT / "eval" / "cases" / "regression.jsonl"))
    parser.add_argument("--only", choices=sorted(RUNS), nargs="+", default=sorted(RUNS, reverse=True))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--out", default=str(ROOT / "var" / "retrieval_compare"))
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cases = [json.loads(line) for line in Path(args.questions).read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = [c for c in cases if c.get("answerable", True)][: args.limit]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    by_run = {name: run_one(name, RUNS[name], cases, args.device, out_dir) for name in args.only}
    print()
    print(f"{'run':8} {'n':>4} {'err':>4} {'hit@1':>6} {'hit@3':>6} {'hit@10':>7} {'MRR':>6} {'median s':>9}")
    for name, results in by_run.items():
        s = summary(results)
        print(f"{name:8} {s['questions']:>4} {s['errors']:>4} {s['hit@1']:>6.1%} {s['hit@3']:>6.1%} "
              f"{s['hit@10']:>7.1%} {s['mrr']:>6.3f} {s['median_s']:>9.1f}")
    if len(by_run) == 2:
        a, b = (dict((r["id"], r["rank"]) for r in by_run[n]) for n in by_run)
        names = list(by_run)
        better = [i for i in a if (b[i] or 99) < (a[i] or 99)]
        worse = [i for i in a if (b[i] or 99) > (a[i] or 99)]
        print(f"\n{names[1]} better than {names[0]}: {len(better)}  worse: {len(worse)}")
        print("worse:", ", ".join(worse) or "-")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
