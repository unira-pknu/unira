"""Safety boundaries for offline promotion of negative feedback into training data."""

from copy import deepcopy
import json
import sys

import pytest

from backend.app.feedback_training import question_key
from eval.tools import build_feedback_training as training
from eval.tools.eval_ask import _read_jsonl


def approved(*, number=1, question="전공필수 이수 조건?", split="train", source="https://example.edu/rules/1"):
    return {
        "schema_version": 1, "id": f"00000000-0000-0000-0000-{number:012d}",
        "rating": "down", "pairing_status": "matched", "question": question,
        "original_question_key": question_key(question), "rejected_answer": "잘못된 기존 답변",
        "comment": "사용자 의견", "reasons": ["부정확해요"],
        "review": {"status": "approved", "split": split, "corrected_answer": "검수한 올바른 답변",
                   "verified_sources": [source], "privacy_checked": True, "context_checked": True,
                   "reviewed_by": "reviewer", "reviewed_at": "2026-11-11T18:00:00+09:00",
                   "required_facts": ["필수 조건을 설명한다"], "forbidden_claims": ["조건 없이 가능하다"]},
    }


def test_only_corrections_become_training_messages():
    row = approved()
    pending, rejected = deepcopy(row), deepcopy(row)
    pending["review"]["status"] = "pending"
    rejected["review"]["status"] = "rejected"
    train, evaluation, skipped = training.build_datasets([pending, row, rejected], [])
    assert train == [{"messages": [{"role": "user", "content": row["question"]},
                                   {"role": "assistant", "content": "검수한 올바른 답변"}]}]
    assert evaluation == []
    assert skipped == 2
    assert "사용자 의견" not in json.dumps(train, ensure_ascii=False)
    assert "잘못된 기존 답변" not in json.dumps(train, ensure_ascii=False)


@pytest.mark.parametrize("field,value", [
    ("privacy_checked", False), ("privacy_checked", "true"), ("context_checked", False),
    ("corrected_answer", ""), ("corrected_answer", "잘못된 기존 답변"),
    ("verified_sources", []), ("verified_sources", ["file:///secret"]),
    ("verified_sources", ["https://user:password@example.edu/rule"]),
    ("reviewed_by", ""), ("reviewed_at", "2026-11-11"), ("split", None),
])
def test_incomplete_approval_is_an_error(field, value):
    row = approved()
    row["review"][field] = value
    with pytest.raises(ValueError):
        training.build_datasets([row], [])


@pytest.mark.parametrize("field,value", [("rating", "up"), ("pairing_status", "ambiguous_question"),
                                        ("original_question_key", ""), ("schema_version", 2)])
def test_wrong_provenance_cannot_be_promoted(field, value):
    row = approved()
    row[field] = value
    with pytest.raises(ValueError):
        training.build_datasets([row], [])


def test_same_question_and_rewritten_original_cannot_cross_splits():
    first = approved()
    second = approved(number=2, question="전공필수  이수 조건?!", split="evaluation", source="https://example.edu/other")
    with pytest.raises(ValueError, match="overlaps"):
        training.build_datasets([first, second], [])
    second["question"] = "대화 맥락을 보강한 질문"
    with pytest.raises(ValueError, match="overlaps"):
        training.build_datasets([first, second], [])


def test_same_source_cannot_cross_splits_even_with_a_fragment():
    rows = [approved(), approved(number=2, question="다른 조건?", split="evaluation",
                                 source="https://EXAMPLE.edu/rules/1/#section")]
    with pytest.raises(ValueError, match="overlaps"):
        training.build_datasets(rows, [])


@pytest.mark.parametrize("holdout", [
    {"question": "전공필수  이수 조건?!"},
    {"question": "다른 평가 질문", "expected_source": {"allowed_urls": ["https://example.edu/rules/1"]}},
])
def test_existing_evaluation_data_is_reserved(holdout):
    with pytest.raises(ValueError, match="holdout"):
        training.build_datasets([approved()], [holdout])


def test_duplicate_approvals_need_one_correction():
    with pytest.raises(ValueError, match="duplicate approved feedback"):
        training.build_datasets([approved(), approved()], [])
    with pytest.raises(ValueError, match="duplicate approved question"):
        training.build_datasets([approved(), approved(number=2)], [])


def test_evaluation_uses_existing_schema_and_requires_fact_labels(tmp_path):
    row = approved(split="evaluation")
    train, evaluation, _ = training.build_datasets([row], [])
    assert train == []
    path = tmp_path / "evaluation.jsonl"
    path.write_text(json.dumps(evaluation[0], ensure_ascii=False) + "\n", encoding="utf-8")
    assert _read_jsonl(path) == evaluation
    assert evaluation[0]["expected_behavior"] == row["review"]["corrected_answer"]
    row["review"]["required_facts"] = []
    with pytest.raises(ValueError, match="required_facts"):
        training.build_datasets([row], [])


def test_invalid_approval_writes_nothing_and_existing_directory_is_preserved(tmp_path, monkeypatch):
    input_path = tmp_path / "review.jsonl"
    output_dir = tmp_path / "dataset"
    row = approved()
    row["review"]["privacy_checked"] = False
    input_path.write_text(json.dumps(row), encoding="utf-8")
    monkeypatch.setattr(training, "DEFAULT_HOLDOUTS", [])
    monkeypatch.setattr(sys, "argv", ["build_feedback_training", "--input", str(input_path), "--output-dir", str(output_dir)])
    with pytest.raises(SystemExit) as result:
        training.main()
    assert result.value.code == 2
    assert not output_dir.exists()
    input_path.write_text(json.dumps(approved()), encoding="utf-8")
    output_dir.mkdir()
    marker = output_dir / "train.jsonl"
    marker.write_text("keep existing output", encoding="utf-8")
    with pytest.raises(SystemExit):
        training.main()
    assert marker.read_text(encoding="utf-8") == "keep existing output"
