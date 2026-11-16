"""Exercise the standalone editor's data contract using Node, without a browser."""

from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from backend.app.feedback_training import build_training_candidate, question_key
from eval.tools.build_feedback_training import build_datasets
from eval.tools.automate_feedback_review import package_synthetic


EDITOR = Path(__file__).resolve().parents[1] / "tools" / "feedback_review_editor.html"
NODE = shutil.which("node")


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.scripts = {}

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = dict(attrs).get("id")
            if self.current:
                self.scripts[self.current] = ""

    def handle_endtag(self, tag):
        if tag == "script":
            self.current = None

    def handle_data(self, data):
        if self.current:
            self.scripts[self.current] += data


def original():
    row = build_training_candidate({
        "id": "00000000-0000-0000-0000-000000000001", "rating": "down",
        "updated_at": "2026-11-14T10:00:00+00:00", "reasons": ["부정확해요"], "comment": "조건이 달라요",
        "chat_messages": {"role": "assistant", "content": "잘못된 기존 답변", "sources": [],
                          "created_at": "2026-11-14T09:01:00+00:00"},
    }, [{"content": "컴퓨터공학과 전공필수 이수 조건?", "created_at": "2026-11-14T09:00:00+00:00"}])
    row["additional_provenance"] = {"must": ["survive", {"nested": True}]}
    row["review"]["custom_notes"] = "추가 검수 정보도 보존"
    return row


def ready():
    row = original()
    row["review"].update({"corrected_answer": "공식 자료에서 확인한 수정 답변",
                          "verified_sources": ["https://example.edu/rules/1"],
                          "required_facts": ["이수 조건"], "forbidden_claims": [],
                          "split": "train", "reviewed_by": "human",
                          "privacy_checked": True, "context_checked": True})
    return row


def run_js(body, payload):
    if not NODE:
        pytest.skip("Node is needed for standalone editor contract tests")
    parser = Scripts()
    parser.feed(EDITOR.read_text(encoding="utf-8"))
    script = (parser.scripts["review-core"] + "\nconst core = globalThis.FeedbackReview;\nconst payload = "
              + json.dumps(payload, ensure_ascii=True) + ";\nconst result = (() => {\n" + body
              + "\n})();\nprocess.stdout.write(JSON.stringify(result));\n")
    result = subprocess.run([NODE], input=script, text=True, encoding="utf-8", capture_output=True,
                            timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_bom_crlf_roundtrip_preserves_all_fields_and_order():
    row = original()
    other = deepcopy(row)
    other["id"] = "00000000-0000-0000-0000-000000000002"
    other["review"]["status"] = "rejected"
    source = "\ufeff" + json.dumps(row) + "\r\n\r\n" + json.dumps(other) + "\r\n"
    output = run_js("const rows = core.parseReviews(payload); return core.serialize(rows, core.clone(rows));", source)
    assert [json.loads(line) for line in output.splitlines()] == [row, other]


def test_question_edit_preserves_original_keys_and_unknown_fields():
    row = original()
    edited = run_js("return core.edit(payload, 'question', '개인정보를 제거하고 맥락을 보강한 질문');", row)
    assert edited["question"] != row["question"]
    assert {key: value for key, value in edited.items() if key not in {"question", "review"}} == {
        key: value for key, value in row.items() if key not in {"question", "review"}}
    assert edited["review"]["custom_notes"] == row["review"]["custom_notes"]


@pytest.mark.parametrize("field", ["id", "original_question_key", "rejected_answer", "answer_sources"])
def test_provenance_fields_cannot_be_edited(field):
    error = run_js("try { core.edit(payload.row, payload.field, 'changed'); return null; } catch (e) { return e.message; }",
                   {"row": original(), "field": field})
    assert error is not None


def test_export_detects_changed_original_data():
    error = run_js("const changed = core.clone(payload); changed.rejected_answer = 'changed'; "
                   "try { core.serialize([changed], [payload]); return null; } catch (e) { return e.message; }", original())
    assert error is not None


def test_editing_approved_answer_requires_fresh_privacy_context_checks():
    edited = run_js("const row = core.approve(payload, '2026-11-14T21:00:00+09:00'); "
                    "return core.edit(row, 'corrected_answer', '수정한 새로운 정답');", ready())
    assert edited["review"]["status"] == "pending"
    assert edited["review"]["privacy_checked"] is False
    assert edited["review"]["context_checked"] is False
    assert edited["review"]["reviewed_at"] == ""


@pytest.mark.parametrize("field,value", [
    ("privacy_checked", False), ("context_checked", False), ("reviewed_by", ""), ("split", None),
    ("corrected_answer", "잘못된 기존 답변"), ("verified_sources", []),
    ("verified_sources", ["javascript:alert(1)"]), ("verified_sources", ["https://name:password@example.edu/rule"]),
])
def test_missing_review_requirements_block_approval(field, value):
    row = ready()
    row["review"][field] = value
    error = run_js("try { core.approve(payload, '2026-11-14T21:00:00+09:00'); return null; } catch (e) { return e.message; }", row)
    assert error is not None


def test_editor_export_is_accepted_by_existing_python_training_builder():
    row = ready()
    output = run_js("const approved = core.approve(payload, '2026-11-14T21:00:00+09:00'); "
                    "return core.serialize([approved], [payload]);", row)
    edited = json.loads(output)
    assert edited["additional_provenance"] == row["additional_provenance"]
    assert edited["review"]["custom_notes"] == row["review"]["custom_notes"]
    train, evaluation, skipped = build_datasets([edited], [])
    assert train[0]["messages"][1]["content"] == row["review"]["corrected_answer"]
    assert evaluation == [] and skipped == 0


def test_cross_split_source_leakage_blocks_export():
    first, second = ready(), ready()
    second.update(id="00000000-0000-0000-0000-000000000002", question="다른 질문?",
                  original_question_key=question_key("다른 질문?"))
    second["review"]["split"] = "evaluation"
    error = run_js("const rows = payload.map(row => core.approve(row, '2026-11-14T21:00:00+09:00')); "
                   "try { core.serialize(rows, payload); return null; } catch (e) { return e.message; }", [first, second])
    assert error is not None


def test_duplicate_ids_and_synthetic_rows_are_rejected():
    row = original()
    duplicate_error = run_js("try { core.parseReviews(payload); return null; } catch (e) { return e.message; }",
                             json.dumps(row) + "\n" + json.dumps(row))
    assert duplicate_error is not None
    row.update(kind="synthetic_feedback_qa", synthetic=True)
    synthetic_error = run_js("try { core.parseReviews(payload); return null; } catch (e) { return e.message; }", json.dumps(row))
    assert synthetic_error is not None


def test_evidence_for_another_question_cannot_be_connected():
    row = original()
    evidence = {"feedback_id": row["id"], "original_question_key": "0" * 64, "sources": []}
    error = run_js("try { core.parseEvidence(payload.input, [payload.row]); return null; } catch (e) { return e.message; }",
                   {"input": json.dumps(evidence), "row": row})
    assert error is not None


def test_incomplete_approved_import_cannot_be_silently_exported():
    row = original()
    row["review"]["status"] = "approved"
    error = run_js("const rows = core.parseReviews(payload); "
                   "try { core.serialize(rows, core.clone(rows)); return null; } catch (e) { return e.message; }", json.dumps(row))
    assert error is not None


@pytest.mark.parametrize("timestamp", ["2026-02-30T12:00:00+09:00", "2026-11-14T24:00:00+09:00", "2026-11-14T21:00:00"])
def test_invalid_calendar_or_timezone_cannot_be_used_for_approval(timestamp):
    error = run_js("try { core.approve(payload.row, payload.timestamp); return null; } catch (e) { return e.message; }",
                   {"row": ready(), "timestamp": timestamp})
    assert error is not None


def test_packaged_synthetic_can_be_reviewed_without_fabricating_a_downvote():
    row = original()
    candidate = {"id": "00000000-0000-0000-0000-000000000099", "schema_version": 1,
                 "kind": "synthetic_feedback_qa", "synthetic": True,
                 "parent_feedback_id": row["id"], "parent_question_key": row["original_question_key"],
                 "question": "합성 관련 질문?", "proposed_answer": "검수할 초안", "required_facts": ["사실"],
                 "forbidden_claims": [], "citations": [], "review": {"status": "pending"}}
    packaged = package_synthetic(candidate, row)
    packaged["review"].update({"split": "train", "reviewed_by": "human", "privacy_checked": True,
                               "context_checked": True, "verified_sources": ["https://example.edu/rule"]})
    result = run_js("const rows = core.parseReviews(payload); const approved = core.approve(rows[0], '2026-11-16T10:00:00+09:00'); "
                    "return core.serialize([approved], rows);", json.dumps(packaged))
    saved = json.loads(result)
    assert saved["review"]["status"] == "approved" and "rating" not in saved
    assert saved["parent_feedback_id"] == row["id"]


def test_editor_evidence_can_match_a_synthetic_only_review_queue():
    row = original()
    child = {"id": "00000000-0000-0000-0000-000000000099", "schema_version": 1,
             "kind": "synthetic_feedback_qa", "synthetic": True, "parent_feedback_id": row["id"],
             "parent_question_key": row["original_question_key"], "question": "관련 합성 질문?",
             "proposed_answer": "정답 초안", "review": {"status": "pending"}}
    packaged = package_synthetic(child, row)
    snapshot = {"feedback_id": row["id"], "original_question_key": row["original_question_key"],
                "sources": [{"evidence_id": "e1", "content": "공식 자료"}]}
    assert run_js("return core.parseEvidence(payload.evidence, [payload.row]).size;",
                  {"row": packaged, "evidence": json.dumps(snapshot)}) == 1
