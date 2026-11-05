"""Run the layout testset through a RAG chain backed by the local TEST store.

Chain: local sentence-transformers query embedding -> cosine top-k over the
``test_layout_rag_*`` store -> the backend's own prompt (``backend/prompts``
system prompt + ``app.generation.build_messages``) -> OpenAI answer.
Each answer is graded against the reference answer by an LLM judge, and
retrieval is scored against the chunk the question was generated from.
Writes ``eval/results/layout_eval_report.md`` (and a JSON with per-question details).

Safety: never queries Supabase. LLM requests are capped (<= 3 per question,
retries included) and the question count is capped at 50.

Example:
  python -m eval.tools.run_evaluation --limit 50
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for extra in (PROJECT_ROOT, PROJECT_ROOT / "backend"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from app.generation import build_messages, load_system_prompt  # noqa: E402
from eval.tools.llm_budget import BudgetedLLM, BudgetExceeded, FatalLLMError  # noqa: E402
from scripts.vectorization.layout_rag import LocalVectorStore, _relative, embed_texts, load_embedder  # noqa: E402

HARD_MAX_QUESTIONS = 50
NOT_FOUND = "관련 정보를 찾을 수 없습니다"
DEFAULT_DATASET = PROJECT_ROOT / "eval" / "results" / "layout_eval_dataset.csv"
DEFAULT_REPORT = PROJECT_ROOT / "eval" / "results" / "layout_eval_report.md"
VERDICT_SCORE = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}

JUDGE_PROMPT = """너는 RAG 답변 채점자다. 질문, 모범답안, 시스템 답변을 비교해 판정한다.
- correct: 모범답안의 핵심 사실을 모두 포함하고 모순되는 내용이 없다.
- partial: 핵심 사실 일부만 맞거나, 맞는 내용과 함께 불필요한 오류가 섞여 있다.
- incorrect: 핵심 사실이 틀렸거나 빠졌거나, 답변을 거부했다.
표현 방식이나 인용 번호([1] 등) 차이는 무시한다.
JSON 객체 {"verdict": "correct|partial|incorrect", "reason": "한 문장"} 만 출력한다."""


def _normalize(text: str) -> str:
    return re.sub(r"[\s|·,.'\"()]+", "", text).lower()


def answer_in_context(ground_truth: str, hits: list[dict[str, Any]]) -> bool:
    """Free proxy: does any retrieved chunk literally contain the reference answer?"""
    target = _normalize(ground_truth)
    return bool(target) and any(target in _normalize(h["content"]) for h in hits)


def as_backend_row(hit: dict[str, Any]) -> dict[str, Any]:
    prov = hit["provenance"]
    return {
        "content": hit["content"],
        "similarity": hit["similarity"],
        "title": f"{prov['source_path']} (본문 이미지, {hit['layout_type']})",
        "url": prov.get("image_url") or prov.get("source_path"),
    }


def judge(llm: BudgetedLLM, question: str, reference: str, answer: str) -> dict[str, str]:
    result = llm.chat_json([
        {"role": "system", "content": JUDGE_PROMPT},
        {"role": "user", "content": f"질문: {question}\n\n모범답안: {reference}\n\n시스템 답변: {answer}"},
    ], max_tokens=200)
    verdict = str(result.get("verdict") or "").strip().lower()
    if verdict not in VERDICT_SCORE:
        verdict = "incorrect"
    return {"verdict": verdict, "reason": str(result.get("reason") or "").strip()}


def summarize(results: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    def block(items: list[dict[str, Any]]) -> dict[str, Any]:
        graded = [r for r in items if r.get("verdict")]
        n = len(items)
        return {
            "n": n,
            "hit_at_1": sum(r["gold_rank"] == 1 for r in items) / n if n else 0.0,
            f"hit_at_{top_k}": sum(r["gold_rank"] is not None for r in items) / n if n else 0.0,
            "mrr": sum(1 / r["gold_rank"] for r in items if r["gold_rank"]) / n if n else 0.0,
            "image_hit": sum(r["image_hit"] for r in items) / n if n else 0.0,
            "answer_in_context": sum(r["answer_in_context"] for r in items) / n if n else 0.0,
            "graded": len(graded),
            "answer_score": sum(VERDICT_SCORE[r["verdict"]] for r in graded) / len(graded) if graded else 0.0,
            "correct": sum(r.get("verdict") == "correct" for r in items),
            "partial": sum(r.get("verdict") == "partial" for r in items),
            "incorrect": sum(r.get("verdict") == "incorrect" for r in items),
            "refused": sum(r.get("refused", False) for r in items),
        }

    by_layout: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_layout[r["layout_type"]].append(r)
    return {"overall": block(results), "by_layout": {k: block(v) for k, v in sorted(by_layout.items())}}


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_report(meta: dict[str, Any], summary: dict[str, Any], results: list[dict[str, Any]],
                  errors: list[dict[str, Any]], top_k: int) -> str:
    lines = ["# Layout RAG 평가 리포트", ""]
    if meta["synthetic_questions"]:
        lines += ["> ⚠️ **합성(synthetic) 테스트 데이터 기반 결과입니다.** 파이프라인 동작 검증용이며, "
                  "실제 OCR 검토 데이터의 품질 수치가 아닙니다.", ""]
    lines += [
        f"- 실행 시각: {meta['started_at']}",
        f"- 테스트 저장소: `{meta['store']}` (로컬 파일 저장소, Supabase 미사용)",
        f"- 임베딩 모델: `{meta['embedding_model']}` / top-k: {top_k}",
        f"- 테스트셋: `{meta['dataset']}` ({meta['questions']}문항, 생성 방식: {meta['generator']})",
    ]
    usage = meta["llm_usage"]
    if usage:
        lines += [
            f"- 답변·채점 모델: `{usage['model']}`",
            f"- LLM 사용량: 요청 {usage['requests']}회 (실패 {usage['failed_requests']}회), "
            f"입력 {usage['prompt_tokens']:,} / 출력 {usage['completion_tokens']:,} 토큰",
        ]
    else:
        lines.append("- 모드: **검색 전용(retrieval-only)** — LLM 답변 생성·채점 생략, API 호출 0회")
    lines += [
        "",
        "## 요약",
        "",
        f"| 구분 | 문항 | Hit@1 | Hit@{top_k} | MRR | 같은 이미지 Hit | 정답 문자열 컨텍스트 포함 | 답변 점수 | 정답/부분/오답 | 답변 거부 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    rows = [("전체", summary["overall"])] + list(summary["by_layout"].items())
    for name, s in rows:
        lines.append(
            f"| {name} | {s['n']} | {_pct(s['hit_at_1'])} | {_pct(s[f'hit_at_{top_k}'])} | {s['mrr']:.3f} | "
            f"{_pct(s['image_hit'])} | {_pct(s['answer_in_context'])} | "
            + (f"{_pct(s['answer_score'])} | {s['correct']}/{s['partial']}/{s['incorrect']} | {s['refused']} |"
               if s["graded"] else "- | - | - |"))
    lines += [
        "",
        "지표 설명: Hit@k = 질문을 만든 원본 청크가 top-k 안에 있는 비율, 같은 이미지 Hit = 같은 이미지의 "
        "다른 청크라도 top-k에 들어온 비율, 정답 문자열 컨텍스트 포함 = 모범답안 문자열이 top-k 청크 본문에 "
        "그대로 들어 있는 비율(LLM 없이 계산하는 상한 지표), 답변 점수 = LLM 채점(정답 1 / 부분 0.5 / 오답 0) 평균.",
        "",
        "## 실패·부분 정답 문항" if usage else f"## 원본 청크가 top-{top_k}에 못 들어온 문항",
        "",
    ]
    failures = [r for r in results if (r.get("verdict") != "correct" if usage else r["gold_rank"] is None)]
    if not failures:
        lines.append("없음")
    for r in failures:
        lines += [
            f"### {r['id']} ({r['layout_type']}) — {r.get('verdict', '채점 안 됨')}, 원본 청크 순위: "
            f"{r['gold_rank'] or f'top-{top_k} 밖'}",
            f"- 질문: {r['question']}",
            f"- 모범답안: {r['ground_truth']}",
        ]
        if usage:
            lines += [f"- 시스템 답변: {r.get('answer', '').replace(chr(10), ' ')[:400]}",
                      f"- 채점 사유: {r.get('reason', '')}"]
        else:
            lines.append("- 검색된 청크: " + ", ".join(f"{h['chunk_id']}({h['similarity']})" for h in r["retrieved"]))
        lines.append("")
    if errors:
        lines += ["## 실행 오류", ""] + [f"- {e['id']}: {e['error']}" for e in errors] + [""]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--store", help="Test store name (default: the store recorded in the dataset)")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int, default=HARD_MAX_QUESTIONS)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--model", help="OpenAI model (default: OPENAI_MODEL from backend/.env)")
    parser.add_argument("--retrieval-only", action="store_true",
                        help="Score retrieval only; skip LLM answer generation and judging (no API cost)")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.top_k < 1:
        parser.error("--limit and --top-k must be positive")
    limit = min(args.limit, HARD_MAX_QUESTIONS)

    dataset = args.dataset if args.dataset.is_absolute() else PROJECT_ROOT / args.dataset
    with dataset.open(encoding="utf-8-sig", newline="") as handle:
        questions = list(csv.DictReader(handle))[:limit]
    if not questions:
        print(json.dumps({"status": "empty_dataset", "dataset": str(dataset)}, ensure_ascii=False))
        return 1
    store_name = args.store or questions[0]["store"]
    store = LocalVectorStore.open(store_name)
    model_name, embedder = load_embedder(store.manifest.get("embedding_model"))
    system_prompt = load_system_prompt()
    # Answer + judge per question, plus retry headroom: at most 3 requests per question.
    llm = None if args.retrieval_only else BudgetedLLM(model=args.model, max_calls=len(questions) * 3)
    started_at = datetime.now().isoformat(timespec="seconds")

    vectors = embed_texts(embedder, [q["question"] for q in questions])
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for q, vector in zip(questions, vectors):
        hits = store.search(vector, top_k=args.top_k)
        gold_rank = next((h["rank"] for h in hits if h["chunk_id"] == q["chunk_id"]), None)
        result: dict[str, Any] = {
            **{k: q[k] for k in ("id", "question", "ground_truth", "layout_type", "chunk_id")},
            "gold_rank": gold_rank,
            "image_hit": any(h["provenance"]["image_sha256"] == q["image_sha256"] for h in hits),
            "answer_in_context": answer_in_context(q["ground_truth"], hits),
            "retrieved": [{"chunk_id": h["chunk_id"], "similarity": round(h["similarity"], 4)} for h in hits],
        }
        if llm is None:
            results.append(result)
            continue
        try:
            messages = build_messages(system_prompt, q["question"], [as_backend_row(h) for h in hits], 500)
            result["answer"] = llm.chat(messages, temperature=0.1, max_tokens=700)
            result["refused"] = NOT_FOUND in result["answer"]
            result.update(judge(llm, q["question"], q["ground_truth"], result["answer"]))
        except (BudgetExceeded, FatalLLMError) as exc:
            errors.append({"id": q["id"], "error": f"{type(exc).__name__}: {exc} (run aborted)"})
            results.append(result)
            break
        except Exception as exc:
            errors.append({"id": q["id"], "error": f"{type(exc).__name__}: {exc}"})
        results.append(result)

    summary = summarize(results, args.top_k)
    meta = {
        "started_at": started_at, "store": store_name, "embedding_model": model_name,
        "dataset": _relative(dataset),
        "questions": len(results), "llm_usage": llm.usage() if llm else None,
        "generator": questions[0].get("generator_model") or "unknown",
        "synthetic_questions": any(q.get("synthetic") == "true" for q in questions),
    }
    report = args.report if args.report.is_absolute() else PROJECT_ROOT / args.report
    report.write_text(render_report(meta, summary, results, errors, args.top_k), encoding="utf-8")
    details = report.with_suffix(".json")
    details.write_text(json.dumps({"meta": meta, "summary": summary, "results": results, "errors": errors},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok" if not errors else "completed_with_errors", "report": _relative(report),
                      "details": _relative(details),
                      "overall": summary["overall"], "errors": errors,
                      "llm_usage": llm.usage() if llm else None},
                     ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
