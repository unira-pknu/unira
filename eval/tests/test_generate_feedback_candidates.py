"""Feedback-grounded drafts: no real model, backend or database calls."""

from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from backend.app.feedback_training import build_training_candidate, question_key
from eval.tools import generate_feedback_candidates as generate
from eval.tools import llm_budget
from eval.tools.build_feedback_training import build_datasets, read_candidates, source_key
from eval.tools.prepare_feedback_training import merge_reviews


def feedback(number=1):
    return build_training_candidate({
        "id": f"00000000-0000-0000-0000-{number:012d}", "rating": "down",
        "updated_at": "2026-11-14T09:00:00+00:00", "reasons": ["정보가 부정확해요"],
        "comment": "적용 학과가 틀립니다.",
        "chat_messages": {"role": "assistant", "content": "전공필수 과목이 없습니다.", "sources": [],
                          "created_at": "2026-11-14T08:01:00+00:00"},
    }, [{"content": f"컴퓨터공학과 전공필수 이수 조건 {number}?", "created_at": "2026-11-14T08:00:00+00:00"}])


def snapshot():
    return {"sources": [{
        "uri": "https://www.pknu.ac.kr/rule/42", "title": "2026학년도 교육과정",
        "content": "2026학년도 컴퓨터공학과 전공필수는 12학점이며, 졸업 전 이수해야 합니다.",
        "source_id": "source-42", "chunk_id": "chunk-42-0", "metadata": {"year": 2026},
    }]}


def evidence():
    return generate.normalize_evidence(snapshot(), ["pknu.ac.kr"], set())


def qa(question=None):
    return {"question": question or feedback()["question"], "answer": "2026학년도 컴퓨터공학과 전공필수는 12학점입니다.",
            "required_facts": ["전공필수 12학점"], "forbidden_claims": ["전공필수가 없다는 안내"],
            "citations": [{"evidence_id": "e1", "quote": "전공필수는 12학점"}]}


def generation():
    return {"failure_type": "scope", "failure_analysis": "학과별 이수 조건을 구분하지 못했습니다.",
            "correction": qa(), "related_questions": [qa("전공필수 12학점은 언제까지 이수해야 하나요?")]}


class FakeLLM:
    model = "fake-model"

    def __init__(self, value=None, error=None):
        self.value, self.error, self.calls = value or generation(), error, []

    def chat_json(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return deepcopy(self.value)

    def usage(self):
        return {"model": self.model, "requests": len(self.calls)}


def batch(row=None, llm=None, sources=None, **kwargs):
    row = row or feedback()
    return generate.generate_batch([row], {row["id"]: evidence() if sources is None else sources}, llm or FakeLLM(),
                                   variants=2, reserved_questions=kwargs.get("reserved", set()),
                                   all_question_keys={row["original_question_key"]}, max_tokens=2500)


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def inputs(tmp_path):
    source, cached = tmp_path / "feedback.jsonl", tmp_path / "evidence.jsonl"
    row = feedback()
    write_rows(source, [row])
    write_rows(cached, [{"feedback_id": row["id"], "original_question_key": row["original_question_key"], **snapshot()}])
    return source, cached


def test_generated_correction_is_pending_and_preserves_provenance():
    row = feedback()
    row["review"].update({"privacy_checked": True, "verified_sources": ["https://unverified.example"]})
    reviews, synthetic, events = batch(row)
    draft = reviews[0]
    assert draft["review"]["corrected_answer"] == qa()["answer"]
    assert draft["review"]["status"] == "pending"
    assert draft["review"]["verified_sources"] == []
    assert draft["review"]["privacy_checked"] is False
    assert draft["review"]["context_checked"] is False
    assert draft["review"]["reviewed_by"] == "" and draft["review"]["split"] is None
    for field in ("id", "original_question_key", "feedback_updated_at", "rejected_answer", "answer_sources"):
        assert draft[field] == row[field]
    assert synthetic[0]["kind"] == "synthetic_feedback_qa"
    assert synthetic[0]["parent_feedback_id"] == row["id"]
    assert synthetic[0]["review"]["status"] == "pending"
    assert "rating" not in synthetic[0]
    assert events == []
    assert row["review"]["corrected_answer"] == ""  # Input is not mutated.


def test_original_draft_integrates_with_existing_review_flow_and_requires_human_checks():
    row = feedback()
    draft = batch()[0][0]
    merged = merge_reviews([row], [draft])
    assert build_datasets(merged, []) == ([], [], 1)
    draft["review"]["status"] = "approved"
    with pytest.raises(ValueError, match="privacy_checked"):
        build_datasets([draft], [])
    draft["review"].update({"privacy_checked": True, "context_checked": True, "reviewed_by": "human",
                            "reviewed_at": "2026-11-14T20:00:00+09:00", "split": "train",
                            "verified_sources": [snapshot()["sources"][0]["uri"]]})
    train, _, _ = build_datasets(merge_reviews([row], [draft]), [])
    assert train[0]["messages"][1]["content"] == qa()["answer"]


def test_synthetic_cannot_masquerade_as_reviewed_feedback():
    synthetic = batch()[1][0]
    synthetic["review"]["status"] = "approved"
    with pytest.raises(ValueError, match="only matched downvote"):
        build_datasets([synthetic], [])


@pytest.mark.parametrize("change", [
    {"citations": [{"evidence_id": "invented", "quote": "전공필수는 12학점"}]},
    {"citations": [{"evidence_id": "e1", "quote": "전공필수는 99학점"}]},
    {"citations": []}, {"required_facts": []}, {"question": "?!"},
    {"review": {"status": "approved"}},
])
def test_invalid_model_qa_cannot_populate_review(change):
    result = generation()
    result["correction"].update(change)
    reviews, synthetic, events = batch(llm=FakeLLM(result))
    assert reviews[0]["review"]["corrected_answer"] == ""
    assert reviews[0]["ai_generation"]["status"] == "generation_failed"
    assert synthetic == [] and events[0]["reason"] == "invalid_or_failed_generation"


def test_no_evidence_skips_model_and_insufficient_context_is_preserved():
    llm = FakeLLM()
    reviews, synthetic, _ = batch(llm=llm, sources=[])
    assert llm.calls == [] and synthetic == []
    assert reviews[0]["ai_generation"]["status"] == "no_allowed_evidence"
    value = generation()
    value.update(correction=None, related_questions=[], failure_type="unclear")
    assert batch(llm=FakeLLM(value))[0][0]["ai_generation"]["status"] == "needs_context_or_evidence"


def test_rejected_answer_is_never_recycled_as_a_correction():
    value = generation()
    value["correction"]["answer"] = feedback()["rejected_answer"]
    assert batch(llm=FakeLLM(value))[0][0]["ai_generation"]["status"] == "generation_failed"


def test_synthetic_duplicates_and_holdouts_are_removed():
    value = generation()
    value["related_questions"] = [qa(), qa("기존 평가 질문?")]
    _, synthetic, events = batch(llm=FakeLLM(value), reserved={question_key("기존 평가 질문?!")})
    assert synthetic == [] and len(events) == 2


def test_unmatched_duplicate_and_reserved_feedback_not_selected():
    first, duplicate, unmatched, reserved = feedback(), feedback(2), feedback(3), feedback(4)
    duplicate["question"] = first["question"]
    duplicate["original_question_key"] = first["original_question_key"]
    unmatched["pairing_status"] = "ambiguous_question"
    selected, skipped = generate.select_feedback([first, duplicate, unmatched, reserved], {reserved["original_question_key"]}, 10)
    assert selected == [first]
    assert [item["reason"] for item in skipped] == ["duplicate_question", "unmatched_question", "reserved_evaluation_question"]


def test_domain_boundaries_reserved_urls_and_source_limits(monkeypatch):
    data = snapshot()
    data["sources"] = [
        {**data["sources"][0], "uri": "https://pknu.ac.kr.evil.example/rule/42"},
        {**data["sources"][0], "uri": "https://user:password@www.pknu.ac.kr/rule/42"},
        *data["sources"],
    ]
    assert len(generate.normalize_evidence(data, ["pknu.ac.kr"], set())) == 1
    assert generate.normalize_evidence(data, ["pknu.ac.kr"], {source_key(snapshot()["sources"][0]["uri"])}) == []
    monkeypatch.setattr(generate, "MAX_SOURCE_CHARS", 10)
    result = generate.normalize_evidence(data, ["pknu.ac.kr"], set())
    assert len(result[0]["content"]) == 10 and result[0]["truncated"] is True
    assert generate.normalize_evidence({"sources": result}, ["pknu.ac.kr"], set()) == result


def test_basic_feedback_identifiers_are_masked_before_model():
    row = feedback()
    row["comment"] = "연락처 student@example.edu, 010-1234-5678, 학번: 2026123456"
    payload = generate.build_messages(row, evidence(), 2)[1]["content"]
    assert all(value not in payload for value in ("student@example.edu", "010-1234-5678", "2026123456"))
    assert "[이메일]" in payload and "[휴대전화]" in payload and "[학번]" in payload
    assert row["comment"].startswith("연락처 student")


def test_backend_retrieval_uses_only_bodies_and_never_reuses_the_answer(monkeypatch):
    opener = Mock()
    opener.open.return_value = BytesIO(json.dumps({"answer": "must be discarded", "evaluation_evidence": snapshot()}).encode())
    monkeypatch.setattr(generate, "build_opener", lambda *args: opener)
    result = generate.retrieve_evidence("http://localhost:8000", "secret", feedback(), 60)
    assert result == snapshot() and "must be discarded" not in json.dumps(result)
    request = opener.open.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer secret"
    assert json.loads(request.data)["include_evidence"] is True


@pytest.mark.parametrize("payload", [{"answer": "no evidence"}, {"retrieval_status": "partial", "evaluation_evidence": snapshot()}])
def test_backend_missing_or_partial_evidence_is_rejected(monkeypatch, payload):
    opener = Mock()
    opener.open.return_value = BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(generate, "build_opener", lambda *args: opener)
    with pytest.raises(ValueError):
        generate.retrieve_evidence("http://localhost:8000", "secret", feedback(), 60)


def test_backend_http_error_hides_private_response(monkeypatch):
    opener = Mock()
    opener.open.side_effect = HTTPError("http://localhost:8000", 401, "private", {}, BytesIO(b"secret"))
    monkeypatch.setattr(generate, "build_opener", lambda *args: opener)
    with pytest.raises(ValueError, match="HTTP 401") as error:
        generate.retrieve_evidence("http://localhost:8000", "secret", feedback(), 60)
    assert "private" not in str(error.value) and "secret" not in str(error.value)


def test_dry_run_never_loads_model_or_calls_backend_or_writes_output(tmp_path, monkeypatch):
    source, _ = inputs(tmp_path)
    output = tmp_path / "generated"
    monkeypatch.setattr(generate, "DEFAULT_HOLDOUTS", [])
    forbidden = Mock(side_effect=AssertionError("dry run performed a side effect"))
    monkeypatch.setattr(generate, "BudgetedLLM", forbidden)
    monkeypatch.setattr(generate, "retrieve_evidence", forbidden)
    monkeypatch.setattr(generate, "getpass", forbidden)
    monkeypatch.setattr(generate, "load_backend_env", forbidden)
    assert generate.main(["--input", str(source), "--base-url", "http://localhost:8000",
                          "--output-dir", str(output), "--dry-run"]) == 0
    assert not output.exists()


@pytest.mark.parametrize("ask_override", [False, True])
def test_generation_loads_dotenv_token_before_retrieval_and_honors_ask_override(tmp_path, monkeypatch, capsys, ask_override):
    source, _ = inputs(tmp_path)
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    contents = "FEEDBACK_API_TOKEN=feedback-dotenv-token\n"
    if ask_override:
        contents += "ASK_API_TOKEN=ask-dotenv-token\n"
    (backend_dir / ".env").write_text(contents, encoding="utf-8")
    monkeypatch.setattr(llm_budget, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(generate, "DEFAULT_HOLDOUTS", [])
    monkeypatch.delenv("FEEDBACK_API_TOKEN", raising=False)
    monkeypatch.delenv("ASK_API_TOKEN", raising=False)
    monkeypatch.setattr(generate, "getpass", Mock(side_effect=AssertionError("token should come from .env")))
    retrieve = Mock(return_value=snapshot())
    monkeypatch.setattr(generate, "retrieve_evidence", retrieve)
    monkeypatch.setattr(generate, "BudgetedLLM", lambda **kwargs: FakeLLM())
    output = tmp_path / "generated"
    assert generate.main(["--input", str(source), "--base-url", "http://localhost:8000",
                          "--output-dir", str(output)]) == 0
    expected = "ask-dotenv-token" if ask_override else "feedback-dotenv-token"
    assert retrieve.call_args.args[1] == expected
    captured = capsys.readouterr()
    assert expected not in captured.out + captured.err
    assert expected not in (output / "summary.json").read_text(encoding="utf-8")


def test_cached_evidence_run_writes_only_pending_candidates_and_usage(tmp_path, monkeypatch):
    source, cached = inputs(tmp_path)
    output = tmp_path / "generated"
    monkeypatch.setattr(generate, "DEFAULT_HOLDOUTS", [])
    llm = FakeLLM()
    monkeypatch.setattr(generate, "BudgetedLLM", lambda **kwargs: llm)
    assert generate.main(["--input", str(source), "--evidence", str(cached), "--output-dir", str(output)]) == 0
    assert read_candidates(output / "review.jsonl")[0]["review"]["status"] == "pending"
    assert read_candidates(output / "synthetic_candidates.jsonl")[0]["parent_feedback_id"] == feedback()["id"]
    assert read_candidates(output / "evidence.jsonl")[0]["sources"] == evidence()
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["generator_usage"]["requests"] == 1 and summary["backend_requests"] == 0
    assert summary["all_pending"] is True and summary["correction_count"] == 1
    assert not (output / "train.jsonl").exists()


@pytest.mark.parametrize("error", [llm_budget.BudgetExceeded("budget"), llm_budget.FatalLLMError("private-provider-data")])
def test_fatal_model_errors_cannot_publish_partial_outputs(tmp_path, monkeypatch, error, capsys):
    source, cached = inputs(tmp_path)
    output = tmp_path / "generated"
    monkeypatch.setattr(generate, "DEFAULT_HOLDOUTS", [])
    monkeypatch.setattr(generate, "BudgetedLLM", lambda **kwargs: FakeLLM(error=error))
    with pytest.raises(SystemExit) as result:
        generate.main(["--input", str(source), "--evidence", str(cached), "--output-dir", str(output)])
    assert result.value.code == 2 and not output.exists()
    assert "private-provider-data" not in capsys.readouterr().err


def test_existing_output_rejected_before_paid_calls(tmp_path, monkeypatch):
    source, cached = inputs(tmp_path)
    output = tmp_path / "generated"
    output.mkdir()
    marker = output / "review.jsonl"
    marker.write_text("keep existing work", encoding="utf-8")
    monkeypatch.setattr(generate, "BudgetedLLM", Mock(side_effect=AssertionError("must not call")))
    with pytest.raises(SystemExit):
        generate.main(["--input", str(source), "--evidence", str(cached), "--output-dir", str(output)])
    assert marker.read_text(encoding="utf-8") == "keep existing work"


@pytest.mark.parametrize("finish_reason,refusal", [("length", None), ("content_filter", None), ("stop", "refused")])
def test_shared_json_helper_rejects_incomplete_or_refused_responses_without_retry(monkeypatch, finish_reason, refusal):
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
                                message=SimpleNamespace(content='{"question":"q"}', refusal=refusal))], usage=None)
    completions = Mock()
    completions.create.return_value = response
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(llm_budget, "load_backend_env", lambda: None)
    llm = llm_budget.BudgetedLLM(model="fake", max_calls=3, client=client)
    with pytest.raises(llm_budget.InvalidLLMOutput):
        llm.chat_json([{"role": "system", "content": "JSON"}])
    assert completions.create.call_count == 1
