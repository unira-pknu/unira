"""Build reviewed chat training pairs and held-out evaluation cases, offline.

No DB writes, model calls, uploads or training jobs. See docs/evaluation/FEEDBACK_TRAINING.md.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from backend.app.feedback_training import question_key
from .eval_ask import _read_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOLDOUTS = [PROJECT_ROOT / "eval" / "cases" / f"{name}.jsonl"
                    for name in ("smoke", "regression", "challenge")]


def source_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("verified_sources must contain HTTP(S) URLs")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("verified_sources must contain HTTP(S) URLs without credentials")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))


def read_candidates(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{number}: expected an object")
            rows.append(row)
    return rows


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _strings(value: Any, field: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise ValueError(f"{field} must be a {'non-empty ' if required else ''}list")
    return [_text(item, field) for item in value]


def build_datasets(
    candidates: list[dict[str, Any]], holdouts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Fail before writing on incomplete approval, conflicting labels or split leakage."""
    assigned: dict[str, str] = {}
    for case in holdouts:
        assigned[f"q:{question_key(case['question'])}"] = "holdout"
        source = case.get("expected_source") or {}
        for url in source.get("allowed_urls") or []:
            assigned[f"s:{source_key(url)}"] = "holdout"

    train, evaluation = [], []
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    skipped = 0
    for row in candidates:
        review = row.get("review")
        if not isinstance(review, dict) or review.get("status") not in ("pending", "rejected", "approved"):
            raise ValueError("every candidate needs review.status: pending, rejected or approved")
        if review["status"] != "approved":
            skipped += 1
            continue
        candidate_id = str(UUID(_text(row.get("id"), "id")))
        if candidate_id in seen_ids:
            raise ValueError(f"duplicate approved feedback id: {candidate_id}")
        seen_ids.add(candidate_id)
        if row.get("schema_version") != 1 or row.get("rating") != "down" or row.get("pairing_status") != "matched":
            raise ValueError(f"{candidate_id}: only matched downvote drafts with schema_version=1 can be approved")
        if review.get("privacy_checked") is not True or review.get("context_checked") is not True:
            raise ValueError(f"{candidate_id}: privacy_checked and context_checked must both be true")
        _text(review.get("reviewed_by"), "reviewed_by")
        reviewed_at = datetime.fromisoformat(_text(review.get("reviewed_at"), "reviewed_at").replace("Z", "+00:00"))
        if reviewed_at.tzinfo is None:
            raise ValueError("reviewed_at must include a timezone")
        split = review.get("split")
        if split not in ("train", "evaluation"):
            raise ValueError(f"{candidate_id}: split must be train or evaluation")
        question = _text(row.get("question"), "question")
        original_key = _text(row.get("original_question_key"), "original_question_key")
        if not re.fullmatch(r"[a-f0-9]{64}", original_key):
            raise ValueError("original_question_key must be the exported SHA-256 key")
        key = question_key(question)
        if not re.sub(r"[\s?!.~…]+", "", question):
            raise ValueError("question must contain more than punctuation")
        answer = _text(review.get("corrected_answer"), "corrected_answer")
        rejected_answer = _text(row.get("rejected_answer"), "rejected_answer")
        if question_key(answer) == question_key(rejected_answer):
            raise ValueError(f"{candidate_id}: corrected_answer still equals the rejected answer")
        sources = _strings(review.get("verified_sources"), "verified_sources", required=True)
        source_keys = {source_key(url) for url in sources}
        tokens = {f"q:{key}", f"q:{original_key}", *(f"s:{url}" for url in source_keys)}
        for token in tokens:
            previous = assigned.get(token)
            if previous and previous != split:
                raise ValueError(f"{candidate_id}: question or source overlaps {previous}; keep related cases in one split")
        if key in seen_questions or original_key in seen_questions:
            raise ValueError(f"{candidate_id}: duplicate approved question; choose one reviewed correction")
        seen_questions.update((key, original_key))
        assigned.update(dict.fromkeys(tokens, split))
        if split == "train":
            # Do not pass raw feedback, reviewer identity or the rejected answer to a model.
            train.append({"messages": [{"role": "user", "content": question},
                                       {"role": "assistant", "content": answer}]})
        else:
            facts = _strings(review.get("required_facts"), "required_facts", required=True)
            forbidden = _strings(review.get("forbidden_claims", []), "forbidden_claims")
            evaluation.append({
                "id": "feedback_" + candidate_id.replace("-", ""),
                "question": question,
                "category": "feedback",
                "answerable": True,
                "expected_source": {"dataset": [], "title_keywords": [],
                                    "allowed_urls": sources, "source_ids": []},
                "required_facts": [{"id": f"fact_{index}", "description": fact, "keywords": []}
                                   for index, fact in enumerate(facts, 1)],
                "forbidden_claims": forbidden,
                "expected_no_info": False,
                "tags": ["negative_feedback"],
                "difficulty": "hard",
                "label_status": "ready",
                "expected_behavior": answer,
            })
    return train, evaluation, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory; existing outputs are never overwritten")
    parser.add_argument("--holdout", type=Path, action="append", default=[],
                        help="Additional evaluation JSONL to exclude, alongside smoke/regression/challenge")
    args = parser.parse_args()
    try:
        holdouts = [case for path in [*DEFAULT_HOLDOUTS, *args.holdout] for case in _read_jsonl(path)]
        train, evaluation, skipped = build_datasets(read_candidates(args.input), holdouts)
        if not train and not evaluation:
            raise ValueError("no approved examples; review drafts before building datasets")
        args.output_dir.mkdir(parents=True, exist_ok=False)
        for filename, rows in (("train.jsonl", train), ("evaluation.jsonl", evaluation)):
            with (args.output_dir / filename).open("x", encoding="utf-8") as stream:
                for row in rows:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"{exc}\n")
    print(f"train={len(train)} evaluation={len(evaluation)} skipped={skipped}; output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
