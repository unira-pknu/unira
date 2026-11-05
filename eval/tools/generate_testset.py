"""Generate a question/reference-answer testset from the layout TEST store.

Reads chunks already ingested by ``scripts.vectorization.ingest_layout_data`` (so every
question is traceable to a stored chunk), samples at most 50 of them
round-robin across layout types, and asks the LLM for one question + reference
answer per chunk. Writes ``eval/results/layout_eval_dataset.csv``.

Example:
  python -m eval.tools.generate_testset --limit 50
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.tools.llm_budget import BudgetedLLM, BudgetExceeded, FatalLLMError  # noqa: E402
from scripts.vectorization.layout_rag import LocalVectorStore, _relative, default_store_name  # noqa: E402

HARD_MAX_QUESTIONS = 50
MIN_CONTENT_CHARS = 30
DEFAULT_OUTPUT = PROJECT_ROOT / "eval" / "results" / "layout_eval_dataset.csv"
CSV_FIELDS = ["id", "question", "ground_truth", "layout_type", "chunk_id", "source_path",
              "image_sha256", "synthetic", "store", "generator_model"]

SYSTEM_PROMPT = """너는 부경대학교 학생 질의응답 RAG 시스템의 평가용 테스트셋을 만드는 도우미다.
주어진 자료(이미지에서 추출한 본문/표/도식)만 보고 답할 수 있는 질문 1개와 모범답안을 만든다.
규칙:
- 학생이 실제로 챗봇에 물어볼 법한 자연스러운 한국어 질문으로 쓴다. "이 표", "위 자료" 같은 지시어는 쓰지 않는다.
- 표 자료면 특정 행/열의 값을 묻고, 도식 자료면 단계 순서나 관계를 묻는다.
- 모범답안은 자료에 있는 사실만으로 1~2문장으로 쓴다.
- 자료가 너무 빈약해 의미 있는 질문을 만들 수 없으면 question을 빈 문자열로 둔다.
JSON 객체 {"question": "...", "answer": "..."} 만 출력한다."""


def select_chunks(records: list[dict], limit: int) -> list[dict]:
    """Round-robin across layout types, preferring one chunk per image."""
    by_layout: dict[str, list[dict]] = defaultdict(list)
    seen_images: set[str] = set()
    deferred: list[dict] = []
    for record in records:
        if len(record.get("content", "")) < MIN_CONTENT_CHARS:
            continue
        digest = record["provenance"]["image_sha256"]
        if digest in seen_images:
            deferred.append(record)
            continue
        seen_images.add(digest)
        by_layout[record["layout_type"]].append(record)
    queues = [by_layout[k] for k in sorted(by_layout)]
    selected: list[dict] = []
    while len(selected) < limit and any(queues):
        for queue in queues:
            if queue and len(selected) < limit:
                selected.append(queue.pop(0))
    for record in deferred:
        if len(selected) >= limit:
            break
        selected.append(record)
    return selected


def generate(llm: BudgetedLLM, record: dict) -> dict[str, str]:
    label = {"table": "표(마크다운)", "diagram": "도식(서술형 변환)", "prose": "본문"}.get(record["layout_type"], "자료")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"자료 유형: {label}\n\n자료:\n{record['content']}"},
    ]
    result = llm.chat_json(messages, temperature=0.3, max_tokens=300)
    return {"question": str(result.get("question") or "").strip(),
            "answer": str(result.get("answer") or "").strip()}


_SYNTHETIC_TAG = re.compile(r"^\[합성 데이터\]\s*")
_DIAGRAM_TITLE = re.compile(r"이 도식은 '(.+?)'에 관한 내용이다\.")
_DIAGRAM_STEP = re.compile(r"'([^']+)' 다음 단계는 '([^']+)'이다\.")


def _pick(items: list, key: str) -> Any:
    return items[int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(items)]


def _markdown_cells(line: str) -> list[str]:
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [c.strip().replace("\\|", "|") for c in cells]


def rule_based_qa(record: dict) -> dict[str, str] | None:
    """Deterministic, LLM-free QA for structured chunks (tables and diagrams)."""
    content = record["content"]
    if record["layout_type"] == "table":
        lines = content.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("|")), None)
        if start is None or len(lines) < start + 3:
            return None
        caption = _SYNTHETIC_TAG.sub("", " ".join(lines[:start])).strip()
        header = _markdown_cells(lines[start])
        body = [_markdown_cells(line) for line in lines[start + 2:] if line.startswith("|")]
        candidates = [(row, j) for row in body if row[0] for j in range(1, min(len(row), len(header)))
                      if row[j] and header[j] and row[j] != row[0]]
        if not candidates:
            return None
        row, j = _pick(candidates, record["chunk_id"])
        subject = f"{caption}에서 " if caption else ""
        return {"question": f"{subject}{row[0]}의 {header[j]}은(는) 무엇인가요?", "answer": row[j]}
    if record["layout_type"] == "diagram":
        steps = _DIAGRAM_STEP.findall(content)
        if not steps:
            return None
        title = _DIAGRAM_TITLE.search(content)
        topic = _SYNTHETIC_TAG.sub("", title.group(1)).strip() if title else ""
        # A branching node has several valid answers; only ask about single-successor steps.
        successors: dict[str, list[str]] = defaultdict(list)
        for src, dst in steps:
            successors[src].append(dst)
        unambiguous = [(src, dsts[0]) for src, dsts in successors.items() if len(dsts) == 1]
        if not unambiguous:
            return None
        src, dst = _pick(unambiguous, record["chunk_id"])
        subject = f"{topic}에서 " if topic else ""
        return {"question": f"{subject}'{src}' 다음 단계는 무엇인가요?", "answer": dst}
    return None  # prose needs the LLM mode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", default=default_store_name())
    parser.add_argument("--limit", type=int, default=HARD_MAX_QUESTIONS,
                        help=f"Max questions (capped at {HARD_MAX_QUESTIONS})")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", help="OpenAI model (default: OPENAI_MODEL from backend/.env)")
    parser.add_argument("--mode", choices=("llm", "rule"), default="llm",
                        help="llm: LLM writes QA (all layouts). rule: deterministic, no API cost "
                             "(tables and diagrams only)")
    parser.add_argument("--dry-run", action="store_true", help="Show the selected chunks; no LLM calls")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    limit = min(args.limit, HARD_MAX_QUESTIONS)

    store = LocalVectorStore.open(args.store)
    records = store.records
    if args.mode == "rule":
        records = [r for r in records if r["layout_type"] in {"table", "diagram"}]
    selected = select_chunks(records, limit)
    if args.dry_run:
        print(json.dumps({"store": args.store, "selected": [
            {"chunk_id": r["chunk_id"], "layout_type": r["layout_type"], "content": r["content"][:80]}
            for r in selected]}, ensure_ascii=False, indent=2))
        return 0

    # One request per question plus retry headroom, never more than 2x.
    llm = BudgetedLLM(model=args.model, max_calls=len(selected) * 2) if args.mode == "llm" else None
    generator = llm.model if llm else "rule_based"
    rows: list[dict] = []
    errors: list[dict] = []
    for record in selected:
        try:
            qa = generate(llm, record) if llm else rule_based_qa(record) or {"question": "", "answer": ""}
        except (BudgetExceeded, FatalLLMError) as exc:
            errors.append({"chunk_id": record["chunk_id"], "error": f"{type(exc).__name__}: {exc} (run aborted)"})
            break
        except Exception as exc:
            errors.append({"chunk_id": record["chunk_id"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not qa["question"] or not qa["answer"]:
            errors.append({"chunk_id": record["chunk_id"], "error": "EMPTY_QUESTION_OR_ANSWER"})
            continue
        prov = record["provenance"]
        rows.append({
            "id": f"layout_{len(rows) + 1:03d}",
            "question": qa["question"],
            "ground_truth": qa["answer"],
            "layout_type": record["layout_type"],
            "chunk_id": record["chunk_id"],
            "source_path": prov["source_path"],
            "image_sha256": prov["image_sha256"],
            "synthetic": str(bool(prov.get("synthetic"))).lower(),
            "store": args.store,
            "generator_model": generator,
        })

    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:  # BOM so Excel reads Korean
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"status": "ok" if rows else "empty", "store": args.store,
                      "output": _relative(output),
                      "mode": args.mode, "questions": len(rows), "errors": errors,
                      "llm_usage": llm.usage() if llm else None},
                     ensure_ascii=False, indent=2))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
