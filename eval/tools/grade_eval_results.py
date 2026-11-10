"""LLM-judge qualitative grading for eval_ask.py result files.

Checks each generated answer against its structured
`required_facts` (covered/partial/missing) and `forbidden_claims`
(violated/ok) using an LLM judge, since keyword matching alone can't tell
whether an answer actually conveys a fact when `keywords` is empty (common
in this dataset) or phrases it differently than the label. Separately checks
claim support against captured citation bodies, over-refusal and scope errors.

Usage:
    python -m eval.tools.grade_eval_results --results eval/results/regression_category2_fix_20260915.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from .provenance import backend_configurations, code_state, file_sha256, timestamp
from .quality import JUDGE_CONTRACT_VERSION, JUDGE_RULES, answer_units, quality_summary, source_context, validate_judgment

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / "backend" / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

JUDGE_SYSTEM_PROMPT = (
    "당신은 대학 RAG 챗봇의 답변 품질을 채점하는 엄격하지만 공정한 평가자입니다. "
    "질문, 실제 답변, 정답 체크리스트, 금지된 주장 및 실제 인용 출처 본문이 주어집니다.\n\n"
    "각 required_fact에 대해 답변이 그 사실을 얼마나 반영했는지 판정하세요:\n"
    "- covered: 사실이 정확하게 답변에 포함됨\n"
    "- partial: 관련은 있지만 불완전하거나 모호하게 언급됨\n"
    "- missing: 답변에 전혀 언급되지 않음\n\n"
    "사실과 다른 값을 말한 경우도 missing입니다. facts에는 violated를 쓰지 마세요.\n\n"
    "각 forbidden_claim에 대해 답변이 실제로 그 주장을 '사실인 것처럼 확정 제시'하고 있는지 판정하세요. "
    "세 가지 중 하나로만 답하세요:\n"
    "- violated: 답변이 금지된 주장을 별다른 단서 없이 확정된 사실처럼 제시함\n"
    "- hedged: 답변이 그 주장과 관련된 구체적 정보(예: 특정 학기의 날짜·금액)를 언급하긴 하지만, "
    "그것이 특정 시점의 예시일 뿐이며 최신 확인이 필요하다는 점을 명시적으로 함께 밝힘. "
    "완벽하진 않지만 사용자를 오도하지 않으려는 시도가 분명한 경우\n"
    "- ok: 답변이 그 주장을 아예 하지 않거나, 명시적으로 부인/거절함\n\n"
    "판정 시 반드시 지켜야 할 원칙:\n"
    "1. 답변이 '확인할 수 없다', '예측할 수 없다', '자료에 없다', '알려진 바 없다'처럼 명시적으로 "
    "그 사실을 모른다고 인정하거나 요청을 거절하는 경우, forbidden_claim의 주제를 답변 문장 안에서 "
    "언급했다는 이유만으로 violated로 판정하지 마세요. 실제로 그 주장을 '사실이다'라고 단정했을 때만 "
    "violated입니다. 정직한 거절/모름 인정은 항상 ok입니다.\n"
    "2. 답변이 특정 연도·학기의 과거 값(날짜, 금액 등)을 예시로 들면서 '학기마다 다르다', '최신 공지를 "
    "확인해야 한다', '변동될 수 있다' 등 명확한 단서를 붙였다면, 그 부분은 violated가 아니라 hedged로 "
    "판정하세요. 단서 없이 값만 단독으로 확정 제시한 경우에만 violated입니다.\n"
    "3. 표현이 다르더라도 같은 의미를 전달하면 covered/violated로 판정하세요.\n"
    "4. violated 또는 hedged로 판정하려면, 그 주장을 하고 있는 답변 속 문장을 evidence에 "
    "**답변에 적힌 그대로 한 글자도 바꾸지 말고** 인용하세요. 인용할 문장이 없으면 ok입니다.\n"
    "5. evidence를 적은 뒤, 그 문장이 금지된 주장과 '같은 의미'인지 다시 확인하세요. 답변이 금지된 "
    "주장과 반대되는 내용(예: 금지 주장 'X가 가능하다고 안내'인데 답변은 'X는 불가능하다')이거나, "
    "같은 주제의 다른 내용(예: 조건을 구분해 설명, 다른 부서 번호 안내)을 말하는 것이라면 ok입니다. "
    "금지된 주장과 주제가 겹친다는 이유만으로 violated로 판정하지 마세요.\n\n"
    "facts에는 모든 fact ID를, forbidden에는 모든 0-based index를 중복 없이 반환하세요.\n"
    "forbidden 항목에는 index, verdict, evidence, reason을 넣으세요. "
    "evidence는 violated/hedged일 때 답변 원문, ok일 때 빈 문자열입니다.\n"
)
JUDGE_SYSTEM_PROMPT += JUDGE_RULES


def build_user_prompt(case: dict[str, Any]) -> str:
    facts = case.get("required_facts") or []
    forbidden = case.get("forbidden_claims") or []
    facts_block = "\n".join(
        f"{i + 1}. [{fact['id']}] {fact['description']}"
        + (f" (keywords: {', '.join(fact['keywords'])})" if fact.get("keywords") else "")
        for i, fact in enumerate(facts)
    ) or "(없음)"
    forbidden_block = "\n".join(f"{i}. {claim}" for i, claim in enumerate(forbidden)) or "(없음)"
    return (
        f"질문: {case.get('question', '')}\n\n"
        f"생성된 답변:\n{case.get('answer', '')}\n\n"
        f"고정 답변 단위 answer_units(JSON; 모든 ID를 claims에서 한 번씩 판정):\n"
        f"{json.dumps(answer_units(case.get('answer') or ''), ensure_ascii=False)}\n\n"
        f"필수 사실 체크리스트:\n{facts_block}\n\n"
        f"금지된 주장:\n{forbidden_block}"
        f"\n\n정답 라벨상 답변 가능: {case.get('answerable')}"
        f"\n기대 행동: {case.get('expected_behavior', '')}"
        f"\n기대 출처: {json.dumps(case.get('expected_source'), ensure_ascii=False)}"
        f"\n실제 API 답변 가능 표시: {case.get('response_answerable')}"
        f"\n\n실제 인용 출처와 평가 시각(JSON 데이터):\n{json.dumps(source_context(case), ensure_ascii=False)}"
    )


def grade_case(
    client: OpenAI, model: str, case: dict[str, Any], timeout: float, *, schema_retries: int = 1,
) -> dict[str, Any]:
    forbidden = case.get("forbidden_claims") or []
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(case)},
    ]
    errors = []
    for attempt in range(schema_retries + 1):
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=0.0, max_tokens=4096,
            timeout=timeout, response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "{}").strip()
        try:
            parsed = validate_judgment(json.loads(raw), case, require_unit_ids=True)
        except (ValueError, TypeError, KeyError) as exc:
            kind = "invalid_json" if isinstance(exc, json.JSONDecodeError) else "invalid_schema"
            errors.append({"attempt": attempt + 1, "kind": kind, "reason": str(exc), "raw": raw})
            log.warning("judge validation failed id=%s attempt=%d: %s", case.get("id"), attempt + 1, exc)
            if attempt == schema_retries:
                return {"facts": [], "forbidden": [], "judge_error": kind, "reason": str(exc),
                        "judge_raw": raw, "schema_errors": errors, "judge_contract_version": JUDGE_CONTRACT_VERSION}
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": (
                    f"JSON 검증 실패: {exc}. 제공된 answer_units의 모든 unit_id를 정확히 한 번씩 쓰고, "
                    "claims에서 text와 citations는 생략하세요. facts는 covered/partial/missing만 쓰며 "
                    "covered/partial에는 실제 answer_unit_ids를 붙이세요. 원문·출처·질문은 변경하지 말고 "
                    "여섯 필수 키를 갖춘 완전한 채점 JSON을 다시 반환하세요."
                )},
            ]
            continue
        parsed.update(judge_contract_version=JUDGE_CONTRACT_VERSION, schema_repair_count=attempt,
                      schema_errors=errors, judge_raw=raw)
        verify_forbidden_evidence(parsed["forbidden"], case.get("answer") or "")
        confirm_forbidden_violations(client, model, parsed["forbidden"], forbidden, timeout)
        return parsed
    raise ValueError("schema_retries must be non-negative")


CONFIRM_SYSTEM_PROMPT = (
    "당신은 한 문장이 특정 주장을 하고 있는지만 판단합니다. "
    "'금지된 주장'은 챗봇이 하면 안 되는 잘못된 안내를 설명한 것입니다. "
    "'답변 문장'이 그 잘못된 안내와 같은 의미를 사실로 말하고 있으면 asserts=true, "
    "반대되는 내용이거나(예: 금지된 주장은 'X가 가능하다고 안내', 답변 문장은 'X는 불가능하다'), "
    "같은 주제의 다른 내용이거나, 확인이 필요하다고만 말하면 asserts=false입니다.\n"
    "금지된 주장이 '정답이 아닌 값을 제시'하는 형태일 때를 주의하세요. 예: 금지된 주장 "
    "'기준 평점을 1.75가 아닌 다른 수치로 제시', 답변 문장 '평점 2.00 미만이면 학사경고' → "
    "답변이 1.75가 아닌 값을 제시했으므로 asserts=true. 같은 금지된 주장에 답변 문장이 "
    "'평점 1.75 미만이면 학사경고'라면 asserts=false.\n"
    'JSON {"asserts": true|false, "reason": "<한 문장>"} 으로만 답하세요.'
)


def confirm_forbidden_violations(
    client: OpenAI, model: str, items: list[dict[str, Any]], claims: list[str], timeout: float,
) -> None:
    """Second, narrow pass for each remaining "violated" verdict: one claim,
    one quoted sentence, one yes/no. The combined judge prompt (many facts +
    claims at once) kept flagging answers that state the *opposite* of a
    forbidden claim (e.g. "전공필수 미이수 시 졸업 불가" judged as violating
    "졸업 가능하다고 안내"); a focused question fixes most of those."""
    for item in items:
        if item.get("verdict") != "violated":
            continue
        index = item.get("index")
        if not isinstance(index, int) or not 0 <= index < len(claims):
            continue
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": CONFIRM_SYSTEM_PROMPT},
                    {"role": "user", "content": f"금지된 주장: {claims[index]}\n\n답변 문장: {item.get('evidence', '')}"},
                ],
                temperature=0.0,
                max_tokens=150,
                timeout=timeout,
                response_format={"type": "json_object"},
            )
            confirmed = json.loads(response.choices[0].message.content or "{}")
        except Exception:
            log.warning("violation confirm failed for claim %r; keeping verdict", claims[index], exc_info=True)
            continue
        if confirmed.get("asserts") is False:
            item["original_verdict"] = "violated"
            item["verdict"] = "ok"
            item["downgraded"] = "confirm_pass_rejected"
            item["confirm_reason"] = str(confirmed.get("reason") or "")


def _normalize_for_quote(text: str) -> str:
    # Ignore markdown emphasis, citation markers and whitespace so a faithful
    # quote still matches even if the judge dropped "**" or "[1]".
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"[*_`#>\-•]", "", text)
    return re.sub(r"\s+", "", text)


def verify_forbidden_evidence(items: list[dict[str, Any]], answer: str) -> None:
    """Downgrade violated/hedged verdicts whose quoted evidence is not in the
    answer. The judge tended to flag any answer that merely touched a
    forbidden claim's topic (09-16 run: ~13 of 25 "violated" were answers
    saying the opposite or something else); requiring a verbatim quote that
    actually exists in the answer removes the unfounded ones."""
    normalized_answer = _normalize_for_quote(answer)
    for item in items:
        if item.get("verdict") not in {"violated", "hedged"}:
            continue
        evidence = _normalize_for_quote(str(item.get("evidence") or ""))
        if len(evidence) >= 4 and evidence in normalized_answer:
            continue
        item["original_verdict"] = item["verdict"]
        item["verdict"] = "ok"
        item["downgraded"] = "evidence_missing" if not evidence else "evidence_not_in_answer"


def summarize(graded: list[dict[str, Any]]) -> dict[str, Any]:
    total_facts = 0
    covered = partial = missing = 0
    total_forbidden = 0
    violated = 0
    hedged = 0
    cases_with_violation: list[str] = []
    cases_with_hedge: list[str] = []
    cases_fully_covered: list[str] = []
    cases_with_missing: list[str] = []
    graded_case_count = 0

    for row in graded:
        judged = row.get("judged") or {}
        if row.get("error") or row.get("skipped") or judged.get("judge_error") or not judged:
            continue
        facts_judged = judged.get("facts") or []
        forbidden_judged = judged.get("forbidden") or []
        graded_case_count += 1

        case_missing = False
        for fact in facts_judged:
            total_facts += 1
            verdict = fact.get("verdict")
            if verdict == "covered":
                covered += 1
            elif verdict == "partial":
                partial += 1
            else:
                missing += 1
                case_missing = True
        if case_missing:
            cases_with_missing.append(row["id"])
        elif facts_judged and all(fact.get("verdict") == "covered" for fact in facts_judged):
            cases_fully_covered.append(row["id"])

        case_violated = False
        case_hedged = False
        for item in forbidden_judged:
            total_forbidden += 1
            verdict = item.get("verdict")
            if verdict == "violated":
                violated += 1
                case_violated = True
            elif verdict == "hedged":
                hedged += 1
                case_hedged = True
        if case_violated:
            cases_with_violation.append(row["id"])
        elif case_hedged:
            cases_with_hedge.append(row["id"])

    fact_coverage = (covered + 0.5 * partial) / total_facts if total_facts else None
    labeled = [row for row in graded if type(row.get("answerable")) is bool]
    evaluated = [row for row in labeled if row.get("judged") and not row["judged"].get("judge_error")
                 and not row.get("error") and not row.get("skipped")]
    correct_ids = []
    for row in evaluated:
        judged = row["judged"]
        correct = all(fact.get("verdict") == "covered" for fact in judged.get("facts", []))
        correct = correct and all(item.get("verdict") != "violated" for item in judged.get("forbidden", []))
        correct = correct and not any(item.get("downgraded") in {"evidence_missing", "evidence_not_in_answer"}
                                      for item in judged.get("forbidden", []))
        refusal = judged.get("refusal", {}).get("verdict")
        correct = correct and (refusal in {"not_refusal", "appropriate"} if row["answerable"] else refusal == "appropriate")
        if correct:
            correct_ids.append(row["id"])
    return {
        "total_case_count": len(graded),
        "graded_case_count": graded_case_count,
        "api_error_case_ids": [row["id"] for row in graded if row.get("error")],
        "judge_error_case_ids": [row["id"] for row in graded if (row.get("judged") or {}).get("judge_error")],
        "judge_error_reasons": {
            row["id"]: {"kind": (row.get("judged") or {}).get("judge_error"),
                        "reason": (row.get("judged") or {}).get("reason")}
            for row in graded if (row.get("judged") or {}).get("judge_error")
        },
        "schema_repaired_case_ids": [row["id"] for row in graded if (row.get("judged") or {}).get("schema_repair_count", 0) > 0],
        "skipped_case_ids": [row["id"] for row in graded if row.get("skipped")],
        "unverified_forbidden_case_ids": [row["id"] for row in evaluated if any(
            item.get("downgraded") in {"evidence_missing", "evidence_not_in_answer"}
            for item in row["judged"].get("forbidden", []))],
        "answer_accuracy": {
            "labeled_cases": len(labeled), "evaluated_cases": len(evaluated), "correct_cases": len(correct_ids),
            "strict_rate_all_labeled": round(len(correct_ids) / len(labeled), 4) if labeled else None,
            "rate_evaluated_only": round(len(correct_ids) / len(evaluated), 4) if evaluated else None,
            "correct_case_ids": correct_ids,
        },
        "quality": quality_summary(graded),
        "by_category": {category: quality_summary([row for row in graded if row.get("category", "uncategorized") == category])
                        for category in sorted({row.get("category", "uncategorized") for row in graded})},
        "total_facts": total_facts,
        "facts_covered": covered,
        "facts_partial": partial,
        "facts_missing": missing,
        "fact_coverage_score": round(fact_coverage, 4) if fact_coverage is not None else None,
        "total_forbidden_claims": total_forbidden,
        "forbidden_claims_violated": violated,
        "forbidden_claims_hedged": hedged,
        "cases_with_any_missing_fact": cases_with_missing,
        "cases_fully_covered": cases_fully_covered,
        "cases_with_forbidden_violation": cases_with_violation,
        "cases_with_hedge_only": cases_with_hedge,
    }


def run(
    results_path: Path, output_path: Path, model: str, timeout: float,
    limit: int | None, sleep_seconds: float, schema_retries: int = 1,
) -> dict[str, Any]:
    if results_path.resolve() == output_path.resolve():
        raise ValueError("graded output must differ from input results")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    results_hash = file_sha256(results_path)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY not set (checked backend/.env)")
    client = OpenAI(api_key=api_key)

    cases: list[dict[str, Any]] = []
    with results_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    if limit is not None:
        cases = cases[:limit]

    graded: list[dict[str, Any]] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for i, case in enumerate(cases, start=1):
            case_id = case.get("id", f"row_{i}")
            if case.get("error") or not (case.get("answer") or "").strip():
                log.info("[%d/%d] %s SKIP (no answer / error)", i, len(cases), case_id)
                row = {**case, "judged": {"facts": [], "forbidden": []}, "skipped": "no_answer"}
            else:
                try:
                    judged = grade_case(client, model, case, timeout, schema_retries=schema_retries)
                except Exception as exc:
                    log.exception("[%d/%d] %s grading failed", i, len(cases), case_id)
                    judged = {"facts": [], "forbidden": [], "judge_error": "exception", "reason": type(exc).__name__}
                row = {**case, "judged": judged}
                log.info("[%d/%d] %s %s facts=%d forbidden=%d", i, len(cases), case_id,
                         "JUDGE_ERROR" if judged.get("judge_error") else "OK",
                         len(judged.get("facts") or []), len(judged.get("forbidden") or []))
                if sleep_seconds:
                    time.sleep(sleep_seconds)
            # Keep the original evidence and labels so every judgment is auditable.
            graded.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()

    summary = summarize(graded)
    manifest_path = results_path.with_suffix(".manifest.json")
    summary["provenance"] = {
        "schema_version": 2, "judged_at": timestamp(), "judge_model": model,
        "judge_prompt_sha256": hashlib.sha256(JUDGE_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "results_sha256": results_hash, "graded_sha256": file_sha256(output_path),
        "run_manifest": json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None,
        "limit": limit,
        "backend_configurations": backend_configurations(graded),
        "judge_contract_version": JUDGE_CONTRACT_VERSION,
        "schema_retries": schema_retries,
        "judge_code": code_state(),
    }
    summary_path = output_path.with_suffix(".summary.json")
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    log.info("=" * 60)
    log.info(
        "graded_cases=%d fact_coverage=%s (covered=%d partial=%d missing=%d / total=%d) "
        "forbidden_violations=%d hedged=%d ok=%d (total=%d)",
        summary["graded_case_count"],
        summary["fact_coverage_score"],
        summary["facts_covered"],
        summary["facts_partial"],
        summary["facts_missing"],
        summary["total_facts"],
        summary["forbidden_claims_violated"],
        summary["forbidden_claims_hedged"],
        summary["total_forbidden_claims"] - summary["forbidden_claims_violated"] - summary["forbidden_claims_hedged"],
        summary["total_forbidden_claims"],
    )
    log.info("saved details=%s summary=%s", output_path, summary_path)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LLM-judge grading for eval_ask.py results.")
    parser.add_argument("--results", type=Path, required=True, help="eval_ask.py output JSONL to grade.")
    parser.add_argument("--output", type=Path, default=None, help="Output JSONL path (default: <results>.graded.jsonl).")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--schema-retries", type=int, choices=(0, 1, 2), default=1,
                        help="Bounded JSON/schema repair attempts per case (default: 1).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output or args.results.with_suffix(".graded.jsonl")
    summary = run(args.results, output, args.model, args.timeout, args.limit, args.sleep, args.schema_retries)
    if summary["judge_error_case_ids"]:
        log.error("grading incomplete: %d judge errors; summary saved for diagnosis", len(summary["judge_error_case_ids"]))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
