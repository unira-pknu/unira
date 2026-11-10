from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from eval.tools.grade_eval_results import build_user_prompt, grade_case, summarize
from eval.tools.eval_ask import run_eval
from eval.tools.provenance import file_sha256
from eval.tools.quality import JUDGE_CONTRACT_VERSION, answer_units, quality_summary, validate_judgment
from eval.tools.record_baseline import _result_rows, build_baseline
from eval.tests.test_eval_ask import valid_case


def result():
    return {
        **valid_case(), "answer": "컴퓨터공학과는 140학점입니다[1].", "error": None,
        "response_answerable": True,
        "sources": [{"content": "짧은 화면용 발췌"}],
        "evaluation_evidence": {
            "schema_version": 1, "body_scope": "retrieved_chunks", "captured_at": "2026-11-10T12:00:00+09:00",
            "sources": [{"index": 1, "content": "컴퓨터공학과 졸업 소요학점은 140학점입니다.",
                         "metadata": {"department_id": "ce"}}],
        },
    }


def judgment():
    evidence = [{"source_index": 1, "quote": "컴퓨터공학과 졸업 소요학점은 140학점입니다."}]
    return {
        "facts": [{"id": "fact_1", "verdict": "covered"}], "forbidden": [],
        "claims": [{"text": "컴퓨터공학과는 140학점입니다[1].", "citations": [1],
                    "verdict": "supported", "source_evidence": evidence}],
        "refusal": {"verdict": "not_refusal", "sources_sufficient": True},
        "department": {"verdict": "correct", "answer_evidence": "컴퓨터공학과는 140학점입니다[1].", "source_evidence": evidence},
        "temporal": {"verdict": "not_applicable"},
    }


def unit_judgment():
    judged = judgment()
    judged["facts"][0]["answer_unit_ids"] = ["u001"]
    for claim in judged["claims"]:
        claim["unit_id"] = "u001"
        del claim["text"]
        del claim["citations"]
    return judged


def test_judge_receives_full_cited_body_and_label_context():
    case = result()
    case["evaluation_evidence"]["sources"][0]["shown_content"] = "모델용으로 잘린 문장…"
    prompt = build_user_prompt(case)
    assert "컴퓨터공학과 졸업 소요학점은 140학점입니다." in prompt
    assert '"index": 1' in prompt
    assert "2026-11-10" in prompt
    assert "정답 라벨상 답변 가능: True" in prompt
    assert "짧은 화면용 발췌" not in prompt
    assert "모델용으로 잘린 문장…" not in prompt


@pytest.mark.parametrize("broken", ["missing_fact", "duplicate_fact", "invalid_claim", "fake_answer_quote"])
def test_incomplete_judgments_cannot_silently_pass(broken):
    judged = judgment()
    if broken == "missing_fact":
        judged["facts"] = []
    elif broken == "duplicate_fact":
        judged["facts"] *= 2
    elif broken == "invalid_claim":
        judged["claims"][0]["verdict"] = "probably"
    else:
        judged["claims"][0]["text"] = "답변에 없는 문장[1]."
    with pytest.raises(ValueError):
        validate_judgment(judged, result())


def test_fabricated_source_quote_is_unknown_and_does_not_raise_accuracy():
    judged = judgment()
    judged["claims"][0]["source_evidence"] = [{"source_index": 1, "quote": "졸업에는 130학점이 필요합니다."}]
    validated = validate_judgment(judged, result())
    assert validated["claims"][0]["verdict"] == "unverifiable"
    metrics = quality_summary([{**result(), "judged": validated}])["claims"]
    assert metrics["citation_accuracy"] is None
    assert metrics["evaluation_coverage"] == 0


def test_source_quote_from_an_uncited_document_does_not_prove_support():
    case, judged = result(), judgment()
    case["evaluation_evidence"]["sources"].append({"index": 2, "content": "다른 학과 140학점"})
    judged["claims"][0]["source_evidence"] = [{"source_index": 2, "quote": "다른 학과 140학점"}]
    assert validate_judgment(judged, case)["claims"][0]["verdict"] == "unverifiable"


def test_absence_in_a_legacy_excerpt_is_not_proof_of_unsupported_claim():
    case, judged = result(), judgment()
    del case["evaluation_evidence"]
    judged["claims"][0]["verdict"] = "unsupported"
    assert validate_judgment(judged, case)["claims"][0]["verdict"] == "unverifiable"


@pytest.mark.parametrize("citation", ["", "[99]"])
def test_uncited_claims_and_invalid_citation_indices_are_unsupported(citation):
    case, judged = result(), judgment()
    text = f"컴퓨터공학과는 140학점입니다{citation}."
    case["answer"] = text
    judged["claims"][0].update(text=text, citations=[] if not citation else [99])
    assert validate_judgment(judged, case)["claims"][0]["verdict"] == "unsupported"


def test_unlabeled_cases_still_get_source_quality_grading():
    case, judged = result(), unit_judgment()
    case["required_facts"] = []
    judged["facts"] = []
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(judged)))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=completion))))
    assert grade_case(client, "judge", case, 5)["claims"][0]["verdict"] == "supported"
    assert client.chat.completions.create.call_count == 1


def test_partial_fact_is_not_fully_covered_and_errors_stay_in_strict_denominator():
    partial = judgment()
    partial["facts"][0]["verdict"] = "partial"
    rows = [
        {**result(), "id": "partial", "judged": partial},
        {**result(), "id": "api_error", "error": "timeout", "skipped": "no_answer"},
        {**result(), "id": "judge_error", "judged": {"judge_error": "invalid_schema"}},
    ]
    summary = summarize(rows)
    assert summary["cases_fully_covered"] == []
    assert summary["answer_accuracy"]["labeled_cases"] == 3
    assert summary["answer_accuracy"]["evaluated_cases"] == 1
    assert summary["answer_accuracy"]["strict_rate_all_labeled"] == 0
    assert summary["api_error_case_ids"] == ["api_error"]
    assert summary["judge_error_case_ids"] == ["judge_error"]


def test_refusal_and_scope_errors_have_separate_denominators():
    refused = judgment()
    refused.update(claims=[], refusal={"verdict": "over_refusal", "sources_sufficient": True},
                   department={"verdict": "not_applicable"}, temporal={"verdict": "unverifiable"})
    wrong = judgment()
    wrong["department"]["verdict"] = "error"
    wrong["temporal"]["verdict"] = "error"
    rows = [{**result(), "id": "refused", "judged": refused}, {**result(), "id": "wrong", "judged": wrong}]
    quality = quality_summary(rows)
    assert quality["over_refusal"]["rate"] == 0.5
    assert quality["over_refusal"]["with_sufficient_retrieved_evidence"] == ["refused"]
    assert quality["department"]["error_rate"] == 1
    assert quality["temporal"]["error_rate"] == 1
    assert quality["temporal"]["unverifiable"] == 1


def baseline_files(directory):
    questions, results, graded = [directory / filename for filename in ("cases.jsonl", "raw.jsonl", "graded.jsonl")]
    case = valid_case()
    questions.write_text(json.dumps(case) + "\n", encoding="utf-8")
    raw = result()
    results.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    grade = {**raw, "judged": judgment()}
    graded.write_text(json.dumps(grade) + "\n", encoding="utf-8")
    summary = summarize([grade])
    summary["provenance"] = {
        "schema_version": 2, "judge_prompt_sha256": "judge-hash",
        "results_sha256": file_sha256(results), "graded_sha256": file_sha256(graded),
        "run_manifest": {
            "questions_sha256": file_sha256(questions), "expected_case_ids": [case["id"]],
            "finished_at": "2026-11-10", "selected_case_count": 1, "available_case_count": 1,
            "results_sha256": file_sha256(results), "data_snapshot": "fixture-v1", "backend_revision": "test-revision",
        },
    }
    graded.with_suffix(".summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return questions, results, graded


def test_baseline_records_completed_run_and_rejects_modified_raw_results(tmp_path):
    paths = baseline_files(tmp_path)
    assert build_baseline(*paths)["status"] == "measured"
    raw = json.loads(paths[1].read_text(encoding="utf-8"))
    raw["answer"] = "수정한 답변"
    paths[1].write_text(json.dumps(raw) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="modified"):
        build_baseline(*paths)


def test_baseline_rejects_subset_and_unidentified_data_snapshot(tmp_path):
    paths = baseline_files(tmp_path)
    summary_path = paths[2].with_suffix(".summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["provenance"]["run_manifest"]["available_case_count"] = 2
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="full run"):
        build_baseline(*paths)
    summary["provenance"]["run_manifest"].update(available_case_count=1, data_snapshot=None)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="data-snapshot"):
        build_baseline(*paths)


def test_runner_records_manifest_without_access_token(monkeypatch, tmp_path):
    questions = tmp_path / "questions.jsonl"
    questions.write_text(json.dumps(valid_case()) + "\n", encoding="utf-8")
    output = tmp_path / "results.jsonl"
    monkeypatch.setattr("eval.tools.eval_ask.code_state", lambda: {"git_commit": "fixture"})
    post = Mock(return_value=(200, result()))
    monkeypatch.setattr("eval.tools.eval_ask._post_ask", post)
    run_eval(base_url="https://user:password@example.com/?secret=hidden", questions_path=questions,
             output_path=output, timeout=5, limit=None, sleep_seconds=0,
             token="access-token-secret", data_snapshot="snapshot-v1", backend_revision="fixture")
    manifest_text = output.with_suffix(".manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["finished_at"]
    assert manifest["results_sha256"] == file_sha256(output)
    assert manifest["questions_sha256"] == file_sha256(questions)
    assert manifest["base_url"] == "https://example.com/"
    assert "access-token-secret" not in manifest_text
    assert "password" not in manifest_text
    assert "hidden" not in manifest_text
    assert post.call_args.kwargs["token"] == "access-token-secret"


def test_runner_stops_on_old_backend_without_a_false_completed_manifest(monkeypatch, tmp_path):
    questions = tmp_path / "questions.jsonl"
    questions.write_text(json.dumps(valid_case()) + "\n", encoding="utf-8")
    output = tmp_path / "results.jsonl"
    monkeypatch.setattr("eval.tools.eval_ask.code_state", lambda: {})
    monkeypatch.setattr("eval.tools.eval_ask._post_ask", Mock(return_value=(200, {"answer": "답변", "sources": []})))
    with pytest.raises(RuntimeError, match="updated backend"):
        run_eval(base_url="http://localhost:8000", questions_path=questions, output_path=output,
                 timeout=5, limit=None, sleep_seconds=0)
    manifest = json.loads(output.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["finished_at"] is None


def test_judge_schema_error_is_visible_instead_of_becoming_an_empty_success():
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"facts": [], "forbidden": []}'))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=completion))))
    assert grade_case(client, "judge", result(), 5)["judge_error"] == "invalid_schema"


def client_with_responses(*responses):
    completions = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)))])
                   for response in responses]
    create = Mock(side_effect=completions)
    completions_client = SimpleNamespace(create=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions_client))
    return client, create


def test_answer_units_keep_decimal_values_list_markers_and_original_citations():
    answer = "1. 평점 3.50 이상입니다[2]. 금액은 2,000,000원입니다. [3]\n\n확인이 필요합니다."
    units = answer_units(answer)
    assert [unit["text"] for unit in units] == [
        "1. 평점 3.50 이상입니다[2].", "금액은 2,000,000원입니다. [3]", "확인이 필요합니다.",
    ]
    assert [unit["citations"] for unit in units] == [[2], [3], []]
    assert [unit["id"] for unit in units] == ["u001", "u002", "u003"]
    assert all(unit["text"] in answer for unit in units)


def test_anchored_claim_gets_text_and_citations_from_the_original_answer():
    validated = validate_judgment(unit_judgment(), result(), require_unit_ids=True)
    assert validated["claims"][0]["text"] == result()["answer"]
    assert validated["claims"][0]["citations"] == [1]
    assert validated["claims"][0]["verdict"] == "supported"


@pytest.mark.parametrize("ids", [[], ["u001", "u001"], ["u999"]])
def test_answer_unit_coverage_rejects_missing_duplicate_and_unknown_ids(ids):
    judged = unit_judgment()
    judged["claims"] = [{"unit_id": key, "verdict": "not_factual"} for key in ids]
    with pytest.raises(ValueError, match="every answer unit"):
        validate_judgment(judged, result(), require_unit_ids=True)


@pytest.mark.parametrize("override", [{"text": "요약해서 바꾼 문장"}, {"citations": [2]}])
def test_judge_cannot_override_the_text_or_citations_of_a_known_unit(override):
    judged = unit_judgment()
    judged["claims"][0].update(override)
    with pytest.raises(ValueError, match="anchored answer unit"):
        validate_judgment(judged, result(), require_unit_ids=True)


def test_non_factual_refusal_is_not_counted_as_an_unsupported_claim():
    case, judged = result(), unit_judgment()
    case.update(answer="관련 정보를 찾을 수 없습니다.", answerable=False, required_facts=[])
    judged.update(facts=[], claims=[{"unit_id": "u001", "verdict": "not_factual"}],
                  refusal={"verdict": "appropriate", "answer_unit_id": "u001"},
                  department={"verdict": "not_applicable"})
    validated = validate_judgment(judged, case, require_unit_ids=True)
    metrics = quality_summary([{**case, "judged": validated}])["claims"]
    assert metrics["total"] == 0
    assert metrics["unsupported"] == 0
    assert metrics["non_factual_units"] == 1
    assert metrics["citation_accuracy"] is None


def test_covered_fact_requires_a_real_answer_unit_reference():
    judged = unit_judgment()
    judged["facts"][0]["answer_unit_ids"] = []
    with pytest.raises(ValueError, match="requires answer_unit_ids"):
        validate_judgment(judged, result(), require_unit_ids=True)


def test_anchor_ids_do_not_make_fabricated_source_quotes_valid():
    judged = unit_judgment()
    judged["claims"][0]["source_evidence"] = [{"source_index": 1, "quote": "졸업 소요학점은 130학점입니다."}]
    validated = validate_judgment(judged, result(), require_unit_ids=True)
    assert validated["claims"][0]["verdict"] == "unverifiable"


def test_scope_evidence_can_reference_an_original_answer_unit():
    judged = unit_judgment()
    del judged["department"]["answer_evidence"]
    judged["department"]["answer_unit_id"] = "u001"
    validated = validate_judgment(judged, result(), require_unit_ids=True)
    assert validated["department"]["answer_evidence"] == result()["answer"]
    assert validated["department"]["verdict"] == "correct"


def test_schema_repair_retries_once_and_keeps_the_failure_for_audit():
    client, create = client_with_responses({"facts": []}, unit_judgment())
    graded = grade_case(client, "judge", result(), 5)
    assert create.call_count == 2
    assert graded["schema_repair_count"] == 1
    assert graded["judge_contract_version"] == JUDGE_CONTRACT_VERSION
    assert graded["schema_errors"][0]["kind"] == "invalid_schema"
    assert graded["claims"][0]["text"] == result()["answer"]
    assert "JSON 검증 실패" in create.call_args.kwargs["messages"][-1]["content"]


def test_invalid_fact_enum_is_repaired_instead_of_silently_relabelled():
    invalid = unit_judgment()
    invalid["facts"][0]["verdict"] = "violated"
    client, create = client_with_responses(invalid, unit_judgment())
    graded = grade_case(client, "judge", result(), 5)
    assert create.call_count == 2
    assert graded["schema_errors"][0]["reason"] == "invalid fact verdict"
    assert graded["facts"][0]["verdict"] == "covered"


@pytest.mark.parametrize("retries", [0, 1])
def test_schema_repair_is_bounded_and_never_marks_an_invalid_result_successful(retries):
    client, create = client_with_responses(*([{"facts": []}] * (retries + 1)))
    graded = grade_case(client, "judge", result(), 5, schema_retries=retries)
    assert create.call_count == retries + 1
    assert graded["judge_error"] == "invalid_schema"


def test_grading_cli_returns_failure_when_judge_errors_remain(monkeypatch):
    from eval.tools import grade_eval_results
    monkeypatch.setattr(grade_eval_results, "parse_args", lambda: SimpleNamespace(
        results=Path("raw.jsonl"), output=Path("graded.jsonl"), model="judge", timeout=5,
        limit=None, sleep=0, schema_retries=1))
    monkeypatch.setattr(grade_eval_results, "run", lambda *args: {"judge_error_case_ids": ["case_1"]})
    assert grade_eval_results.main() == 2


def test_regrading_keeps_raw_evidence_unchanged_and_records_the_new_contract(monkeypatch, tmp_path):
    from eval.tools import grade_eval_results
    raw_path = tmp_path / "raw.jsonl"
    raw_path.write_text(json.dumps(result()) + "\n", encoding="utf-8")
    original_hash = file_sha256(raw_path)
    client, create = client_with_responses(unit_judgment())
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    monkeypatch.setattr(grade_eval_results, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(grade_eval_results, "code_state", lambda: {"code_sha256": "fixture"})
    graded_path = tmp_path / "regraded.jsonl"
    summary = grade_eval_results.run(raw_path, graded_path, "judge", 5, None, 0)
    assert file_sha256(raw_path) == original_hash
    saved = json.loads(graded_path.read_text(encoding="utf-8"))
    assert saved["evaluation_evidence"] == result()["evaluation_evidence"]
    assert summary["provenance"]["judge_contract_version"] == JUDGE_CONTRACT_VERSION
    assert not summary["judge_error_case_ids"]
    assert create.call_count == 1


def test_baseline_keeps_api_429_as_a_measured_failure_instead_of_requiring_new_answers(tmp_path):
    paths = baseline_files(tmp_path)
    raw = result()
    raw.update(answer="", status_code=429, error={"detail": "일일 한도 소진"}, evaluation_evidence=None, sources=[])
    paths[1].write_text(json.dumps(raw) + "\n", encoding="utf-8")
    grade = {**raw, "judged": {"facts": [], "forbidden": []}, "skipped": "no_answer"}
    paths[2].write_text(json.dumps(grade) + "\n", encoding="utf-8")
    summary_path = paths[2].with_suffix(".summary.json")
    previous = json.loads(summary_path.read_text(encoding="utf-8"))
    summary = summarize([grade])
    summary["provenance"] = previous["provenance"]
    summary["provenance"].update(results_sha256=file_sha256(paths[1]), graded_sha256=file_sha256(paths[2]))
    summary["provenance"]["run_manifest"]["results_sha256"] = file_sha256(paths[1])
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    baseline = build_baseline(*paths)
    assert baseline["status"] == "measured"
    assert baseline["summary"]["answer_accuracy"]["strict_rate_all_labeled"] == 0
    assert baseline["summary"]["api_error_case_ids"] == [raw["id"]]


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_result_reader_keeps_unicode_line_separators_inside_source_bodies(tmp_path, separator):
    path = tmp_path / "results.jsonl"
    row = {"id": "case_1", "content": f"첫 문장{separator}둘째 문장"}
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    assert _result_rows(path) == [row]
