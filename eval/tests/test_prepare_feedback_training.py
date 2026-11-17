"""Feedback collection, freshness checks and complete dataset publication; no live API."""

from copy import deepcopy
from http.client import IncompleteRead
from io import BytesIO
import json
import sys
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from backend.app.feedback_training import build_training_candidate
from eval.tools import llm_budget
from eval.tools import prepare_feedback_training as prepare


def candidate(number=1):
    return build_training_candidate({
        "id": f"00000000-0000-0000-0000-{number:012d}", "rating": "down",
        "updated_at": "2026-11-11T10:00:00+00:00", "reasons": ["부정확해요"], "comment": "조건이 틀려요",
        "chat_messages": {"role": "assistant", "content": "기존 오답", "sources": [],
                          "created_at": "2026-11-11T09:01:00+00:00"},
    }, [{"content": f"전공필수 조건 {number}?", "created_at": "2026-11-11T09:00:00+00:00"}])


def approve(row):
    reviewed = deepcopy(row)
    reviewed["question"] += " (컴퓨터공학과 기준)"
    reviewed["review"].update({
        "status": "approved", "split": "train", "corrected_answer": "공식 문서에서 확인한 정답",
        "verified_sources": ["https://example.edu/official-rule"],
        "reviewed_by": "reviewer", "reviewed_at": "2026-11-11T20:00:00+09:00",
        "privacy_checked": True, "context_checked": True,
    })
    return reviewed


def mock_api(monkeypatch, pages):
    opener = Mock()
    opener.open.side_effect = [BytesIO(json.dumps(page).encode()) for page in pages]
    monkeypatch.setattr(prepare, "build_opener", lambda *args: opener)
    return opener


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["prepare_feedback_training", *map(str, args)])
    monkeypatch.setattr(prepare, "DEFAULT_HOLDOUTS", [])
    return prepare.main()


def test_collects_all_pages_with_admin_auth_and_frontend_prefix(monkeypatch):
    first, second = candidate(), candidate(2)
    opener = mock_api(monkeypatch, [
        {"items": [first], "nextCursor": first["id"]},
        {"items": [second], "nextCursor": None},
    ])
    assert prepare.fetch_candidates("https://campus.example/api/", "secret", page_size=1) == [first, second]
    requests = [call.args[0] for call in opener.open.call_args_list]
    assert requests[0].full_url == "https://campus.example/api/admin/feedback/training-candidates?limit=1"
    assert requests[0].get_header("Authorization") == "Bearer secret"
    assert requests[0].get_method() == "GET"
    assert "after_id=" + first["id"] in requests[1].full_url


@pytest.mark.parametrize("page", [
    {"items": [], "nextCursor": "stuck"},
    {"items": [candidate()], "nextCursor": "different"},
    {"items": [candidate()]},
    {"items": "invalid", "nextCursor": None},
    {"items": [candidate(), candidate()], "nextCursor": None},
])
def test_invalid_pages_fail(monkeypatch, page):
    mock_api(monkeypatch, [page])
    with pytest.raises(ValueError):
        prepare.fetch_candidates("http://localhost:8000", "secret")


def test_duplicate_across_pages_cannot_loop(monkeypatch):
    row = candidate()
    mock_api(monkeypatch, [{"items": [row], "nextCursor": row["id"]},
                           {"items": [row], "nextCursor": row["id"]}])
    with pytest.raises(ValueError, match="did not advance"):
        prepare.fetch_candidates("http://localhost:8000", "secret")


@pytest.mark.parametrize("field,value", [
    ("rating", "up"), ("review", {"status": "approved"}), ("pairing_status", []),
    ("feedback_updated_at", "2026-11-11"), ("original_question_key", "0" * 64),
])
def test_untrusted_export_fields_are_rejected(field, value):
    row = candidate()
    row[field] = value
    with pytest.raises(ValueError):
        prepare.validate_candidates([row])


@pytest.mark.parametrize("url", [
    "http://school.example:8000", "https://user:password@school.example", "file:///tmp/data",
    "https://school.example?token=private", "https://school.example/#fragment",
])
def test_rejects_unsafe_token_destinations(url):
    with pytest.raises(ValueError):
        prepare.feedback_endpoint(url)


def test_redirect_handler_does_not_forward_credentials():
    assert prepare.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.example") is None


@pytest.mark.parametrize("status,hint", [(401, "expired"), (403, "ADMIN_EMAILS"), (404, "deploy"), (302, "failed")])
def test_http_error_hides_response_body(monkeypatch, status, hint):
    opener = Mock()
    opener.open.side_effect = HTTPError("https://school.example", status, "private message", {}, BytesIO(b"secret"))
    monkeypatch.setattr(prepare, "build_opener", lambda *args: opener)
    with pytest.raises(ValueError, match=hint) as error:
        prepare.fetch_candidates("https://school.example", "secret")
    assert "secret" not in str(error.value)
    assert "private message" not in str(error.value)


def test_merge_keeps_reviewed_question_and_new_pending_rows_without_mutating_source():
    first, new = candidate(), candidate(2)
    reviewed = approve(first)
    merged = prepare.merge_reviews([first, new], [reviewed])
    assert merged == [reviewed, new]
    assert first["review"]["status"] == "pending"


@pytest.mark.parametrize("field,value", [
    ("feedback_updated_at", "2026-11-11T11:00:00+00:00"),
    ("rejected_answer", "변경된 답변"), ("answer_sources", [{"url": "https://example.edu/new"}]),
    ("comment", "변경된 의견"), ("original_question_key", "0" * 64),
])
def test_approved_changed_feedback_aborts(field, value):
    current = candidate()
    reviewed = approve(current)
    current[field] = value
    with pytest.raises(ValueError, match="changed or disappeared"):
        prepare.merge_reviews([current], [reviewed])


def test_withdrawn_approval_aborts_and_changed_pending_is_reset():
    first, second = candidate(), candidate(2)
    with pytest.raises(ValueError, match="changed or disappeared"):
        prepare.merge_reviews([second], [approve(first)])
    changed = deepcopy(first)
    changed["comment"] = "변경된 의견"
    changed["review"]["corrected_answer"] = "검토 중인 초안"
    assert prepare.merge_reviews([first], [changed]) == [first]
    with pytest.raises(ValueError, match="duplicate reviewed"):
        prepare.merge_reviews([first], [first, first])


def test_offline_collection_produces_review_without_training(tmp_path, monkeypatch):
    source, output = tmp_path / "download.jsonl", tmp_path / "round1"
    write_rows(source, [candidate()])
    assert invoke(monkeypatch, "--input", source, "--output-dir", output) == 0
    assert prepare.read_candidates(output / "candidates.jsonl") == [candidate()]
    assert prepare.read_candidates(output / "review.jsonl")[0]["review"]["status"] == "pending"
    assert not (output / "train.jsonl").exists()
    assert not json.loads((output / "summary.json").read_text())["dataset_built"]


def test_api_and_review_build_training_in_one_command(tmp_path, monkeypatch):
    row = candidate()
    reviewed, output = tmp_path / "review.jsonl", tmp_path / "dataset"
    write_rows(reviewed, [approve(row)])
    mock_api(monkeypatch, [{"items": [row], "nextCursor": None}])
    monkeypatch.setenv("FEEDBACK_API_TOKEN", "secret")
    assert invoke(monkeypatch, "--base-url", "http://localhost:8000", "--reviewed", reviewed,
                  "--output-dir", output) == 0
    messages = prepare.read_candidates(output / "train.jsonl")[0]["messages"]
    assert messages == [{"role": "user", "content": approve(row)["question"]},
                        {"role": "assistant", "content": approve(row)["review"]["corrected_answer"]}]
    summary = json.loads((output / "summary.json").read_text())
    assert summary["freshness_checked"] is True and summary["train"] == 1
    assert "secret" not in (output / "summary.json").read_text()


@pytest.mark.parametrize("process_token", [None, "process-token"])
def test_collection_loads_dotenv_token_without_prompt_and_preserves_process_override(tmp_path, monkeypatch, capsys, process_token):
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    (backend_dir / ".env").write_text("FEEDBACK_API_TOKEN=dotenv-token\n", encoding="utf-8")
    monkeypatch.setattr(llm_budget, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("FEEDBACK_API_TOKEN", raising=False)
    if process_token:
        monkeypatch.setenv("FEEDBACK_API_TOKEN", process_token)
    monkeypatch.setattr(prepare, "getpass", Mock(side_effect=AssertionError("token should come from environment")))
    opener = mock_api(monkeypatch, [{"items": [candidate()], "nextCursor": None}])
    output = tmp_path / "collected"
    assert invoke(monkeypatch, "--base-url", "http://localhost:8000", "--output-dir", output) == 0
    expected = process_token or "dotenv-token"
    assert opener.open.call_args.args[0].get_header("Authorization") == "Bearer " + expected
    captured = capsys.readouterr()
    assert expected not in captured.out + captured.err
    assert expected not in (output / "summary.json").read_text(encoding="utf-8")


def test_failed_second_page_publishes_nothing(tmp_path, monkeypatch):
    row, output = candidate(), tmp_path / "dataset"
    opener = mock_api(monkeypatch, [])
    opener.open.side_effect = [BytesIO(json.dumps({"items": [row], "nextCursor": row["id"]}).encode()),
                               URLError("connection interrupted")]
    monkeypatch.setenv("FEEDBACK_API_TOKEN", "secret")
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--base-url", "http://localhost:8000", "--output-dir", output)
    assert error.value.code == 2
    assert not output.exists()


def test_interrupted_response_is_a_clean_failure(monkeypatch):
    opener = Mock()
    opener.open.side_effect = IncompleteRead(b"partial response")
    monkeypatch.setattr(prepare, "build_opener", lambda *args: opener)
    with pytest.raises(ValueError, match="connection failed"):
        prepare.fetch_candidates("http://localhost:8000", "secret")


def test_additional_holdout_is_reserved_by_combined_command(tmp_path, monkeypatch):
    row = candidate()
    source, reviewed = tmp_path / "source.jsonl", tmp_path / "reviewed.jsonl"
    holdout, output = tmp_path / "holdout.jsonl", tmp_path / "dataset"
    write_rows(source, [row])
    write_rows(reviewed, [approve(row)])
    write_rows(holdout, [{"question": approve(row)["question"]}])
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--input", source, "--reviewed", reviewed,
               "--holdout", holdout, "--output-dir", output)
    assert error.value.code == 2
    assert not output.exists()


def test_no_approvals_cannot_create_empty_dataset(tmp_path, monkeypatch):
    source, output = tmp_path / "source.jsonl", tmp_path / "dataset"
    write_rows(source, [candidate()])
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--input", source, "--reviewed", source, "--output-dir", output)
    assert error.value.code == 2
    assert not output.exists()


def test_invalid_approval_writes_nothing_and_existing_output_is_preserved(tmp_path, monkeypatch):
    source, reviewed, output = tmp_path / "source.jsonl", tmp_path / "reviewed.jsonl", tmp_path / "dataset"
    write_rows(source, [candidate()])
    invalid = approve(candidate())
    invalid["review"]["privacy_checked"] = False
    write_rows(reviewed, [invalid])
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "--input", source, "--reviewed", reviewed, "--output-dir", output)
    assert not output.exists()
    output.mkdir()
    marker = output / "train.jsonl"
    marker.write_text("keep existing output", encoding="utf-8")
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "--input", source, "--output-dir", output)
    assert marker.read_text(encoding="utf-8") == "keep existing output"


def test_write_failure_cannot_publish_partial_dataset(tmp_path, monkeypatch):
    source, output = tmp_path / "source.jsonl", tmp_path / "dataset"
    write_rows(source, [candidate()])
    original = prepare._write_jsonl

    def fail_after_first_file(path, rows):
        if path.name == "review.jsonl":
            raise OSError("disk full")
        original(path, rows)

    monkeypatch.setattr(prepare, "_write_jsonl", fail_after_first_file)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "--input", source, "--output-dir", output)
    assert not output.exists()
    assert not list(tmp_path.glob(".feedback-*"))
