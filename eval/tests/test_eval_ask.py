from __future__ import annotations

import unittest
from pathlib import Path

from eval.tools.audit_eval_question_bank import audit
from eval.tools.audit_regression_labels import audit as audit_regression
from eval.tools.build_eval_question_bank import TARGET_COUNTS, build_rows, validate_rows
from eval.tools.build_regression_template import build_template_rows
from eval.tools.eval_ask import (
    DEFAULT_OUTPUT_PATH,
    DEFAULT_QUESTIONS_PATH,
    _is_structured_case,
    _read_jsonl,
    _result_row,
    _validate_structured_case,
    _validate_unique_ids,
)


def valid_case() -> dict:
    return {
        "id": "rule_test_001",
        "question": "질문",
        "category": "규정",
        "answerable": True,
        "expected_source": {
            "dataset": ["rule"],
            "title_keywords": ["학칙"],
            "allowed_urls": [],
            "source_ids": [],
        },
        "required_facts": [
            {"id": "fact_1", "description": "사실", "keywords": ["키워드"]}
        ],
        "forbidden_claims": [],
        "expected_no_info": False,
        "tags": ["테스트"],
        "difficulty": "easy",
    }


class EvalSchemaTests(unittest.TestCase):
    def test_result_keeps_label_and_response_answerability_separate_and_retains_evidence(self):
        evidence = {"schema_version": 1, "body_scope": "retrieved_chunks", "sources": [{"index": 1, "content": "원문"}]}
        row = _result_row(
            question_row=valid_case(), status_code=200, elapsed_seconds=1.25,
            response={"answer": "관련 정보를 찾을 수 없습니다.", "answerable": False,
                      "sources": [], "evaluation_evidence": evidence},
        )
        self.assertIs(row["answerable"], True)
        self.assertIs(row["response_answerable"], False)
        self.assertEqual(evidence, row["evaluation_evidence"])

    def test_accepts_answerable_case(self) -> None:
        _validate_structured_case(valid_case(), location="case")

    def test_accepts_unanswerable_case(self) -> None:
        case = valid_case()
        case.update(
            answerable=False,
            expected_no_info=True,
            expected_source=None,
            required_facts=[],
        )
        _validate_structured_case(case, location="case")

    def test_rejects_inconsistent_no_info(self) -> None:
        case = valid_case()
        case["expected_no_info"] = True
        with self.assertRaisesRegex(ValueError, "inverse of answerable"):
            _validate_structured_case(case, location="case")

    def test_rejects_duplicate_case_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate case ids"):
            _validate_unique_ids([valid_case(), valid_case()], location="cases")

    def test_legacy_question_remains_readable(self) -> None:
        self.assertFalse(_is_structured_case({"question": "기존 질문", "category": "규정"}))

    def test_repository_eval_files_are_readable(self) -> None:
        smoke = _read_jsonl(Path("eval/cases/smoke.jsonl"))
        challenge = _read_jsonl(Path("eval/cases/challenge.jsonl"))
        regression, statuses = audit_regression(Path("eval/cases/regression.jsonl"))
        self.assertEqual(len(smoke), 8)
        self.assertEqual(len(regression), 100)
        self.assertEqual(len(challenge), 5)
        self.assertTrue(all(case.get("id") for case in smoke + challenge))
        self.assertGreaterEqual(len(regression), len(smoke))
        # Every regression case was reviewed and promoted in ec2f264.
        self.assertEqual(statuses["ready"], 100)
        self.assertEqual(DEFAULT_QUESTIONS_PATH, Path("eval/cases/smoke.jsonl"))
        self.assertEqual(DEFAULT_OUTPUT_PATH, Path("eval/results/smoke.jsonl"))

    def test_balanced_draft_question_bank(self) -> None:
        rows = audit(Path("eval/drafts/question_bank_100.jsonl"))
        self.assertEqual(len(rows), 100)
        self.assertEqual(
            {case_type: sum(row["case_type"] == case_type for row in rows) for case_type in TARGET_COUNTS},
            TARGET_COUNTS,
        )
        self.assertTrue(all(row["label_status"] == "needs_review" for row in rows))

    def test_builder_source_matches_generated_bank(self) -> None:
        rows = build_rows()
        validate_rows(rows)
        generated = audit(Path("eval/drafts/question_bank_100.jsonl"))
        self.assertEqual(rows, generated)

    def test_regression_cases_keep_question_bank_structure(self) -> None:
        # Reviewers fill in the label fields; everything else must still match
        # the template generated from the question bank.
        label_fields = {
            "label_status", "review_notes", "tags", "answerable", "expected_no_info",
            "expected_source", "required_facts", "forbidden_claims", "expected_behavior",
        }
        expected = build_template_rows()
        actual, _ = audit_regression(Path("eval/cases/regression.jsonl"))
        self.assertEqual(
            [{k: v for k, v in row.items() if k not in label_fields} for row in expected],
            [{k: v for k, v in row.items() if k not in label_fields} for row in actual],
        )
        self.assertTrue(all(isinstance(row["answerable"], bool) for row in actual))
        self.assertTrue(all(row["expected_no_info"] is (not row["answerable"]) for row in actual))


if __name__ == "__main__":
    unittest.main()
