"""Stage two: prepare a review queue, audit approvals and export eligible datasets.

No automatic approvals, model calls, DB writes or changes to source files.
Rejected/pending/blocked records remain in the queue and decision report.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import UUID

from backend.app.feedback_training import question_key
from .build_feedback_training import DEFAULT_HOLDOUTS, build_datasets, read_candidates, source_key
from .eval_ask import _read_jsonl
from .llm_budget import load_backend_env
from .prepare_feedback_training import fetch_candidates, feedback_endpoint, validate_candidates
from .provenance import file_sha256


EXTRA_HOLDOUT = Path(__file__).resolve().parents[1] / "cases" / "feedback_graduation_20261115.jsonl"
FEEDBACK_FIELDS = (
    "schema_version", "id", "rating", "feedback_updated_at", "pairing_status",
    "original_question_key", "rejected_answer", "answer_sources", "reasons", "comment",
)
SYNTHETIC_KIND = "synthetic_feedback_qa"
REASON_LABELS = {
    "feedback_changed_since_review": "검수 후 원본 피드백 변경",
    "synthetic_generation_anchor_changed": "합성 질문 생성 원본 또는 검수 패키지 변경",
    "synthetic_parent_changed_or_withdrawn": "부모 피드백 변경·취소·삭제",
    "missing_synthetic_generation_anchor": "합성 질문의 생성 원본 없음",
    "feedback_withdrawn_or_deleted": "피드백 취소·삭제 또는 현재 조회에 없음",
    "invalid_review_status": "잘못된 검수 상태",
    "incomplete_or_invalid_approval": "승인 필수 정보 또는 근거 확인 필요",
    "related_group_crosses_train_evaluation": "같은 질문 묶음이 학습·평가에 함께 배정됨",
    "duplicate_approved_question": "중복된 질문을 여러 번 승인함",
    "reserved_evaluation_overlap": "보호된 평가 질문·출처와 중복됨",
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def feedback_fingerprint(row: dict[str, Any]) -> str:
    return digest({field: row.get(field) for field in FEEDBACK_FIELDS})


def _index(rows: list[dict[str, Any]], label: str, key: str = "id") -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        identifier = row.get(key)
        if not isinstance(identifier, str) or not identifier or identifier in result:
            raise ValueError(f"{label}: missing or duplicate {key}")
        if key in {"id", "feedback_id"} and str(UUID(identifier)) != identifier:
            raise ValueError(f"{label}: {key} must be a canonical UUID")
        result[identifier] = row
    return result


def _same_feedback(row: dict[str, Any], current: dict[str, Any]) -> bool:
    return all(field in row and row[field] == current.get(field) for field in FEEDBACK_FIELDS)


def _immutable(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in {"question", "review", "review_pipeline"}}


def package_synthetic(candidate: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """Enrich original generated candidates; never synthesize a downvote label."""
    if (candidate.get("schema_version") != 1 or candidate.get("kind") != SYNTHETIC_KIND
            or candidate.get("synthetic") is not True or "rating" in candidate):
        raise ValueError("invalid original synthetic candidate")
    UUID(candidate["id"])
    if not isinstance(candidate.get("question"), str) or not candidate["question"].strip():
        raise ValueError("synthetic candidate needs a question")
    if candidate.get("parent_question_key") != parent.get("original_question_key"):
        raise ValueError("synthetic parent question key differs from its generation snapshot")
    review = candidate.get("review")
    if not isinstance(review, dict) or review.get("status") != "pending":
        raise ValueError("generation candidates must remain pending; provide approvals with --reviewed")
    row = deepcopy(candidate)
    row.update(
        review_package_version=1, original_question_key=question_key(candidate["question"]),
        candidate_fingerprint=digest(_immutable(candidate)), parent_feedback_fingerprint=feedback_fingerprint(parent),
    )
    row["review"] = {
        **deepcopy(review), "status": "pending", "split": None,
        "corrected_answer": candidate.get("proposed_answer") or "",
        "required_facts": candidate.get("required_facts") or [],
        "forbidden_claims": candidate.get("forbidden_claims") or [],
        "verified_sources": [], "reviewed_by": "", "reviewed_at": "",
        "privacy_checked": False, "context_checked": False,
    }
    return row


def prepare_queue(
    current: list[dict[str, Any]], reviewed: list[dict[str, Any]],
    synthetic: list[dict[str, Any]], parents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    validate_candidates(current)
    fresh = _index(current, "current feedback")
    submitted = _index(reviewed, "reviewed input")
    parent_index = _index(parents, "generation parents")
    _index(synthetic, "generation synthetic candidates")
    queue, problems, anchored = [], defaultdict(list), set()
    for candidate_id, row in fresh.items():
        if candidate_id in submitted:
            row = deepcopy(submitted[candidate_id])
            if row.get("kind") == SYNTHETIC_KIND or not _same_feedback(row, fresh[candidate_id]):
                problems[candidate_id].append("feedback_changed_since_review")
        else:
            row = deepcopy(row)
        queue.append(row)
        anchored.add(candidate_id)
    for candidate in synthetic:
        candidate_id = candidate["id"]
        if candidate_id in anchored:
            raise ValueError("feedback and synthetic IDs must not collide")
        parent = parent_index.get(candidate.get("parent_feedback_id"))
        if parent is None:
            raise ValueError("synthetic candidate has no generation parent snapshot")
        expected = package_synthetic(candidate, parent)
        row = deepcopy(submitted.get(candidate_id, expected))
        if _immutable(row) != _immutable(expected):
            problems[candidate_id].append("synthetic_generation_anchor_changed")
        current_parent = fresh.get(candidate.get("parent_feedback_id"))
        if current_parent is None or not _same_feedback(parent, current_parent):
            problems[candidate_id].append("synthetic_parent_changed_or_withdrawn")
        queue.append(row)
        anchored.add(candidate_id)
    for candidate_id, row in submitted.items():
        if candidate_id not in anchored:
            queue.append(deepcopy(row))
            problems[candidate_id].append(
                "missing_synthetic_generation_anchor" if row.get("kind") == SYNTHETIC_KIND else "feedback_withdrawn_or_deleted"
            )
    return queue, dict(problems)


def validate_synthetic_evidence(row: dict[str, Any], snapshots: dict[str, dict[str, Any]]) -> None:
    snapshot = snapshots.get(row.get("parent_feedback_id"))
    if snapshot is None or snapshot.get("original_question_key") != row.get("parent_question_key"):
        raise ValueError("missing or mismatched parent evidence snapshot")
    sources = snapshot.get("sources")
    if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
        raise ValueError("evidence sources must be objects")
    by_id = _index(sources, "evidence", "evidence_id")
    urls = {source_key(source.get("uri")) for source in sources}
    verified = {source_key(url) for url in row["review"].get("verified_sources", [])}
    if not verified <= urls:
        raise ValueError("verified sources are not in the saved evidence; provide the reviewed document body")
    citations = row.get("citations")
    if not isinstance(citations, list) or not citations:
        raise ValueError("synthetic candidate needs generation citations")
    cited_urls = set()
    for citation in citations:
        if not isinstance(citation, dict) or not isinstance(citation.get("evidence_id"), str):
            raise ValueError("invalid synthetic citation")
        source = by_id.get(citation["evidence_id"])
        quote = citation.get("quote")
        if (source is None or not isinstance(source.get("content"), str) or not isinstance(quote, str)
                or not quote.strip() or quote not in source["content"]):
            raise ValueError("synthetic citation is not an exact quote of its saved evidence")
        if source.get("content_sha256") and source["content_sha256"] != hashlib.sha256(source["content"].encode()).hexdigest():
            raise ValueError("saved evidence content hash changed")
        cited_urls.add(source_key(source["uri"]))
    if not cited_urls & verified:
        raise ValueError("at least one cited source must be reviewed and verified")


def question_tokens(row: dict[str, Any]) -> set[str]:
    return {"q:" + question_key(row["question"]), "q:" + row["original_question_key"]}


def group_tokens(row: dict[str, Any]) -> set[str]:
    tokens = question_tokens(row) | {"s:" + source_key(url) for url in row["review"]["verified_sources"]}
    if row.get("kind") == SYNTHETIC_KIND:
        tokens.add("q:" + row["parent_question_key"])
    return tokens


def audit_and_build(
    queue: list[dict[str, Any]], initial_problems: dict[str, list[str]],
    snapshots: list[dict[str, Any]], holdouts: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence = _index(snapshots, "evidence snapshots", "feedback_id")
    decisions, candidates = {}, {}
    for row in queue:
        candidate_id = row["id"]
        review = row.get("review")
        status = review.get("status") if isinstance(review, dict) else None
        decision = {"id": candidate_id, "kind": row.get("kind") or "feedback", "review_status": status,
                    "requested_split": review.get("split") if isinstance(review, dict) else None,
                    "decision": "pending", "reasons": list(initial_problems.get(candidate_id, []))}
        decisions[candidate_id] = decision
        if status == "rejected":
            decision.update(decision="rejected")
            continue
        if status == "pending":
            continue
        if status != "approved":
            decision.update(decision="blocked")
            decision["reasons"].append("invalid_review_status")
            continue
        if decision["reasons"]:
            decision["decision"] = "blocked"
            continue
        try:
            # Individual approval contract, before global component checks.
            build_datasets([row], [], allow_synthetic=True)
            if row.get("kind") == SYNTHETIC_KIND:
                validate_synthetic_evidence(row, evidence)
        except (ValueError, TypeError, KeyError) as exc:
            decision.update(decision="blocked", validation_error=str(exc))
            decision["reasons"].append("incomplete_or_invalid_approval")
            continue
        candidates[candidate_id] = row

    # Connected components ensure all related questions/documents stay together.
    parent = {key: key for key in candidates}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    token_owner, question_owner = {}, {}
    duplicate_ids = set()
    for candidate_id, row in candidates.items():
        for token in group_tokens(row):
            if token in token_owner:
                parent[find(candidate_id)] = find(token_owner[token])
            else:
                token_owner[token] = candidate_id
        for token in question_tokens(row):
            if token in question_owner and question_owner[token] != candidate_id:
                duplicate_ids.update((candidate_id, question_owner[token]))
            else:
                question_owner[token] = candidate_id
    components = defaultdict(list)
    for candidate_id in candidates:
        components[find(candidate_id)].append(candidate_id)
    reserved = defaultdict(list)
    for case in holdouts:
        item = {"case_id": case.get("id"), "question_key": question_key(case["question"])}
        reserved["q:" + item["question_key"]].append(item)
        for url in (case.get("expected_source") or {}).get("allowed_urls") or []:
            reserved["s:" + source_key(url)].append({"case_id": case.get("id"), "source_url": source_key(url)})
    for ids in components.values():
        reasons, matches = [], []
        if len({candidates[key]["review"]["split"] for key in ids}) > 1:
            reasons.append("related_group_crosses_train_evaluation")
        if duplicate_ids.intersection(ids):
            reasons.append("duplicate_approved_question")
        for candidate_id in ids:
            for token in group_tokens(candidates[candidate_id]):
                matches.extend(reserved.get(token, []))
        if matches:
            reasons.append("reserved_evaluation_overlap")
        for candidate_id in ids:
            decision = decisions[candidate_id]
            decision["group_ids"] = sorted(ids)
            if reasons:
                decision.update(decision="blocked", holdout_matches=list({digest(item): item for item in matches}.values()))
                decision["reasons"].extend(reasons)
            else:
                decision["decision"] = "ready"
    ready = [row for row in queue if decisions[row["id"]]["decision"] == "ready"]
    # This last strict global check remains authoritative, including all holdouts.
    train, evaluation, _ = build_datasets(ready, holdouts, allow_synthetic=True)
    for decision in decisions.values():
        decision["reason_labels"] = [REASON_LABELS.get(reason, reason) for reason in decision["reasons"]]
    return {"ready": ready, "train": train, "evaluation": evaluation, "decisions": list(decisions.values()),
            "needs_review": [row for row in queue if decisions[row["id"]]["decision"] not in {"ready", "rejected"}]}


def write_package(output: Path, files: dict[str, list[dict[str, Any]]], summary: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".feedback-stage2-", dir=output.parent) as directory:
        staged = Path(directory) / "package"
        staged.mkdir()
        for name, rows in files.items():
            with (staged / name).open("x", encoding="utf-8") as stream:
                for row in rows:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        (staged / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = ["# 검수·변환 처리 결과\n", "승인 기록은 원본 그대로 보존합니다. blocked 항목은 데이터셋에서 제외했으며 사유를 아래에 기록합니다.\n",
                 "최신 서버 상태 대조: " + ("완료\n" if summary["freshness_checked"] else "미완료 — 오프라인 스냅샷 검사 결과입니다. 실시간 대조를 다시 수행하세요.\n"),
                 f"학습 {summary['train']}건 · 평가 {summary['evaluation']}건 · 추가 확인 {summary['needs_review']}건\n",
                 "| ID | 종류 | 처리 | 사유 |", "| --- | --- | --- | --- |"]
        for item in files["decisions.jsonl"]:
            kind = "합성 질문" if item["kind"] == SYNTHETIC_KIND else "실제 피드백"
            lines.append(f"| {item['id']} | {kind} | {item['decision']} | {', '.join(item['reason_labels']) or '-'} |")
        (staged / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        staged.rename(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base-url", help="Read current downvotes from the admin API")
    source.add_argument("--current-candidates", type=Path, help="Offline unreviewed snapshot; cannot verify current server state")
    parser.add_argument("--reviewed", type=Path, action="append", default=[], help="Reviewed feedback or packaged synthetic JSONL; repeat for separate files")
    parser.add_argument("--generation-dir", type=Path, help="Original stage-one directory with review, synthetic_candidates and evidence JSONL")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory; never overwritten")
    parser.add_argument("--holdout", type=Path, action="append", default=[])
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(argv)
    try:
        if args.output_dir.exists():
            raise ValueError("output-dir already exists; choose a new directory")
        inputs = [*args.reviewed]
        reviewed = [row for path in args.reviewed for row in read_candidates(path)]
        _index(reviewed, "reviewed input")
        synthetic, parents, snapshots = [], [], []
        if args.generation_dir:
            paths = {name: args.generation_dir / name for name in ("review.jsonl", "synthetic_candidates.jsonl", "evidence.jsonl")}
            parents = read_candidates(paths["review.jsonl"])
            synthetic = read_candidates(paths["synthetic_candidates.jsonl"])
            snapshots = read_candidates(paths["evidence.jsonl"])
            inputs.extend(paths.values())
        holdout_paths = [*DEFAULT_HOLDOUTS, EXTRA_HOLDOUT, *args.holdout]
        holdouts = [case for path in holdout_paths for case in _read_jsonl(path)]
        if args.base_url:
            feedback_endpoint(args.base_url)
            load_backend_env()
            token = os.getenv("FEEDBACK_API_TOKEN") or getpass("Admin access_token (hidden): ")
            current = fetch_candidates(args.base_url, token, timeout=args.timeout)
        else:
            current = read_candidates(args.current_candidates)
            inputs.append(args.current_candidates)
        queue, problems = prepare_queue(current, reviewed, synthetic, parents)
        result = audit_and_build(queue, problems, snapshots, holdouts)
        decisions_by_id = {item["id"]: item for item in result["decisions"]}
        for row in queue:
            row["review_pipeline"] = deepcopy(decisions_by_id[row["id"]])
        decision_counts = Counter(item["decision"] for item in result["decisions"])
        summary = {
            "schema_version": 2, "created_at": datetime.now(timezone.utc).isoformat(),
            "freshness_checked": bool(args.base_url), "dataset_built": bool(result["ready"]),
            "queue": len(queue), "feedback": sum(row.get("kind") != SYNTHETIC_KIND for row in queue),
            "synthetic": sum(row.get("kind") == SYNTHETIC_KIND for row in queue),
            "train": len(result["train"]), "evaluation": len(result["evaluation"]),
            "needs_review": len(result["needs_review"]), "decisions": dict(decision_counts),
            "source_sha256": [{"file": path.name, "sha256": file_sha256(path)} for path in inputs],
            "current_candidates_sha256": digest(current),
            "holdouts": [{"file": path.name, "sha256": file_sha256(path)} for path in holdout_paths],
            "model_calls": 0, "database_writes": 0,
        }
        files = {"review_queue.jsonl": queue, "needs_review.jsonl": result["needs_review"],
                 "ready.jsonl": result["ready"], "decisions.jsonl": result["decisions"],
                 "current_candidates.jsonl": current, "evidence.jsonl": snapshots}
        if result["ready"]:
            files.update({"train.jsonl": result["train"], "evaluation.jsonl": result["evaluation"]})
        write_package(args.output_dir, files, summary)
    except (ValueError, OSError, EOFError, KeyError, TypeError) as exc:
        parser.exit(2, f"{exc}\n")
    print(f"queue={len(queue)} train={summary['train']} evaluation={summary['evaluation']} "
          f"blocked={decision_counts['blocked']} pending={decision_counts['pending']}; output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
