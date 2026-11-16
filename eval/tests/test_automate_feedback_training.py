"""No-network tests for paid-operation gates, frozen comparisons and recovery."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from backend.app.feedback_training import build_training_candidate
from backend.app.generation import build_user_message
from eval.tools import automate_feedback_training as pipeline
from eval.tools.build_feedback_training import build_datasets, read_candidates


URL = "https://training.example.edu/notice/1"
CANDIDATE = "ft:base:org:candidate:1"


def case(number=1):
    return {"id": f"comparison_{number}", "question": f"독립 평가 질문 {number}?", "category": "test",
            "answerable": True, "expected_no_info": False, "label_status": "ready", "difficulty": "hard",
            "expected_source": {"dataset": [], "title_keywords": [], "allowed_urls": [f"https://evaluation.example.edu/{number}"], "source_ids": []},
            "required_facts": [{"id": "fact_1", "description": "필수 조건", "keywords": []}],
            "forbidden_claims": [], "tags": [], "expected_behavior": "answer"}


def approved(number=1, split="train"):
    row = build_training_candidate({
        "id": f"00000000-0000-0000-0000-{number:012d}", "rating": "down",
        "updated_at": "2026-11-16T00:00:00+00:00", "reasons": ["incorrect"], "comment": "검토 필요",
        "chat_messages": {"role": "assistant", "content": "원래 오답", "sources": [], "created_at": "2026-11-15T23:02:00+00:00"},
    }, [{"content": f"학습 질문 {number}?", "created_at": "2026-11-15T23:01:00+00:00"}])
    row["review"].update(status="approved", split=split, corrected_answer="검수된 필수 조건 [1]",
                         verified_sources=[URL], required_facts=["필수 조건"], forbidden_claims=[],
                         privacy_checked=True, context_checked=True, reviewed_by="reviewer",
                         reviewed_at="2026-11-16T10:00:00+09:00")
    return row


def evidence(row):
    content = "공식 자료의 필수 조건"
    return {"feedback_id": row["id"], "original_question_key": row["original_question_key"],
            "captured_at": "2026-11-15T16:00:00+00:00", "sources": [
                {"uri": URL, "title": "공식 안내", "content": content,
                 "content_sha256": hashlib.sha256(content.encode()).hexdigest()}]}


def stage_two(tmp_path, monkeypatch, count=3):
    directory = tmp_path / "stage2"
    directory.mkdir()
    rows = [approved(i + 1) for i in range(count)]
    current = deepcopy(rows)
    for row in current:
        row["review"] = {"status": "pending"}
    train, evaluation, _ = build_datasets(rows, [])
    for name, data in {"ready.jsonl": rows, "current_candidates.jsonl": current, "train.jsonl": train,
                       "evaluation.jsonl": evaluation, "evidence.jsonl": [evidence(row) for row in rows]}.items():
        pipeline.write_rows(directory / name, data)
    pipeline.write_json(directory / "summary.json", {"schema_version": 2, "dataset_built": True,
                        "freshness_checked": True, "current_candidates_sha256": pipeline.digest(current),
                        "created_at": "2026-11-16T00:00:00+00:00", "holdouts": []})
    cases = tmp_path / "cases.jsonl"
    pipeline.write_rows(cases, [case()])
    monkeypatch.setattr(pipeline, "DEFAULT_HOLDOUTS", [])
    monkeypatch.setattr(pipeline, "EXTRA_HOLDOUT", cases)
    args = pipeline.parser().parse_args(["prepare", "--dataset-dir", str(directory), "--run-dir", str(tmp_path / "run"),
        "--base-model", "base", "--baseline-model", "baseline", "--judge-model", "judge", "--cases", str(cases)])
    return args, rows


def test_prepare_reaudits_and_blocks_three_examples_before_any_api(tmp_path, monkeypatch):
    args, rows = stage_two(tmp_path, monkeypatch)
    pipeline.prepare(args)
    manifest, state = pipeline.load_run(args.run_dir)
    assert manifest["train_count"] == 3
    assert manifest["training_blockers"] == ["minimum_training_examples:10;available:3"]
    with pytest.raises(ValueError, match="Training blocked"):
        pipeline.submit(args.run_dir, manifest, state, eligible=True, client=object())
    uploads = read_candidates(args.run_dir / "train.jsonl")
    assert len(uploads) == 3
    assert uploads[0]["messages"][-1]["content"] == rows[0]["review"]["corrected_answer"]
    text = (args.run_dir / "train.jsonl").read_text(encoding="utf-8")
    assert "원래 오답" not in text and "reviewer" not in text and "독립 평가 질문" not in text
    assert "공식 자료의 필수 조건" in text
    assert "2026-11-16" in uploads[0]["messages"][1]["content"]  # Frozen KST date.


@pytest.mark.parametrize("mutation", ["offline", "withdrawn", "edited_export", "overlap"])
def test_prepare_refuses_stale_or_contaminated_inputs(tmp_path, monkeypatch, mutation):
    args, rows = stage_two(tmp_path, monkeypatch)
    if mutation == "offline":
        summary = pipeline.read_json(args.dataset_dir / "summary.json")
        summary["freshness_checked"] = False
        pipeline.write_json(args.dataset_dir / "summary.json", summary)
    elif mutation == "withdrawn":
        pipeline.write_rows(args.dataset_dir / "ready.jsonl", [approved(99)])
    elif mutation == "edited_export":
        pipeline.write_rows(args.dataset_dir / "train.jsonl", [])
    else:
        value = case()
        value["expected_source"]["allowed_urls"] = [URL]
        pipeline.write_rows(args.cases[0], [value])
    with pytest.raises(ValueError):
        pipeline.prepare(args)
    assert not args.run_dir.exists()


def test_training_requires_verified_body_and_checks_hash():
    row = approved()
    data, problems = pipeline.build_training_messages([row], [], "system")
    assert not data and problems[0].startswith("missing_training_evidence:")
    saved = evidence(row)
    saved["sources"][0]["content"] = "tampered"
    with pytest.raises(ValueError, match="hash mismatch"):
        pipeline.build_training_messages([row], [saved], "system")


def test_manifest_and_context_changes_are_rejected(tmp_path, monkeypatch):
    args, _ = stage_two(tmp_path, monkeypatch)
    pipeline.prepare(args)
    with (args.run_dir / "train.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{}\n")
    with pytest.raises(ValueError, match="artifact changed"):
        pipeline.load_run(args.run_dir)


def captured(value, system="system"):
    content, shown = "원본 전체 본문. 공식 필수 조건", "공식 필수 조건"
    return {"id": value["id"], "question": value["question"], "evaluation_evidence": {
        "body_scope": "retrieved_chunks", "answer_mode": "search", "captured_at": "2026-11-16T10:00:00+09:00",
        "system_prompt_sha256": hashlib.sha256(system.encode()).hexdigest(), "plan": {},
        "prompt_source_block": "[1] 공식 안내\n공식 필수 조건", "sources": [
            {"index": 1, "uri": URL, "content": content, "shown_content": shown,
             "content_sha256": hashlib.sha256(content.encode()).hexdigest()}]}}


def test_freeze_preserves_prompt_context_and_limits_judge_to_shown_body():
    value = case()
    frozen = pipeline.freeze_case(value, captured(value), "system")
    assert frozen["eligible"]
    assert "[1] 공식 안내\n공식 필수 조건" in frozen["messages"][1]["content"]
    assert frozen["evaluation_evidence"]["sources"][0]["content"] == "공식 필수 조건"
    assert frozen["messages_sha256"] == pipeline.digest(frozen["messages"])
    bad = captured(value)
    bad["evaluation_evidence"]["system_prompt_sha256"] = "old"
    with pytest.raises(ValueError, match="prompt mismatch"):
        pipeline.freeze_case(value, bad, "system")


def test_direct_routes_are_explicit_exclusions():
    value = case()
    saved = captured(value)
    saved["evaluation_evidence"].update(answer_mode="direct", prompt_source_block=None)
    assert pipeline.freeze_case(value, saved, "system") == {
        "id": value["id"], "eligible": False, "exclusion": "non_search_route:direct"}


def test_production_message_builder_has_deterministic_replay_date():
    from datetime import date
    text = build_user_message("질문", [], 100, source_block="frozen context", as_of=date(2026, 11, 1))
    assert "frozen context" in text and "2026-11-01" in text


def training_manifest():
    return {"training_blockers": [], "base_model": "base", "run_id": "run-1", "seed": 42,
            "epochs": 3, "timeout": 10, "artifacts": {}}


def test_submit_uncertain_outcome_never_creates_a_second_job(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "TRAINING_DEADLINE", "9999-01-01")
    manifest, state = training_manifest(), {"phase": "uploaded", "training_file_id": "file-1"}
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("lost response")
    client = SimpleNamespace(fine_tuning=SimpleNamespace(jobs=SimpleNamespace(create=create)))
    with pytest.raises(TimeoutError):
        pipeline.submit(tmp_path, manifest, state, eligible=True, client=client)
    with pytest.raises(ValueError, match="outcome uncertain"):
        pipeline.submit(tmp_path, manifest, state, eligible=True, client=client)
    assert len(calls) == 1
    assert "validation_file" not in calls[0]
    assert calls[0]["training_file"] == "file-1"
    assert pipeline.read_json(tmp_path / "state.json")["phase"] == "submit_intent"


def test_repeated_submit_uses_existing_job_without_client(tmp_path):
    pipeline.submit(tmp_path, training_manifest(), {"job_id": "ftjob-1"}, eligible=True, client=object())
    assert not (tmp_path / "state.json").exists()


def test_comparison_rejects_non_openai_fine_tuned_candidate(tmp_path):
    with pytest.raises(ValueError, match="OpenAI ft:"):
        pipeline.compare(tmp_path, {"baseline_model": "baseline", "judge_model": "judge"}, {},
                         candidate_model="external-provider-model", client=object())


def test_attach_rejects_a_different_training_job(tmp_path):
    job = SimpleNamespace(id="job", model="other", training_file="file-1", status="running")
    client = SimpleNamespace(fine_tuning=SimpleNamespace(jobs=SimpleNamespace(retrieve=lambda _: job)))
    with pytest.raises(ValueError, match="does not match"):
        pipeline.attach(tmp_path, training_manifest(), {"training_file_id": "file-1"}, file_id=None, job_id="job", client=client)


def response(answer="필수 조건 [1]", finish="stop"):
    return SimpleNamespace(model="resolved-model", usage=SimpleNamespace(prompt_tokens=15, completion_tokens=8),
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=answer, refusal=None))])


def test_durable_budget_includes_failed_calls_and_truncated_outputs(tmp_path):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return response(finish="length")
    state = {"requests": 0}
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    metered = pipeline.MeteredClient(tmp_path, {"max_calls": 1}, state, client)
    with pytest.raises(pipeline.ModelRequestError, match="Model request failed"):
        metered.create(model="baseline", messages=[])
    with pytest.raises(ValueError, match="budget exhausted"):
        metered.create(model="judge", messages=[])
    assert len(calls) == 1
    assert pipeline.read_json(tmp_path / "state.json")["requests"] == 1
    assert state["usage_by_model"]["baseline"]["prompt_tokens"] == 15


def test_provider_errors_are_sanitized_before_judge_logging(tmp_path):
    class ProviderError(Exception):
        status_code = 401
    def create(**kwargs):
        raise ProviderError("Incorrect API key: sk-do-not-log-this")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    metered = pipeline.MeteredClient(tmp_path, {"max_calls": 4}, {"requests": 0}, client)
    with pytest.raises(pipeline.ModelRequestError) as caught:
        metered.create(model="judge", messages=[])
    assert caught.value.status_code == 401
    assert "sk-do-not-log-this" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def judgment(covered=True):
    return {"facts": [{"id": "fact_1", "verdict": "covered" if covered else "missing"}], "forbidden": [],
            "claims": [{"verdict": "supported"}], "refusal": {"verdict": "not_refusal"},
            "department": {"verdict": "not_applicable"}, "temporal": {"verdict": "not_applicable"}}


def test_decision_requires_complete_pairs_and_rejects_regressions():
    manifest = {"case_count": 1, "min_cases": 1, "min_improvement": 0.02}
    base = [{**case(), "judged": judgment(False)}]
    candidate = [{**case(), "judged": judgment(True)}]
    good = pipeline.decision_report(base, candidate, manifest)
    assert good["recommendation"] == "review_candidate" and good["automatic_deployment"] is False
    regression = pipeline.decision_report(candidate, base, manifest)
    assert "previously_correct_case_regressed" in regression["reasons"]
    missing = pipeline.decision_report(base, [], manifest)
    assert missing["recommendation"] == "hold" and "incomplete_evaluation" in missing["reasons"]
    candidate[0]["judged"]["temporal"]["verdict"] = "unverifiable"
    assert "unverified_quality" in pipeline.decision_report(base, candidate, manifest)["reasons"]


def test_compare_same_messages_ordered_resume_and_blind_fixed_judge(tmp_path, monkeypatch):
    from eval.tools import grade_eval_results
    value = case()
    frozen = pipeline.freeze_case(value, captured(value), "system")
    pipeline.write_rows(tmp_path / "contexts.jsonl", [frozen])
    pipeline.write_rows(tmp_path / "cases.jsonl", [value])
    manifest = {"baseline_model": "baseline", "judge_model": "judge", "max_calls": 10, "timeout": 10,
                "temperature": 0.1, "max_tokens": 700, "case_count": 1, "min_cases": 1,
                "min_improvement": 0.02, "run_id": "test"}
    state = {"requests": 0, "capture_requests": 0, "data_snapshot": "snapshot-1", "frozen_artifacts": {
        "contexts.jsonl": pipeline.file_hash(tmp_path / "contexts.jsonl")}}
    calls, judged_models = [], []
    def create(**kwargs):
        calls.append(deepcopy(kwargs))
        return response()
    def grade(client, model, row, timeout, **kwargs):
        judged_models.append(model)
        return judgment(row["variant"] == "candidate")
    monkeypatch.setattr(grade_eval_results, "grade_case", grade)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    report = pipeline.compare(tmp_path, manifest, state, candidate_model=CANDIDATE, client=client)
    assert report["recommendation"] == "review_candidate"
    assert calls[0]["messages"] == calls[1]["messages"]
    assert calls[0]["temperature"] == calls[1]["temperature"]
    assert judged_models == ["judge", "judge"]
    pipeline.compare(tmp_path, manifest, state, candidate_model=CANDIDATE, client=client)
    assert len(calls) == 2  # No duplicated generation or grading on completed resume.
    with pytest.raises(ValueError, match="cannot change"):
        pipeline.compare(tmp_path, manifest, state, candidate_model="ft:base:org:different:2", client=client)


def test_excluded_context_never_calls_model_and_is_not_reported_as_complete(tmp_path):
    value = case()
    pipeline.write_rows(tmp_path / "cases.jsonl", [value])
    pipeline.write_rows(tmp_path / "contexts.jsonl", [{"id": value["id"], "eligible": False, "exclusion": "non_search_route:direct"}])
    manifest = {"baseline_model": "base", "judge_model": "judge", "max_calls": 4, "timeout": 10,
                "case_count": 1, "min_cases": 1, "min_improvement": 0.02, "run_id": "test"}
    state = {"requests": 0, "capture_requests": 0, "data_snapshot": "snapshot", "frozen_artifacts": {"contexts.jsonl": "hash"}}
    report = pipeline.compare(tmp_path, manifest, state, candidate_model=CANDIDATE, client=object())
    assert report["recommendation"] == "hold" and "incomplete_evaluation" in report["reasons"]
    assert state["requests"] == 0


def test_uncertain_paid_comparison_is_not_retried(tmp_path):
    value = case()
    pipeline.write_rows(tmp_path / "cases.jsonl", [value])
    pipeline.write_rows(tmp_path / "contexts.jsonl", [pipeline.freeze_case(value, captured(value), "system")])
    state = {"requests": 1, "capture_requests": 0, "frozen_artifacts": {"contexts.jsonl": "hash"},
             "comparison_attempted": [f"{value['id']}:baseline"]}
    manifest = {"baseline_model": "base", "judge_model": "judge", "timeout": 10}
    with pytest.raises(ValueError, match="Uncertain comparison outcome"):
        pipeline.compare(tmp_path, manifest, state, candidate_model=CANDIDATE, client=object())


def test_run_waits_for_training_without_capturing_or_comparing(tmp_path, monkeypatch):
    args = pipeline.parser().parse_args(["run", "--run-dir", str(tmp_path), "--base-url", "http://localhost:8000",
                                        "--data-snapshot", "db-20261116", "--eligible-account"])
    calls = []
    monkeypatch.setattr(pipeline, "submit", lambda *a, **kw: calls.append("submit"))
    def pending(run, manifest, state, **kwargs):
        calls.append("status")
        state["job_status"] = "running"
    monkeypatch.setattr(pipeline, "status", pending)
    monkeypatch.setattr(pipeline, "capture", lambda *a, **kw: pytest.fail("must wait for training"))
    assert pipeline.advance(args, {}, {}) == 3
    assert calls == ["submit", "status"]


def test_run_supports_existing_candidate_without_training_access(tmp_path, monkeypatch):
    args = pipeline.parser().parse_args(["run", "--run-dir", str(tmp_path), "--base-url", "http://localhost:8000",
        "--data-snapshot", "db-20261116", "--candidate-model", CANDIDATE])
    calls = []
    monkeypatch.setattr(pipeline, "submit", lambda *a, **kw: pytest.fail("existing model comparison must not train"))
    monkeypatch.setattr(pipeline, "capture", lambda *a, **kw: calls.append("capture"))
    monkeypatch.setattr(pipeline, "compare", lambda *a, **kw: {"reasons": []})
    assert pipeline.advance(args, {"training_blockers": ["minimum_training_examples"]}, {}) == 0
    assert calls == ["capture"]


def test_capture_uses_backend_token_and_resumes_without_extra_http_call(tmp_path, monkeypatch):
    from io import StringIO
    value = case()
    pipeline.write_rows(tmp_path / "cases.jsonl", [value])
    (tmp_path / "system.txt").write_text("system", encoding="utf-8")
    monkeypatch.setenv("ASK_API_TOKEN", "test-token")
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return StringIO(json.dumps({"evaluation_evidence": captured(value)["evaluation_evidence"]}))
    monkeypatch.setattr(pipeline, "build_opener", lambda handler: Opener())
    manifest, state = {"timeout": 10}, {"capture_requests": 0}
    pipeline.capture(tmp_path, manifest, state, "http://localhost:8000", "db")
    assert calls[0].full_url == "http://localhost:8000/ask"
    assert calls[0].get_header("Authorization") == "Bearer test-token"
    assert state["capture_requests"] == 1
    assert state["frozen_artifacts"]
    assert "test-token" not in (tmp_path / "state.json").read_text(encoding="utf-8")


def test_lock_blocks_concurrent_paid_commands(tmp_path):
    with pipeline.lock(tmp_path):
        with pytest.raises(ValueError, match="locked"):
            with pipeline.lock(tmp_path):
                pass
    assert not (tmp_path / ".experiment.lock").exists()
