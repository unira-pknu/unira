from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from eval.tools import llm_budget
from eval.tools.generate_testset import rule_based_qa, select_chunks
from eval.tools.llm_budget import BudgetedLLM, BudgetExceeded, FatalLLMError
from eval.tools.run_evaluation import answer_in_context
from scripts.vectorization import layout_rag
from scripts.vectorization.layout_rag import (
    LocalVectorStore,
    build_image_chunks,
    collect_chunks,
    diagram_to_sentences,
    load_extraction_index,
    split_prose,
    split_table,
    store_path,
    table_to_markdown_rows,
)

FIXTURES = Path(__file__).resolve().parents[2] / "scripts" / "tests" / "fixtures" / "layout_rag"


def review(layout: str, expected: str = "", digest: str = "ab" * 32) -> dict:
    return {"source_path": "files/ce/output/json/x.json", "image_sha256": digest,
            "reviewer_layout_type": layout, "expected_text_or_structure": expected}


class TableTests(unittest.TestCase):
    def test_positioned_cells_render_markdown_with_spans(self) -> None:
        table = {"title": "학점", "cells": [
            {"text": "전공", "row": 0, "column": 0}, {"text": "학점", "row": 0, "column": 1},
            {"text": "컴공", "row": 1, "column": 0, "rowspan": 2}, {"text": "130", "row": 1, "column": 1},
            {"text": "132", "row": 2, "column": 1},
            {"text": "비고 a|b", "row": 3, "column": 0, "colspan": 2},
        ]}
        caption, header, body = table_to_markdown_rows(table)
        self.assertEqual("학점", caption)
        self.assertEqual(["| 전공 | 학점 |", "|---|---|"], header)
        self.assertEqual(["| 컴공 | 130 |", "| 컴공 | 132 |", "| 비고 a\\|b |  |"], body)

    def test_plain_rows_and_split_repeats_header(self) -> None:
        rows = [["h1", "h2"]] + [[f"k{i}", "v" * 40] for i in range(10)]
        caption, header, body = table_to_markdown_rows({"title": "T", "rows": rows})
        chunks = split_table(caption, header, body, max_chars=150)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.startswith("T\n| h1 | h2 |\n|---|---|"))
        self.assertEqual(10, sum(chunk.count("| k") for chunk in chunks))


class DiagramAndProseTests(unittest.TestCase):
    def test_diagram_becomes_sentences(self) -> None:
        sentences = diagram_to_sentences({
            "diagram_title": "휴학 절차",
            "nodes": [{"id": "a", "text": "신청"}, {"id": "b", "text": "승인"}],
            "edges": [{"from": "a", "to": "b"}],
            "unverified_edges": [{"from": "b", "to": "a"}],
        })
        self.assertEqual("이 도식은 '휴학 절차'에 관한 내용이다.", sentences[0])
        self.assertIn("'신청' 다음 단계는 '승인'이다.", sentences)
        self.assertIn("'승인' 다음 단계는 '신청'이다(관계는 검토 필요).", sentences)

    def test_split_prose_respects_limit(self) -> None:
        text = " ".join(f"문장 {i}입니다." for i in range(200))
        chunks = split_prose(text, max_chars=100, overlap=20)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertIn("문장 199입니다.", chunks[-1])


class ImageChunkTests(unittest.TestCase):
    def test_non_text_layouts_are_skipped(self) -> None:
        for layout in ("no_text", "unknown"):
            chunks, reason = build_image_chunks(review(layout, "텍스트"), None)
            self.assertEqual([], chunks)
            self.assertEqual(f"LAYOUT_NOT_INGESTED:{layout}", reason)

    def test_missing_text_is_skipped(self) -> None:
        self.assertEqual(([], "NO_TEXT_AVAILABLE"), build_image_chunks(review("prose"), None))

    def test_reviewer_text_wins_over_extraction(self) -> None:
        extraction = {"extractor": "table_ocr", "result": {"tables": [{"rows": [["OCR", "값"]]}]}}
        chunks, _ = build_image_chunks(review("table", '{"tables": [{"rows": [["검토", "값"]]}]}'), extraction)
        self.assertIn("| 검토 | 값 |", chunks[0]["content"])
        self.assertEqual("reviewer_expected_text_or_structure", chunks[0]["provenance"]["content_origin"])

    def test_extraction_used_with_provenance(self) -> None:
        extraction = {"extractor": "diagram_ocr", "extractor_version": "1",
                      "result": {"diagram": {"nodes": [{"id": 1, "text": "A"}, {"id": 2, "text": "B"}],
                                             "edges": [{"from": 1, "to": 2}]}}}
        chunks, reason = build_image_chunks(review("diagram"), extraction)
        self.assertIsNone(reason)
        self.assertEqual("diagram", chunks[0]["layout_type"])
        self.assertEqual("diagram_ocr_result", chunks[0]["provenance"]["content_origin"])
        self.assertEqual("ab" * 8 + ":0", chunks[0]["chunk_id"])

    def test_fixture_collection_and_limit(self) -> None:
        tables = load_extraction_index(FIXTURES / "table_extractions.jsonl")
        diagrams = load_extraction_index(FIXTURES / "diagram_extractions.jsonl")
        chunks, report = collect_chunks([FIXTURES / "sample.reviewed.jsonl"], tables, diagrams, limit=50)
        self.assertEqual(9, len(report))
        self.assertEqual({"table", "diagram", "prose"}, {c["layout_type"] for c in chunks})
        self.assertTrue(all(c["provenance"]["synthetic"] for c in chunks))
        _, limited = collect_chunks([FIXTURES / "sample.reviewed.jsonl"], tables, diagrams, limit=3)
        self.assertEqual(3, len(limited))


class StoreTests(unittest.TestCase):
    def test_store_name_must_be_test_prefixed(self) -> None:
        for bad in ("rag_chunks", "test_layout_rag_", "test_layout_rag_2026", "../test_layout_rag_20260101"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                store_path(bad)

    def test_create_search_and_replace_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(layout_rag, "TEST_DB_ROOT", Path(tmp)):
            records = [{"chunk_id": str(i), "content": f"c{i}", "layout_type": "prose"} for i in range(3)]
            vectors = np.eye(3, dtype="float32")
            LocalVectorStore.create("test_layout_rag_20260101", records, vectors, manifest_extra={})
            with self.assertRaises(FileExistsError):
                LocalVectorStore.create("test_layout_rag_20260101", records, vectors, manifest_extra={})
            store = LocalVectorStore.open("test_layout_rag_20260101")
            hits = store.search([0, 1, 0], top_k=2)
            self.assertEqual(2, len(hits))
            self.assertEqual(("1", 1, 1.0), (hits[0]["chunk_id"], hits[0]["rank"], hits[0]["similarity"]))
            self.assertEqual(0.0, hits[1]["similarity"])

            foreign = Path(tmp) / "test_layout_rag_20260102"
            foreign.mkdir()
            (foreign / "manifest.json").write_text(json.dumps({"marker": "other"}), encoding="utf-8")
            with self.assertRaises(PermissionError):
                LocalVectorStore.create("test_layout_rag_20260102", records, vectors,
                                        manifest_extra={}, replace=True)


class _HTTPError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _FakeCompletions:
    def __init__(self, failures: int, error: Exception | None = None):
        self.failures = failures
        self.error = error or ConnectionError("boom")
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        message = type("M", (), {"content": '{"question": "q", "answer": "a"}'})
        return type("R", (), {"choices": [type("C", (), {"message": message})], "usage": None})


def fake_llm(failures: int, max_calls: int, error: Exception | None = None) -> tuple[BudgetedLLM, _FakeCompletions]:
    completions = _FakeCompletions(failures, error)
    client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})})
    return BudgetedLLM(model="fake", max_calls=max_calls, client=client), completions


@patch.object(llm_budget.time, "sleep", lambda _s: None)
class BudgetedLLMTests(unittest.TestCase):
    def test_retries_then_succeeds(self) -> None:
        llm, completions = fake_llm(failures=2, max_calls=10)
        self.assertEqual({"question": "q", "answer": "a"}, llm.chat_json([]))
        self.assertEqual(3, completions.calls)

    def test_gives_up_after_three_attempts(self) -> None:
        llm, completions = fake_llm(failures=99, max_calls=10)
        with self.assertRaises(RuntimeError):
            llm.chat([])
        self.assertEqual(3, completions.calls)

    def test_auth_error_is_not_retried(self) -> None:
        llm, completions = fake_llm(failures=99, max_calls=10, error=_HTTPError(401))
        with self.assertRaises(FatalLLMError):
            llm.chat([])
        self.assertEqual(1, completions.calls)

    def test_rate_limit_is_retried(self) -> None:
        llm, completions = fake_llm(failures=1, max_calls=10, error=_HTTPError(429))
        llm.chat([])
        self.assertEqual(2, completions.calls)

    def test_retry_cap_cannot_be_raised(self) -> None:
        llm, _ = fake_llm(failures=0, max_calls=10)
        self.assertEqual(3, BudgetedLLM(model="x", max_calls=1, max_retries=10, client=llm.client).max_retries)

    def test_budget_is_hard_limit(self) -> None:
        llm, completions = fake_llm(failures=0, max_calls=2)
        llm.chat([])
        llm.chat([])
        with self.assertRaises(BudgetExceeded):
            llm.chat([])
        self.assertEqual(2, completions.calls)


class SelectChunksTests(unittest.TestCase):
    def test_round_robin_one_per_image_then_limit(self) -> None:
        def rec(layout: str, image: str, i: int) -> dict:
            return {"chunk_id": f"{image}:{i}", "layout_type": layout, "content": "x" * 40,
                    "provenance": {"image_sha256": image}}
        records = [rec("table", "t1", 0), rec("table", "t1", 1), rec("table", "t2", 0),
                   rec("diagram", "d1", 0), rec("prose", "p1", 0), rec("prose", "short", 0)]
        records[-1]["content"] = "짧음"
        selected = [r["chunk_id"] for r in select_chunks(records, 50)]
        self.assertEqual(["d1:0", "p1:0", "t1:0", "t2:0", "t1:1"], selected)
        self.assertEqual(2, len(select_chunks(records, 2)))


class RuleBasedQATests(unittest.TestCase):
    def test_table_question_uses_row_key_and_header(self) -> None:
        record = {"chunk_id": "x:0", "layout_type": "table", "content":
                  "[합성 데이터] 졸업 학점\n| 전공 | 총학점 |\n|---|---|\n| 컴공 | 130 |"}
        self.assertEqual({"question": "졸업 학점에서 컴공의 총학점은(는) 무엇인가요?", "answer": "130"},
                         rule_based_qa(record))

    def test_diagram_question_uses_step(self) -> None:
        record = {"chunk_id": "x:0", "layout_type": "diagram", "content":
                  "이 도식은 '휴학 절차'에 관한 내용이다. '신청' 다음 단계는 '승인'이다."}
        self.assertEqual({"question": "휴학 절차에서 '신청' 다음 단계는 무엇인가요?", "answer": "승인"},
                         rule_based_qa(record))

    def test_branching_step_is_not_asked(self) -> None:
        record = {"chunk_id": "x:0", "layout_type": "diagram", "content":
                  "'A' 다음 단계는 'B'이다. 'B' 다음 단계는 'C'이다. 'B' 다음 단계는 'D'이다."}
        self.assertEqual("B", rule_based_qa(record)["answer"])

    def test_prose_and_unverified_edges_are_not_used(self) -> None:
        self.assertIsNone(rule_based_qa({"chunk_id": "x", "layout_type": "prose", "content": "본문"}))
        self.assertIsNone(rule_based_qa({"chunk_id": "x", "layout_type": "diagram", "content":
                                         "'A' 다음 단계는 'B'이다(관계는 검토 필요)."}))

    def test_answer_in_context_ignores_spacing(self) -> None:
        hits = [{"content": "| 평일 | 09:00~18:00 |"}]
        self.assertTrue(answer_in_context("09:00 ~ 18:00", hits))
        self.assertFalse(answer_in_context("17:00", hits))
        self.assertFalse(answer_in_context("", hits))


if __name__ == "__main__":
    unittest.main()
