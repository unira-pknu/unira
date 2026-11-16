"""Render feedback JSONL as a local, read-only Markdown review report.

No model calls, DB access or changes to the input. Review decisions still belong
in the original JSONL used by prepare_feedback_training.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import textwrap
from typing import Any

from .build_feedback_training import read_candidates


STATUS_LABELS = {"pending": "검수 대기", "approved": "승인", "rejected": "제외"}
AI_LABELS = {
    "suggested": "수정 답변 제안 있음",
    "needs_context_or_evidence": "맥락 또는 근거 확인 필요",
    "no_allowed_evidence": "사용할 수 있는 근거 없음",
    "generation_failed": "후보 생성 실패",
}


def _text(value: Any) -> str:
    if value is None or value == "" or value == []:
        return "(없음)"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _block(value: Any) -> str:
    """Keep source HTML, images and links inert, including embedded code fences."""
    content = _text(value)
    if isinstance(value, str):
        content = "\n".join(textwrap.fill(line, width=88, break_long_words=False, break_on_hyphens=False,
                                          replace_whitespace=False) for line in content.split("\n"))
    longest = max((len(match.group()) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{content}\n{fence}\n"


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def render_review(
    rows: list[dict[str, Any]], snapshots: list[dict[str, Any]] | None = None,
    *, source: str = "review.jsonl",
) -> str:
    evidence = {}
    for snapshot in snapshots or []:
        candidate_id = snapshot.get("feedback_id")
        if not isinstance(candidate_id, str) or candidate_id in evidence:
            raise ValueError("evidence feedback_id must be a unique string")
        evidence[candidate_id] = snapshot
    counts = Counter(_text(_object(row.get("review")).get("status")) for row in rows)
    lines = [
        "# 피드백 검수 보기\n",
        "보기용 보고서입니다. 승인·정답 수정은 원본 JSONL에 반영해야 합니다. "
        "이 Markdown 파일의 수정 내용은 학습 데이터에 반영되지 않습니다.\n",
        "원본 파일:\n", _block(source),
        f"전체 {len(rows)}건 · 검수 대기 {counts['pending']}건 · 승인 {counts['approved']}건 · 제외 {counts['rejected']}건\n",
    ]
    for number, row in enumerate(rows, 1):
        review = _object(row.get("review"))
        ai = _object(row.get("ai_generation"))
        correction = _object(ai.get("correction"))
        snapshot = evidence.get(row.get("id"), {})
        if snapshot and snapshot.get("original_question_key") != row.get("original_question_key"):
            raise ValueError("evidence question key does not match the feedback")
        sources = snapshot.get("sources") or []
        if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
            raise ValueError("evidence sources must be a list of objects")
        by_id = {item.get("evidence_id"): item for item in sources if isinstance(item.get("evidence_id"), str)}
        status = review.get("status")
        ai_status = ai.get("status")
        lines.extend([
            f"---\n\n## {number}. 피드백\n",
            "### 식별 정보와 상태\n",
            _block({"id": row.get("id"), "검수": STATUS_LABELS.get(str(status), status),
                    "AI 제안": AI_LABELS.get(str(ai_status), ai_status),
                    "질문 연결": row.get("pairing_status"), "피드백 수정 시각": row.get("feedback_updated_at")}),
            "### 질문\n", _block(row.get("question")),
            "### 기존 답변\n", _block(row.get("rejected_answer")),
            "### 사용자 피드백\n", _block({"사유": row.get("reasons") or [], "의견": row.get("comment")}),
            "### 수정 답변\n", _block(review.get("corrected_answer")),
            "### AI 실패 분석\n", _block({"유형": ai.get("failure_type"), "설명": ai.get("failure_analysis"),
                                           "오류 유형": ai.get("error_type")}),
            "### 필수 사실\n", _block(review.get("required_facts")),
            "### 금지할 주장\n", _block(review.get("forbidden_claims")),
            "### 제안된 근거와 원문 인용\n",
        ])
        citations = correction.get("citations") or []
        if not isinstance(citations, list) or any(not isinstance(item, dict) for item in citations):
            raise ValueError("correction citations must be a list of objects")
        if not citations:
            lines.append("제안된 인용이 없습니다.\n")
        for citation_number, citation in enumerate(citations, 1):
            item = by_id.get(str(citation.get("evidence_id")), {})
            lines.extend([
                f"#### 인용 {citation_number}\n",
                _block({"근거 ID": citation.get("evidence_id"), "문서": item.get("title"), "URL": item.get("uri")}),
                "인용문:\n", _block(citation.get("quote")),
            ])
            if item:
                lines.extend(["적용 조건·시점 메타데이터:\n", _block(item.get("metadata")),
                              "사용한 근거 본문:\n", _block(item.get("content"))])
                if item.get("truncated") is True:
                    lines.append("이 본문은 생성 시 길이 제한으로 잘린 발췌입니다. 원문 전체도 확인하세요.\n")
            else:
                lines.append("연결된 근거 본문이 없습니다. 같은 폴더의 evidence.jsonl 또는 --evidence 경로를 확인하세요.\n")
        lines.extend([
            "### 검수 기록\n",
            _block({key: review.get(key) for key in (
                "status", "split", "verified_sources", "privacy_checked", "context_checked", "reviewed_by", "reviewed_at",
            )}),
        ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Original or AI-generated review JSONL")
    parser.add_argument("--output", type=Path, help="New Markdown file; default: input with .md suffix")
    parser.add_argument("--evidence", type=Path, help="Defaults to evidence.jsonl next to input, when present")
    args = parser.parse_args(argv)
    output = args.output or args.input.with_suffix(".md")
    try:
        if output.exists():
            raise ValueError("output already exists; choose a new --output path")
        if output.suffix.lower() != ".md":
            raise ValueError("output must use the .md extension")
        rows = read_candidates(args.input)
        evidence_path = args.evidence or args.input.with_name("evidence.jsonl")
        snapshots = read_candidates(evidence_path) if args.evidence or evidence_path.is_file() else []
        report = render_review(rows, snapshots, source=str(args.input))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(report)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"{exc}\n")
    print(f"items={len(rows)}; report={output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
