"""Stage-two approval/provenance gates and order-independent split quarantine."""

from copy import deepcopy
import hashlib
import json

import pytest

from backend.app.feedback_training import build_training_candidate, question_key
from eval.tools import automate_feedback_review as pipeline
from eval.tools.build_feedback_training import build_datasets, read_candidates


URL = "https://stage2.example.edu/official/1"


def parent(number=1, question=None):
    return build_training_candidate({
        "id": f"00000000-0000-0000-0000-{number:012d}", "rating": "down",
        "updated_at": "2026-11-16T00:00:00+00:00", "reasons": ["부정확해요"], "comment": "검토 필요",
        "chat_messages": {"role": "assistant", "content": "기존 오답", "sources": [],
                          "created_at": "2026-11-15T23:02:00+00:00"},
    }, [{"content": question or f"검증용 질문 {number}?", "created_at": "2026-11-15T23:01:00+00:00"}])


def approved(row, split="train", source=URL):
    result = deepcopy(row)
    result["review"].update({"status": "approved", "split": split, "corrected_answer": "출처에 근거한 검수 정답",
                             "verified_sources": [source], "required_facts": ["필수 조건"], "forbidden_claims": [],
                             "privacy_checked": True, "context_checked": True, "reviewed_by": "human",
                             "reviewed_at": "2026-11-16T10:00:00+09:00"})
    return result


def child(row):
    return {"schema_version": 1, "kind": "synthetic_feedback_qa", "synthetic": True,
            "id": "00000000-0000-0000-0000-000000000099", "parent_feedback_id": row["id"],
            "parent_question_key": row["original_question_key"], "family_id": row["original_question_key"],
            "question": "관련 조건을 확인하는 합성 질문?", "proposed_answer": "공식 자료의 추가 조건",
            "required_facts": ["추가 조건"], "forbidden_claims": [],
            "citations": [{"evidence_id": "e1", "quote": "공식 자료의 추가 조건"}],
            "generator": {"model": "fake", "prompt_version": "feedback-candidates-v1"},
            "review": {"status": "pending", "split": None, "verified_sources": [], "reviewed_by": "",
                       "reviewed_at": "", "privacy_checked": False, "context_checked": False}}


def snapshot(row, source=URL):
    content = "공식 자료의 추가 조건을 안내합니다."
    return {"feedback_id": row["id"], "original_question_key": row["original_question_key"], "sources": [
        {"evidence_id": "e1", "uri": source, "content": content,
         "content_sha256": hashlib.sha256(content.encode()).hexdigest()}]}


def test_holdout_conflicts_are_reported_without_relabeling_or_losing_valid_rows():
    first, second = parent(), parent(2)
    reviews = [approved(first), approved(second, source="https://stage2.example.edu/other")]
    queue, problems = pipeline.prepare_queue([first, second], reviews, [], [])
    result = pipeline.audit_and_build(queue, problems, [], [{"id": "existing", "question": "기존 평가 질문",
                                                           "expected_source": {"allowed_urls": [URL]}}])
    assert len(result["train"]) == 1 and result["evaluation"] == []
    decisions = {item["id"]: item for item in result["decisions"]}
    assert decisions[first["id"]]["decision"] == "blocked"
    assert decisions[first["id"]]["reasons"] == ["reserved_evaluation_overlap"]
    assert decisions[second["id"]]["decision"] == "ready"
    assert all(row["review"]["status"] == "approved" for row in queue)
    assert result["needs_review"] == [queue[0]]


def test_synthetic_package_stays_pending_and_has_no_fabricated_downvote():
    row = parent()
    queue, problems = pipeline.prepare_queue([row], [], [child(row)], [row])
    synthetic = queue[1]
    assert synthetic["review"]["status"] == "pending"
    assert synthetic["review"]["corrected_answer"] == child(row)["proposed_answer"]
    assert synthetic["review"]["verified_sources"] == []
    assert synthetic["review"]["privacy_checked"] is False
    assert "rating" not in synthetic and synthetic["review_package_version"] == 1
    result = pipeline.audit_and_build(queue, problems, [snapshot(row)], [])
    assert result["train"] == [] and len(result["needs_review"]) == 2


@pytest.mark.parametrize("split", ["train", "evaluation"])
def test_reviewed_synthetic_is_converted_only_with_fresh_parent_and_exact_evidence(split):
    row, source = parent(), child(parent())
    reviewed = approved(pipeline.package_synthetic(source, row), split=split)
    reviewed["question"] = "검수자가 맥락을 보강한 합성 질문?"
    queue, problems = pipeline.prepare_queue([row], [reviewed], [source], [row])
    result = pipeline.audit_and_build(queue, problems, [snapshot(row)], [])
    assert len(result["ready"]) == 1 and result["ready"][0]["kind"] == pipeline.SYNTHETIC_KIND
    if split == "train":
        assert result["train"][0]["messages"][0]["content"] == reviewed["question"]
        exported = json.dumps(result["train"], ensure_ascii=False)
        assert all(value not in exported for value in ("human", "기존 오답", "검토 필요"))
    else:
        assert result["evaluation"][0]["tags"] == ["synthetic_feedback"]
        assert result["evaluation"][0]["id"].startswith("synthetic_feedback_")


def test_original_converter_still_rejects_synthetic_without_stage_two_opt_in():
    row = parent()
    reviewed = approved(pipeline.package_synthetic(child(row), row))
    with pytest.raises(ValueError, match="only matched downvote"):
        build_datasets([reviewed], [])


def test_changed_parent_and_generation_source_cannot_reuse_approval():
    row, source = parent(), child(parent())
    reviewed = approved(pipeline.package_synthetic(source, row))
    current = deepcopy(row)
    current["comment"] = "새 피드백"
    queue, problems = pipeline.prepare_queue([current], [reviewed], [source], [row])
    result = pipeline.audit_and_build(queue, problems, [snapshot(row)], [])
    assert result["train"] == []
    assert "synthetic_parent_changed_or_withdrawn" in result["decisions"][1]["reasons"]
    modified = deepcopy(source)
    modified["proposed_answer"] = "생성 원본이 변경됨"
    queue, problems = pipeline.prepare_queue([row], [reviewed], [modified], [row])
    assert problems[reviewed["id"]] == ["synthetic_generation_anchor_changed"]


@pytest.mark.parametrize("damage", ["quote", "body", "source"])
def test_synthetic_evidence_failures_are_quarantined(damage):
    row, source, evidence = parent(), child(parent()), snapshot(parent())
    if damage == "quote":
        source["citations"][0]["quote"] = "원문에 없는 인용"
    elif damage == "body":
        evidence["sources"][0]["content"] += "변경된 본문"
    reviewed = approved(pipeline.package_synthetic(source, row), source=URL if damage != "source" else "https://stage2.example.edu/not-saved")
    queue, problems = pipeline.prepare_queue([row], [reviewed], [source], [row])
    result = pipeline.audit_and_build(queue, problems, [evidence], [])
    assert result["train"] == [] and result["decisions"][1]["decision"] == "blocked"


def test_same_parent_cannot_cross_splits_even_when_verified_documents_differ():
    row, source = parent(), child(parent())
    second_url = "https://stage2.example.edu/separate-document"
    reviews = [approved(row), approved(pipeline.package_synthetic(source, row), split="evaluation", source=second_url)]
    queue, problems = pipeline.prepare_queue([row], reviews, [source], [row])
    result = pipeline.audit_and_build(queue, problems, [snapshot(row, second_url)], [])
    assert result["ready"] == []
    assert all("related_group_crosses_train_evaluation" in item["reasons"] for item in result["decisions"])


def test_holdout_block_propagates_to_related_synthetic_with_another_source():
    row, source = parent(), child(parent())
    other = "https://stage2.example.edu/other"
    reviews = [approved(row), approved(pipeline.package_synthetic(source, row), source=other)]
    queue, problems = pipeline.prepare_queue([row], reviews, [source], [row])
    result = pipeline.audit_and_build(queue, problems, [snapshot(row, other)], [
        {"id": "fixed_eval", "question": "보호된 평가 질문", "expected_source": {"allowed_urls": [URL]}}])
    assert result["ready"] == []
    assert all("reserved_evaluation_overlap" in item["reasons"] for item in result["decisions"])


def test_duplicate_approvals_are_quarantined_independently_of_input_order():
    first, second = parent(), parent(2, question=parent()["question"])
    for current in ([first, second], [second, first]):
        queue, problems = pipeline.prepare_queue(current, [approved(first), approved(second)], [], [])
        result = pipeline.audit_and_build(queue, problems, [], [])
        assert result["ready"] == []
        assert all("duplicate_approved_question" in item["reasons"] for item in result["decisions"])


def test_invalid_approval_does_not_discard_unrelated_valid_examples():
    first, second = parent(), parent(2)
    invalid = approved(first)
    invalid["review"]["privacy_checked"] = "true"
    queue, problems = pipeline.prepare_queue([first, second], [invalid, approved(second)], [], [])
    result = pipeline.audit_and_build(queue, problems, [], [])
    assert len(result["ready"]) == 1 and result["ready"][0]["id"] == second["id"]
    assert result["decisions"][0]["decision"] == "blocked"


def test_missing_parent_and_unanchored_synthetic_remain_visible_but_cannot_train():
    row = parent()
    reviewed = approved(pipeline.package_synthetic(child(row), row))
    queue, problems = pipeline.prepare_queue([], [approved(row), reviewed], [], [])
    result = pipeline.audit_and_build(queue, problems, [], [])
    assert len(result["needs_review"]) == 2 and result["ready"] == []
    assert {reason for item in result["decisions"] for reason in item["reasons"]} == {
        "feedback_withdrawn_or_deleted", "missing_synthetic_generation_anchor"}


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def fixture_files(tmp_path, monkeypatch):
    row = parent()
    current, reviewed, holdout = tmp_path / "current.jsonl", tmp_path / "reviewed.jsonl", tmp_path / "holdout.jsonl"
    write_rows(current, [row])
    write_rows(reviewed, [approved(row)])
    write_rows(holdout, [])
    monkeypatch.setattr(pipeline, "DEFAULT_HOLDOUTS", [])
    monkeypatch.setattr(pipeline, "EXTRA_HOLDOUT", holdout)
    return current, reviewed


def test_cli_publishes_complete_offline_package_without_changing_inputs(tmp_path, monkeypatch):
    current, reviewed = fixture_files(tmp_path, monkeypatch)
    original = reviewed.read_bytes()
    output = tmp_path / "package"
    assert pipeline.main(["--current-candidates", str(current), "--reviewed", str(reviewed), "--output-dir", str(output)]) == 0
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["freshness_checked"] is False and summary["train"] == 1
    assert summary["model_calls"] == summary["database_writes"] == 0
    assert read_candidates(output / "train.jsonl")[0]["messages"][1]["content"] == approved(parent())["review"]["corrected_answer"]
    assert read_candidates(output / "review_queue.jsonl")[0]["review_pipeline"]["decision"] == "ready"
    assert reviewed.read_bytes() == original


def test_existing_outputs_are_preserved_and_duplicate_inputs_fail(tmp_path, monkeypatch):
    current, reviewed = fixture_files(tmp_path, monkeypatch)
    output = tmp_path / "package"
    output.mkdir()
    marker = output / "train.jsonl"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(SystemExit):
        pipeline.main(["--current-candidates", str(current), "--reviewed", str(reviewed), "--output-dir", str(output)])
    assert marker.read_text() == "keep"
    with pytest.raises(ValueError, match="duplicate"):
        pipeline.prepare_queue([parent()], [approved(parent()), approved(parent())], [], [])
