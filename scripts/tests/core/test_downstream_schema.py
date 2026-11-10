from __future__ import annotations

from scripts.tests._paths import PROJECT_ROOT

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.vectorization import vectorization as vectors

from scripts.crawlers.common.reader import read_document, remove_redundant_legacy_fields
from scripts.crawlers.common.schema import apply_common_schema, attachment_text, content_sha256
from scripts.migrations.crawler_documents import convert_file
from scripts.preprocessing.file_preprocessing import extract_crawled_json_provenance
from scripts.vectorization.vectorization import extract_chunk_records
from scripts.vectorization.load_to_supabase import normalize_vector_metadata
from scripts.preprocessing.rule import extract_rule_json_blocks
from scripts.preprocessing import rule as rule_preprocessing
from scripts.preprocessing.rule_provenance import snapshot_identity, duplicate_version, attachment_identity, version_state, restore_from_snapshot


WORKSPACE_ROOT = PROJECT_ROOT


def legacy_notice() -> dict:
    return {
        "slug": "legacy-slug", "title": "Legacy notice", "date": "2026-09-02",
        "url": "https://www.pknu.ac.kr/main/163?action=view&no=1",
        "category": "notice", "subcategory": "general", "type": "notice",
        "content": "same document content", "crawled_at": "2026-09-02T10:00:00",
        "attachments": [], "notice_no": "12",
    }


class DownstreamSchemaTests(unittest.TestCase):
    def test_rule_provenance_index_keeps_collected_aliases_and_double_spaces_in_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            json_root = root / "files/rule/output/json"
            source = json_root / "pknu_rule_law/collected-file.json"
            source.parent.mkdir(parents=True)
            saved = "files/rule/output/files/pknu_rule_law/item/rule  original.pdf"
            attachment = root / saved
            attachment.parent.mkdir(parents=True)
            attachment.write_bytes(b"fixture")
            payload = {"slug": "raw-alias", "title": "collected label", "type": "hak", "source_site": "https://www.pknu.ac.kr",
                       "category": "pknu_rule_law", "content": "rule body", "source_id": "search_no_1",
                       "url": "https://www.pknu.ac.kr/rule/searchSchRegAndRuleView.do?no=1",
                       "crawled_at": "2026-11-08T00:00:00+09:00",
                       "attachments": [{"name": "rule  original.pdf", "saved_path": saved,
                           "url": "https://www.pknu.ac.kr/rule/ruleBoardDownload.do?no=1", "downloaded": True,
                           "size_bytes": 7, "sha256": "a" * 64}]}
            source.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(rule_preprocessing, "PROJECT_ROOT", root):
                indexed, attachments = rule_preprocessing.load_rule_json_index(json_root)
            self.assertIn("pknu_rule_law/collected-file", indexed)
            self.assertIn("pknu_rule_law/raw-alias", indexed)
            self.assertEqual(indexed["pknu_rule_law/collected-file"]["doc_url"], payload["url"])
            self.assertIn(attachment.resolve().as_posix().lower(), attachments)

    def test_rule_snapshot_dates_are_explicit_and_version_deduplication_is_scoped(self):
        html = ('<input id="schlPubRulId" value="123"><input id="schlPubRulSeq" value="456">'
                '<input id="schlPubRulNm" value="부경대학교 학칙"><input id="efYd" value="20230101">'
                '<div id="conTop"><h2>부경대학교 학칙</h2><div class="subtit1">[시행 2023.1.1.]</div></div>'
                '<div id="conScroll">제1조 목적과 적용 대상</div>')
        old = snapshot_identity(html)
        self.assertEqual(old["effective_at"], "2023-01-01T00:00:00+09:00")
        self.assertTrue(duplicate_version(old, snapshot_identity(html)))
        newer = snapshot_identity(html.replace('value="456"', 'value="789"').replace("20230101", "20260227").replace("2023.1.1.", "2026.2.27."))
        self.assertFalse(duplicate_version(old, newer))
        self.assertEqual(version_state(old, [newer])["rule_version_state"], "historical")
        self.assertIsNone(version_state(old, [newer])["rule_valid_until"])
        self.assertFalse(duplicate_version(old, {**old, "rule_document_id": "other"}))
        with self.assertRaisesRegex(ValueError, "effective date"):
            snapshot_identity(html.replace("[시행 2023.1.1.]", "[시행 2024.1.1.]"))
        unconfirmed = snapshot_identity(html.replace('<input id="efYd" value="20230101">', '').replace("[시행 2023.1.1.]", "개정 기록"))
        self.assertIsNone(unconfirmed["effective_at"])
        self.assertFalse(duplicate_version(unconfirmed, unconfirmed))
        restored = restore_from_snapshot({"doc_title": "old label", "doc_url": ""}, html, "stored.html")
        self.assertEqual(restored["doc_title"], "부경대학교 학칙")
        self.assertEqual(restored["doc_url"], "")

    def test_rule_attachment_does_not_infer_effective_date_from_filename_or_revision(self):
        document = {"source_file": "운영지침(2027.07.01  전문).pdf", "blocks": [
            {"text": "개정 2026. 07. 01."}, {"text": "부 칙(2026. 07. 01.)"},
            {"text": "제1조(시행일) 이 지침은 공포한 날부터 시행한다."}]}
        provenance = attachment_identity({"doc_title": "운영지침(일부개정 2026.7.1.)",
            "source_id": "bylaw:2:1", "published_at": "2025-03-04T00:00:00+09:00"}, document)
        self.assertIsNone(provenance["effective_at"])
        self.assertEqual(provenance["rule_effective_status"], "unconfirmed")
        self.assertTrue(provenance["rule_filename_date_conflict"])
        self.assertEqual(provenance["rule_revision_dates"], ["2026-07-01"])
        self.assertEqual(provenance["published_at"], "2025-03-04T00:00:00+09:00")

    def test_rule_effective_conditions_and_version_evidence_survive_vectorization(self):
        provenance = {"rule_provenance_policy": "rule-provenance-v1", "rule_document_id": "123", "rule_version_id": "456",
                      "rule_effective_status": "unconfirmed", "effective_at": None,
                      "applicability_note": "공포일 확인 필요. 파일명 날짜를 시행일로 사용하지 마세요.",
                      "doc_title": "운영 지침", "doc_url": "https://www.pknu.ac.kr/rule/view?no=1", "source_dataset": "pknu_rule"}
        doc = {"slug": "rule-attachment", "source_kind": "file", "source_file": "rule.pdf", "source_path": "files/rule/rule.pdf",
               "provenance": provenance, "chunks": [{"chunk_id": 1, "text": "수강신청 절차와 졸업 학점 기준 및 학생 신청 자격에 관한 규정입니다. " * 12}]}
        records = extract_chunk_records(doc, WORKSPACE_ROOT / "preprocessed/rule/file/rule.json", WORKSPACE_ROOT)
        self.assertTrue(records)
        metadata = records[0]["metadata"]
        self.assertEqual(metadata["rule_document_id"], "123")
        self.assertEqual(metadata["rule_effective_status"], "unconfirmed")
        self.assertEqual(metadata["applicability_note"], provenance["applicability_note"])
        self.assertIsNone(metadata.get("effective_at"))

    def test_archive_member_provenance_survives_vector_and_database_preparation(self):
        doc = {"slug": "stable-member", "source_file": "guide.pdf", "source_ext": ".pdf",
               "source_path": "files/ce/output/files/archive.zip!/nested/guide.pdf",
               "chunks": [{"chunk_id": 1, "text": "수강신청 기간과 등록금 납부 절차 및 졸업 학점 안내입니다. " * 12}],
               "provenance": {"source_dataset": "ce", "source_kind": "archive_member",
                              "archive_path": "files/ce/output/files/archive.zip",
                              "archive_member_path": "nested/guide.pdf", "archive_member_ext": ".pdf",
                              "archive_member_sha256": "member-hash", "archive_path_policy_version": 2,
                              "format_policy_version": "attachment-signature-v1", "actual_source_ext": ".pdf",
                              "original_sha256": "original-hash", "processed_sha256": "processed-hash",
                              "previous_preprocessed_path": "preprocessed/ce/guide.json"}}
        records = extract_chunk_records(doc, WORKSPACE_ROOT / "preprocessed/ce/guide.pdf.json", WORKSPACE_ROOT)
        self.assertTrue(records)
        metadata = normalize_vector_metadata(records[0]["metadata"])
        self.assertEqual(metadata["archive_member_path"], "nested/guide.pdf")
        self.assertEqual(metadata["archive_member_ext"], ".pdf")
        self.assertEqual(metadata["archive_member_sha256"], "member-hash")
        self.assertEqual(metadata["archive_path_policy_version"], 2)
        self.assertEqual(metadata["actual_source_ext"], ".pdf")
        self.assertEqual(metadata["original_sha256"], "original-hash")
        self.assertEqual(metadata["processed_sha256"], "processed-hash")

    def test_legacy_department_types_map_without_losing_raw_type(self) -> None:
        cases = (("curriculum", "static_page"), ("resource", "notice"))
        for raw_type, expected_type in cases:
            with self.subTest(raw_type=raw_type):
                legacy = {
                    **legacy_notice(),
                    "type": raw_type,
                    "source_dataset": "ce",
                    "source_id": raw_type,
                }
                converted = read_document(
                    legacy, dataset="ce", project_root=WORKSPACE_ROOT,
                    remove_redundant=True,
                )
                self.assertEqual(expected_type, converted["type"])
                self.assertEqual(raw_type, converted["metadata"]["source_type"])
                self.assertEqual(legacy["content"], converted["content"])
                self.assertEqual(legacy["title"], converted["title"])

    def test_legacy_and_schema_1_reader(self) -> None:
        legacy = read_document(
            legacy_notice(), dataset="pknu_notice", project_root=WORKSPACE_ROOT,
            remove_redundant=True,
        )
        current = read_document(legacy, project_root=WORKSPACE_ROOT)
        self.assertEqual("1.0", current["schema_version"])
        self.assertEqual("2026-09-02T00:00:00+09:00", current["published_at"])
        self.assertEqual(12, current["notice_no"])
        self.assertNotIn("date", current)
        self.assertNotIn("crawled_at", current)

    def test_rule_legacy_text_is_consolidated_without_changing_blocks(self) -> None:
        legacy = {
            "slug": "rule", "title": "Rule", "date": "2026-09-02",
            "url": "https://www.pknu.ac.kr/rule/view?no=1", "source_id": "1",
            "source_site": "pknu_rule", "category": "pknu_rule_law",
            "subcategory": "regulation", "type": "gyu",
            "content": "rule body\n\nattachment text", "html_text": "rule body",
            "page_content": "rule body", "crawled_at": "2026-09-02T10:00:00",
            "attachments": [{
                "name": "rule.pdf", "url": "https://example.test/rule.pdf",
                "saved_path": None, "downloaded": False,
            }],
            "attachment_texts": [{"name": "rule.pdf", "text": "attachment text"}],
            "file_preview_texts": [{"name": "rule.pdf", "text": "attachment text"}],
        }
        converted = read_document(
            legacy, dataset="rule", project_root=WORKSPACE_ROOT, remove_redundant=True
        )
        self.assertEqual("regulation", converted["type"])
        self.assertEqual("gyu", converted["metadata"]["source_type"])
        for field in ("date", "crawled_at", "html_text", "page_content", "attachment_texts", "file_preview_texts"):
            self.assertNotIn(field, converted)
        blocks, _ = extract_rule_json_blocks(converted, WORKSPACE_ROOT / "missing")
        texts = [block["text"] for block in blocks]
        self.assertTrue(any("rule body" in text for text in texts))
        self.assertTrue(any("attachment text" in text for text in texts))

    def test_preprocessing_and_vectorization_emit_canonical_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "files" / "ce" / "output" / "json" / "cat" / "doc.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps(legacy_notice()), encoding="utf-8")
            provenance = extract_crawled_json_provenance(source)
            self.assertEqual("ce", provenance["source_dataset"])
            # Explicit path dataset wins when a legacy fixture has no source_dataset.
            self.assertIn("published_at", provenance)
            self.assertNotIn("date", provenance)
            preprocessed = {
                "slug": "doc", "source_file": "doc.json", "source_path": source.as_posix(),
                "source_kind": "post", "provenance": provenance,
                "chunks": [{
                    "chunk_id": "1",
                    "text": "This canonical university notice explains registration schedules, course requirements, scholarship applications, student services, academic policies, deadlines, contacts, and required supporting documents in sufficient detail for retrieval testing.",
                }],
            }
            records = extract_chunk_records(preprocessed, source, WORKSPACE_ROOT)
            metadata = records[0]["metadata"]
            self.assertIn("published_at", metadata)
            self.assertIn("crawl", metadata)
            self.assertNotIn("date", metadata)
            self.assertNotIn("crawled_at", metadata)

    def test_migration_dry_run_preserves_file_content_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            original = legacy_notice()
            path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            before = path.read_bytes()
            report = convert_file(path, "pknu_notice", apply=False)
            self.assertEqual(before, path.read_bytes())
            self.assertTrue(report["content_preserved"])
            self.assertEqual(report["attachments_before"], report["attachments_after"])
            self.assertEqual(content_sha256(original["content"]), report["content_hash_after"])
            self.assertIsNone(report["backup"])

    def test_schema_file_matches_required_model(self) -> None:
        schema = json.loads((WORKSPACE_ROOT / "schemas" / "crawler-document-1.0.schema.json").read_text(encoding="utf-8"))
        required = set(schema["required"])
        fixture = apply_common_schema(
            {"title": "x", "url": "https://example.test/x", "category": "x", "content": "x"},
            source_dataset="ce", source_id="1", source_site="https://example.test",
            document_type="notice", content_source="fixture",
        )
        self.assertTrue(required.issubset(fixture))
        self.assertEqual("1.0", schema["properties"]["schema_version"]["const"])

    def test_loader_normalizes_transition_metadata(self) -> None:
        metadata = normalize_vector_metadata({
            "date": "2026-09-02", "crawled_at": "2026-09-02T10:00:00+09:00",
            "num_chars": "123", "attachments": [{"size_bytes": "45"}],
        })
        self.assertEqual("2026-09-02", metadata["published_at"])
        self.assertEqual(123, metadata["num_chars"])
        self.assertEqual(45, metadata["attachments"][0]["size_bytes"])
        self.assertNotIn("date", metadata)
        self.assertNotIn("crawled_at", metadata)

    def test_information_is_not_removed_when_not_represented(self) -> None:
        doc = {
            "published_at": None, "crawl": {}, "content": "main body",
            "html_text": "unique hidden text", "attachment_texts": [{"text": "unique attachment"}],
            "attachments": [],
        }
        removed = remove_redundant_legacy_fields(doc)
        self.assertNotIn("html_text", removed)
        self.assertNotIn("attachment_texts", removed)
        self.assertIn("html_text", doc)
        self.assertIn("attachment_texts", doc)


class VectorizationRegressionTests(unittest.TestCase):
    def test_reviewed_exclusion_keeps_unrelated_chunk_positions_and_original_text(self):
        document = {"slug": "reviewed", "source_path": "files/ce/guide.pdf", "source_file": "guide.pdf",
                    "provenance": {"source_dataset": "ce"}, "chunks": [
                        {"text": "원문 \ufffd 보존", "search_excluded": True},
                        {"text": "수강신청 기간과 등록금 납부 및 졸업 학점 안내입니다. " * 8}]}
        records = extract_chunk_records(document, WORKSPACE_ROOT / "preprocessed/ce/guide.pdf.json", WORKSPACE_ROOT)
        self.assertEqual([r["chunk_index"] for r in records], [1])
        self.assertEqual(document["chunks"][0]["text"], "원문 \ufffd 보존")

    def test_stale_vector_cannot_reinsert_a_reviewed_excluded_chunk(self):
        from scripts.vectorization.load_to_supabase import prepare_row, DEFAULT_EMBEDDING_MODEL, NoSearchContent
        record = self.ingestion_record()
        record["metadata"]["source_path"] = "files/ce/guide.pdf"
        document = {"slug": record["source_slug"], "source_path": "files/ce/guide.pdf", "chunks": [
            {"text": "깨진 원문 \ufffd", "search_excluded": True}, {"text": "수강신청 절차 안내"}]}
        with self.assertRaises(NoSearchContent):
            prepare_row(record, DEFAULT_EMBEDDING_MODEL, "ce", body_document=document)
        record["chunk_index"] = 1
        self.assertEqual(prepare_row(record, DEFAULT_EMBEDDING_MODEL, "ce", body_document=document)["chunk"]["chunk_index"], 1)

    def test_staged_body_validation_does_not_require_unpublished_canonical_path(self):
        from scripts.vectorization.load_to_supabase import prepare_row, DEFAULT_EMBEDDING_MODEL
        record = self.ingestion_record()
        record["metadata"].update(source_path="archive.zip!/a/file.pdf",
                                  preprocessed_path="preprocessed/ce/not-published.pdf.json")
        document = {"slug": record["source_slug"], "source_path": record["metadata"]["source_path"],
                    "chunks": [{"text": "수강신청 절차와 신청 기간 안내입니다."}], "used_metadata_fallback": False}
        prepared = prepare_row(record, DEFAULT_EMBEDDING_MODEL, "ce", body_document=document)
        self.assertEqual(prepared["source"]["file_path"], document["source_path"])
        with self.assertRaisesRegex(ValueError, "does not match record identity"):
            prepare_row(record, DEFAULT_EMBEDDING_MODEL, "ce", body_document={**document, "slug": "another-member"})
        with self.assertRaisesRegex(ValueError, "Empty body"):
            prepare_row(record, DEFAULT_EMBEDDING_MODEL, "ce", body_document={**document, "used_metadata_fallback": True})

    def test_vector_workers_partition_datasets_without_overlap(self):
        selected = [(name, Path(name), Path("output") / name)
                    for name in ("ce", "pknu_main", "pknu_syllabus", "rule", "me")]
        assigned = [row[0] for index in range(4)
                    for row in vectors.partition_datasets(selected, 4, index)]
        self.assertCountEqual([row[0] for row in selected], assigned)
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_db_ingestion_rejects_hash_vectors_and_model_mismatch(self):
        from scripts.vectorization.load_to_supabase import prepare_row, DEFAULT_EMBEDDING_MODEL
        with self.assertRaisesRegex(ValueError, "hash vectors"):
            prepare_row({}, "hash")
        with self.assertRaisesRegex(ValueError, "differs"):
            prepare_row({"embedding_model": "sentence-transformers:other"}, DEFAULT_EMBEDDING_MODEL)

    def ingestion_record(self):
        return {"id": "original", "source_slug": "shared", "chunk_id": 0,
                "chunk_index": 0, "text": "University information", "embedding": [0.1] * 384,
                "metadata": {"source_dataset": "ce", "archive_member_path": "a/file.pdf"}}

    def test_ingestion_keys_are_stable_and_isolated_by_dataset(self):
        from scripts.vectorization import load_to_supabase as loader
        record = self.ingestion_record()
        first = loader.prepare_row(record, loader.DEFAULT_EMBEDDING_MODEL, "ce")
        second = loader.prepare_row(record, loader.DEFAULT_EMBEDDING_MODEL, "english")
        self.assertNotEqual(first["source"]["id"], second["source"]["id"])
        self.assertNotEqual(first["chunk"]["id"], second["chunk"]["id"])
        self.assertNotEqual(first["source"]["source_slug"], second["source"]["source_slug"])
        self.assertEqual(first["chunk"]["id"], loader.prepare_row(record, loader.DEFAULT_EMBEDDING_MODEL, "ce")["chunk"]["id"])
        self.assertEqual("shared", first["source"]["metadata"].obj["original_source_slug"])
        self.assertEqual("ce", second["source"]["metadata"].obj["source_dataset"])
        self.assertEqual("english", second["source"]["metadata"].obj["ingestion_dataset"])

    def test_table_cells_remain_chunk_metadata_not_source_metadata(self):
        from scripts.vectorization import load_to_supabase as loader
        record = self.ingestion_record()
        table = {"table_id": "p1-t1", "rows": [{"table_cells": ["35%", "40%"]}]}
        record["metadata"]["structured_table"] = table
        row = loader.prepare_row(record, loader.DEFAULT_EMBEDDING_MODEL, "ce")
        self.assertNotIn("structured_table", row["source"]["metadata"].obj)
        self.assertEqual(table, row["chunk"]["metadata"].obj["structured_table"])

    def test_manifest_preflight_blocks_failures_before_database_connection(self):
        from scripts.vectorization import load_to_supabase as loader
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.jsonl"
            index.write_text(json.dumps(self.ingestion_record()) + "\n", encoding="utf-8")
            manifest = {"dataset": "ce", "vectorized_at": "2026-11-03T00:00:00",
                        "embedding_backend": loader.DEFAULT_EMBEDDING_MODEL,
                        "embedding_dimensions": 384, "index_path": str(index),
                        "num_files_failed": 0, "failures": [], "num_chunks": 1}
            path = index.with_name("manifest.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(loader.DEFAULT_EMBEDDING_MODEL, loader.preflight_index(index, "ce"))
            for changes in ({"num_files_failed": 1}, {"failures": ["bad"]},
                            {"dataset": "rule"}, {"num_chunks": 2},
                            {"embedding_dimensions": 768}, {"embedding_backend": "hash"}):
                path.write_text(json.dumps({**manifest, **changes}), encoding="utf-8")
                with self.subTest(changes=changes), patch.object(loader, "connect") as connect:
                    with self.assertRaises(ValueError):
                        loader.load(index, 200, "ce")
                    connect.assert_not_called()
            path.unlink()
            with self.assertRaisesRegex(ValueError, "manifest"):
                loader.preflight_index(index, "ce")

    def test_relocated_index_resolves_original_manifest_without_rewriting_it(self):
        from scripts.vectorization import load_to_supabase as loader
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / "vectorized/ce/index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text(json.dumps(self.ingestion_record()) + "\n", encoding="utf-8")
            manifest = {"dataset": "ce", "vectorized_at": "2026-11-03T00:00:00",
                        "embedding_backend": loader.DEFAULT_EMBEDDING_MODEL,
                        "embedding_dimensions": 384, "index_path": "files/vectorized/ce/index.jsonl",
                        "num_files_failed": 0, "failures": [], "num_chunks": 1}
            path = index.with_name("manifest.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            frozen = path.read_bytes()
            with patch.object(loader, "PROJECT_ROOT", root):
                self.assertEqual(loader.resolve_index_path("ce", Path(manifest["index_path"])), index)
                self.assertEqual(loader.DEFAULT_EMBEDDING_MODEL, loader.preflight_index(index, "ce"))
            self.assertEqual(path.read_bytes(), frozen)

    def test_deduplication_is_dataset_and_archive_scoped(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 0
        loader.deduplicate_sources(conn, "rag_sources", "rag_chunks", "ce")
        query, params = cursor.execute.call_args.args
        self.assertEqual(("ce",), params)
        self.assertIn("ingestion_dataset' = %s", query.as_string())
        self.assertIn("archive_member_path", query.as_string())
        self.assertIn("source_path", query.as_string())

    def test_priority_dispatch_covers_all_generic_rag_datasets(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        with patch.object(loader, "connect_postgres", return_value=MagicMock()), \
             patch("scripts.priority.department.update_priorities.update_department_priorities", return_value=1) as update:
            for dataset in ("ce", "english"):
                loader.refresh_priority_scores(dataset)
            self.assertEqual(2, update.call_count)
        with patch.object(loader, "connect_postgres", return_value=MagicMock()), \
             patch("scripts.priority.rag.update_main_priorities.fetch_rule_contents", return_value=[]), \
             patch("scripts.priority.rag.update_main_priorities.update_dataset_priorities", return_value=1) as update:
            for dataset in ("pknu_notice", "pknu_main", "pknu_syllabus", "pknu_student_life"):
                loader.refresh_priority_scores(dataset)
            self.assertEqual(["pknu_notice", "pknu_main", "pknu_syllabus", "pknu_main"],
                             [call.args[1] for call in update.call_args_list])
        with patch.object(loader, "connect_postgres", return_value=MagicMock()), \
             patch("scripts.priority.rule.update_priorities.update_rule_priorities", return_value=1) as update:
            loader.refresh_priority_scores("rule")
            update.assert_called_once()

    def test_department_priority_refresh_is_scoped_to_the_loaded_department(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        with patch.object(loader, "connect_postgres", return_value=MagicMock()), \
             patch("scripts.priority.department.update_priorities.update_department_priorities",
                   return_value=1) as update:
            loader.refresh_priority_scores("ce")
            loader.refresh_priority_scores("rag")
        self.assertEqual([["ce"], None], [call.args[3] for call in update.call_args_list])

    def test_department_priorities_are_fetched_one_dataset_at_a_time(self):
        # 2026-11-03: one query over every department timed out the daily load.
        from unittest.mock import MagicMock
        from scripts.priority.department import update_priorities as department
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{"id": "src_1", "title": "공지", "metadata": {}, "content": "본문"}]
        with patch.object(department, "fetch_rule_contents", return_value=[]), \
             patch.object(department, "fetch_main_contents", return_value=[]):
            self.assertEqual(2, department.update_department_priorities(conn, False, 10, ["me", "ce"]))
        fetches = [call for call in cursor.execute.call_args_list if "string_agg" in call.args[0]]
        self.assertEqual([("ce",), ("me",)], [call.args[1] for call in fetches])
        self.assertTrue(all("source_dataset' = %s" in call.args[0] for call in fetches))
        self.assertEqual(2, conn.commit.call_count)

    def test_recent_load_keeps_going_after_a_priority_failure_then_fails(self):
        from types import SimpleNamespace
        from scripts.vectorization import preprocessing_pipeline_recent as recent
        from scripts.vectorization.load_to_supabase import PriorityRefreshError
        loaded = []

        def fake_load(*, index_path, batch_size, dataset, replace_sources):
            loaded.append(dataset)
            if dataset == "ce":
                raise PriorityRefreshError("ce priority refresh failed")
            return 1

        paths = {name: {"output_root": Path("x")} for name in ("ce", "pknu_notice", "rule")}
        with patch.object(recent, "load_to_supabase", side_effect=fake_load), \
             patch.dict(recent.DATASET_PATHS, paths):
            with self.assertRaisesRegex(RuntimeError, "ce"):
                recent.load_recent(["ce", "pknu_notice", "rule"], SimpleNamespace(load_batch_size=200))
        self.assertEqual(["ce", "pknu_notice", "rule"], loaded)

    def test_all_ingestion_preflights_every_dataset_and_refreshes_each_family_once(self):
        from scripts.vectorization import load_to_supabase as loader
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            paths = {name: {"input_root": source} for name in ("ce", "english", "rule")}
            with patch.object(loader, "dataset_paths", return_value=paths), \
                 patch.object(loader, "preflight_index") as preflight, \
                 patch.object(loader, "load", return_value=2) as load, \
                 patch.object(loader, "refresh_priority_scores") as refresh:
                self.assertEqual(6, loader.load_all())
                self.assertEqual(3, preflight.call_count)
                self.assertEqual("rule", load.call_args_list[0].args[2])
                self.assertTrue(all(not call.kwargs["refresh_priorities"] for call in load.call_args_list))
                self.assertEqual(2, refresh.call_count)
            with patch.object(loader, "dataset_paths", return_value=paths), \
                 patch.object(loader, "preflight_index", side_effect=ValueError("bad manifest")), \
                 patch.object(loader, "load") as load:
                with self.assertRaises(ValueError):
                    loader.load_all()
                load.assert_not_called()

    def test_notice_attachments_are_routed_by_origin_not_is_notice_flag(self):
        from scripts.vectorization.datasets import dataset_families, table_family
        self.assertEqual("pknu_notice", table_family("pknu_main", {
            "source_dataset": "pknu_notice", "is_notice": False, "source_kind": "attachment"}))
        self.assertEqual("pknu_main", table_family("pknu_main", {"source_dataset": "pknu_student_life"}))
        self.assertEqual("pknu_main", table_family("pknu_student_life"))
        self.assertEqual("pknu_syllabus", table_family("pknu_syllabus"))
        self.assertEqual({"pknu_notice", "pknu_main"}, set(dataset_families("pknu_main")))
        with self.assertRaises(ValueError):
            table_family("pknu_main", {"source_dataset": "ce"})

    def test_department_attributes_are_canonical_and_filterable(self):
        from scripts.vectorization import load_to_supabase as loader
        from scripts.vectorization.datasets import department_attributes
        record = self.ingestion_record()
        record["metadata"]["department_id"] = "wrong"
        row = loader.prepare_row(record, loader.DEFAULT_EMBEDDING_MODEL, "ce")
        expected = department_attributes("ce")
        self.assertEqual(expected["department_id"], row["source"]["department_id"])
        self.assertEqual(expected["department_name"], row["source"]["department_name"])
        self.assertEqual("ce", row["chunk"]["metadata"].obj["department_id"])

    def test_mixed_main_batch_commits_each_family_and_preserves_original_keys(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.jsonl"
            records = [self.ingestion_record(), {**self.ingestion_record(), "chunk_index": 1}]
            records[0]["metadata"] = {"source_dataset": "pknu_notice", "is_notice": False}
            records[1]["metadata"] = {"source_dataset": "pknu_student_life"}
            index.write_text("\n".join(json.dumps(r) for r in records)+"\n",encoding="utf-8")
            with patch.object(loader, "preflight_index", return_value=loader.DEFAULT_EMBEDDING_MODEL), \
                 patch.object(loader, "connect", return_value=MagicMock()), \
                 patch.object(loader, "ensure_schema_ready"), \
                 patch.object(loader, "flush_batch") as flush, \
                 patch.object(loader, "delete_excluded_chunks"), \
                 patch.object(loader, "deduplicate_sources"), \
                 patch.object(loader, "refresh_priority_scores") as refresh:
                self.assertEqual(2, loader.load(index, 200, "pknu_main"))
                destinations={call.args[2]: call.args[1][0] for call in flush.call_args_list if call.args[1]}
                self.assertEqual({"pknu_main_sources", "pknu_notice_sources"}, set(destinations))
                for record in records:
                    target="pknu_notice_sources" if record["metadata"]["source_dataset"]=="pknu_notice" else "pknu_main_sources"
                    self.assertEqual(loader.record_identity(record,"pknu_main")[0], destinations[target]["source"]["id"])
                self.assertEqual({"pknu_main", "pknu_notice"}, {call.args[0] for call in refresh.call_args_list})

    def test_metadata_compaction_keeps_real_values_and_schema_contract(self):
        from scripts.preprocessing.text_cleaning import compact_metadata
        metadata = {"empty": " \n", "missing": None, "empty_list": [], "empty_dict": {},
                    "is_form": False, "chunk_index": 0, "url": "https://example.org",
                    "published_at": None, "crawl": {},
                    "attachments": [{"name": "file.pdf", "size_bytes": 0, "url": ""}]}
        clean = compact_metadata(metadata)
        self.assertNotIn("empty", clean)
        self.assertNotIn("missing", clean)
        self.assertNotIn("empty_list", clean)
        self.assertNotIn("empty_dict", clean)
        self.assertIs(False, clean["is_form"])
        self.assertEqual(0, clean["chunk_index"])
        self.assertIn("published_at", clean)
        self.assertEqual({}, clean["crawl"])
        self.assertEqual([{"name": "file.pdf", "size_bytes": 0}], clean["attachments"])
        self.assertEqual("", metadata["attachments"][0]["url"])

    def test_ingestion_schema_check_stops_on_readonly_or_replica(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        for read_only, replica in ((True, False), (False, True)):
            conn = MagicMock()
            cur = conn.cursor.return_value.__enter__.return_value
            cur.fetchone.return_value = (True, True, read_only, replica)
            with self.assertRaises(loader.psycopg.errors.ReadOnlySqlTransaction):
                loader.ensure_schema_ready(conn, "rag_sources", "rag_chunks")
            self.assertEqual(1, cur.execute.call_count)

    def test_old_vectors_with_metadata_fallback_are_excluded_without_reembedding(self):
        from scripts.vectorization import load_to_supabase as loader
        from scripts.preprocessing.text_cleaning import has_searchable_body
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "preprocessed/ce/poster.json"
            path.parent.mkdir(parents=True)
            doc = {"used_metadata_fallback": True, "chunks": [{"text": "Document title and filename only"}]}
            path.write_text(json.dumps(doc), encoding="utf-8")
            record = self.ingestion_record()
            record["metadata"]["preprocessed_path"] = "preprocessed/ce/poster.json"
            with patch.object(loader, "PROJECT_ROOT", root):
                self.assertFalse(loader.has_record_body(record))
                with self.assertRaises(loader.NoSearchContent):
                    loader.prepare_row(record, loader.DEFAULT_EMBEDDING_MODEL, "ce")
            self.assertEqual([], vectors.extract_chunk_records(doc, path, root))
            doc["used_metadata_fallback"] = False
            doc["ocr_error"] = "one image failed; text still exists"
            self.assertTrue(has_searchable_body(doc))

    def test_preflight_accounts_for_excluded_records_without_manifest_mismatch(self):
        from scripts.vectorization import load_to_supabase as loader
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.jsonl"
            excluded = {**self.ingestion_record(), "text": " \n", "chunk_index": 1}
            index.write_text(json.dumps(self.ingestion_record()) + "\n" + json.dumps(excluded) + "\n", encoding="utf-8")
            manifest = {"dataset": "ce", "vectorized_at": "2026-11-03", "embedding_backend": loader.DEFAULT_EMBEDDING_MODEL,
                        "embedding_dimensions": 384, "index_path": str(index), "num_files_failed": 0,
                        "failures": [], "num_chunks": 2}
            index.with_name("manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(loader.DEFAULT_EMBEDDING_MODEL, loader.preflight_index(index, "ce"))

    def test_copy_staging_upsert_ignores_timestamp_only_updates(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        record = self.ingestion_record()
        record["metadata"].update(num_chars=20, quality_flags=[])
        row = loader.prepare_row(record, loader.DEFAULT_EMBEDDING_MODEL, "ce")
        self.assertNotIn("num_chars", row["source"]["metadata"].obj)
        loader.flush_batch(conn, [row], "rag_sources", "rag_chunks")
        cur.executemany.assert_not_called()
        self.assertEqual(2, cur.copy.call_count)
        copies = [c.args[0].as_string() for c in cur.copy.call_args_list]
        self.assertTrue(all('"pg_temp".' in query for query in copies))
        self.assertTrue(all('from stdin' in query for query in copies))
        queries = [c.args[0].as_string() for c in cur.execute.call_args_list]
        merges = [query for query in queries if 'on conflict' in query]
        self.assertEqual(2, len(merges))
        self.assertTrue(all('is distinct from' in query for query in merges))
        for query in merges:
            predicate = query.split('where (')[-1]
            self.assertNotIn('processed_at', predicate)
            self.assertNotIn('embedded_at', predicate)

    def test_index_maintenance_refuses_unique_indexes(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("rag_sources", True, True)
        with self.assertRaisesRegex(ValueError, "unique"):
            loader.manage_search_indexes(conn, "rag_sources", "rag_chunks", create=False)
        self.assertFalse(any('drop index' in str(c) for c in cur.execute.call_args_list))

    def test_main_index_maintenance_covers_both_routed_tables(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        with patch.object(loader, "configure_logging"), \
             patch.object(loader, "connect", return_value=MagicMock()), \
             patch.object(loader, "ensure_schema_ready"), \
             patch.object(loader, "manage_search_indexes") as indexes, \
             patch("sys.argv", ["loader", "--dataset", "pknu_main", "--finalize-indexes"]):
            loader.main()
        self.assertEqual({("pknu_main_sources", "pknu_main_chunks"),
                          ("pknu_notice_sources", "pknu_notice_chunks")},
                         {call.args[1:3] for call in indexes.call_args_list})

    def test_priority_preview_does_not_mix_notice_attachments_with_main(self):
        from scripts.priority.rag import update_main_priorities as priorities
        records = [
            {"source_slug": "notice", "text": "notice attachment",
             "metadata": {"source_dataset": "pknu_notice", "is_notice": False}},
            {"source_slug": "guide", "text": "student guide",
             "metadata": {"source_dataset": "pknu_student_life"}},
        ]
        for dataset, expected in [("pknu_notice", "notice"), ("pknu_main", "guide")]:
            with self.subTest(dataset=dataset), \
                 patch.object(priorities, "iter_index_records", side_effect=[iter(records), iter([])]), \
                 patch.object(priorities, "build_updates", return_value=[]) as updates, \
                 patch.object(priorities, "log_preview"):
                priorities.preview_from_index(dataset, Path("main/index.jsonl"), Path("rule/index.jsonl"), 10)
                self.assertEqual([expected], [doc["id"] for doc in updates.call_args.args[1]])

    def test_index_timeout_can_be_disabled_without_changing_global_settings(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        loader.manage_search_indexes(conn, "rag_sources", "rag_chunks", create=True, timeout_seconds=0)
        settings = [call for call in cur.execute.call_args_list
                    if isinstance(call.args[0], str) and 'set_config' in call.args[0]]
        self.assertTrue(settings)
        for call in settings:
            self.assertIn("true", call.args[0])
            self.assertEqual(("0",), call.args[1])
        self.assertFalse(any('alter system' in str(call).lower() or 'alter role' in str(call).lower()
                             for call in cur.execute.call_args_list))
        with self.assertRaises(ValueError):
            loader.manage_search_indexes(conn, "rag_sources", "rag_chunks", create=True, timeout_seconds=-1)

    def test_existing_vector_index_is_not_silently_reused_for_halfvec(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [None, None, None,
                                   ("rag_chunks", False, True, "hnsw", None, "vector_cosine_ops", None, None)]
        with self.assertRaisesRegex(ValueError, "does not match halfvec"):
            loader.manage_search_indexes(conn, "rag_sources", "rag_chunks", create=True)
        queries = [str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertFalse(any("drop index" in query for query in queries))
        self.assertFalse(any("using hnsw" in query for query in queries))

    def test_existing_halfvec_index_requires_original_parameters_and_full_coverage(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        for options, predicate in [(["m=8", "ef_construction=32"], None),
                                   (["m=16", "ef_construction=64"], "dataset filter")]:
            with self.subTest(options=options, predicate=predicate):
                conn = MagicMock()
                cur = conn.cursor.return_value.__enter__.return_value
                cur.fetchone.side_effect = [None, None, None, (
                    "rag_chunks", False, True, "hnsw", "(embedding)::extensions.halfvec(384)",
                    "halfvec_cosine_ops", options, predicate)]
                with self.assertRaisesRegex(ValueError, "does not match halfvec"):
                    loader.manage_search_indexes(conn, "rag_sources", "rag_chunks", create=True)

    def test_halfvec_index_retains_full_precision_ingestion_and_existing_hnsw_defaults(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        for existing in [None, ("rag_chunks", False, True, "hnsw", "embedding::halfvec(384)",
                                "halfvec_cosine_ops", None, None)]:
            with self.subTest(existing=existing):
                conn = MagicMock()
                cur = conn.cursor.return_value.__enter__.return_value
                cur.fetchone.side_effect = [None, None, None, existing]
                loader.manage_search_indexes(conn, "rag_sources", "rag_chunks", create=True)
                queries = [call.args[0].as_string() for call in cur.execute.call_args_list
                           if not isinstance(call.args[0], str)]
                hnsw = next(query for query in queries if "using hnsw" in query)
                self.assertIn("(embedding::extensions.halfvec(384)) extensions.halfvec_cosine_ops", hnsw)
                self.assertIn("with (m = 16, ef_construction = 64)", hnsw)
                self.assertFalse(any("alter table" in query for query in queries))
        record = self.ingestion_record()
        record["embedding"][0] = 0.123456789
        row = loader.prepare_row(record, loader.DEFAULT_EMBEDDING_MODEL, "ce")
        self.assertEqual(384, row["chunk"]["embedding_dim"])
        self.assertIn("0.123456789", row["chunk"]["embedding"])

    def test_excluded_cleanup_cannot_alias_a_valid_record(self):
        from scripts.vectorization import load_to_supabase as loader
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.jsonl"
            first = self.ingestion_record()
            index.write_text(json.dumps(first) + "\n" + json.dumps({**first, "text": " "}) + "\n", encoding="utf-8")
            index.with_name("manifest.json").write_text(json.dumps({
                "dataset": "ce", "vectorized_at": "2026-11-03", "embedding_backend": loader.DEFAULT_EMBEDDING_MODEL,
                "embedding_dimensions": 384, "index_path": str(index), "num_files_failed": 0,
                "failures": [], "num_chunks": 2}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                loader.preflight_index(index, "ce")

    def test_loader_cleans_only_excluded_stable_chunk_ids(self):
        from unittest.mock import MagicMock
        from scripts.vectorization import load_to_supabase as loader
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.jsonl"
            first = self.ingestion_record()
            excluded = {**first, "chunk_index": 1, "text": " "}
            index.write_text(json.dumps(first) + "\n" + json.dumps(excluded) + "\n", encoding="utf-8")
            index.with_name("manifest.json").write_text(json.dumps({
                "dataset": "ce", "vectorized_at": "2026-11-03", "embedding_backend": loader.DEFAULT_EMBEDDING_MODEL,
                "embedding_dimensions": 384, "index_path": str(index), "num_files_failed": 0,
                "failures": [], "num_chunks": 2}), encoding="utf-8")
            with patch.object(loader, "connect", return_value=MagicMock()), \
                 patch.object(loader, "ensure_schema_ready"), \
                 patch.object(loader, "flush_batch") as flush, \
                 patch.object(loader, "deduplicate_sources"), \
                 patch.object(loader, "refresh_priority_scores"), \
                 patch.object(loader, "delete_excluded_chunks") as cleanup:
                self.assertEqual(1, loader.load(index, 200, "ce"))
                self.assertEqual([loader.record_identity(excluded, "ce")[1]], cleanup.call_args.args[2])
                self.assertEqual(1, len(flush.call_args.args[1]))

    def make_input(self, root, name):
        path = root / "preprocessed/demo" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"slug": name, "source_file": name, "source_path": f"source/{name}.txt",
                   "chunks": [{"text": "This university document explains course registration, scholarship applications, graduation requirements, academic deadlines and supporting documents for undergraduate students."}]}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_vectors(self, root, **kwargs):
        return vectors.run_batch(root / "preprocessed/demo", root / "vectors", root,
                                 vectors.HashEmbedder(8), 2, False, "demo", **kwargs)

    def test_incremental_and_targeted_runs_preserve_complete_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = self.make_input(root, "one"), self.make_input(root, "two")
            self.run_vectors(root)
            summary = self.run_vectors(root, changed_only=True)
            self.assertEqual(0, summary["num_files_vectorized"])
            self.assertEqual(2, summary["num_chunks"])
            summary = self.run_vectors(root, target_files=[first])
            self.assertEqual(2, summary["num_chunks"])
            second.unlink()
            self.assertEqual(1, self.run_vectors(root, changed_only=True)["num_chunks"])
            first.unlink()
            self.assertEqual(0, self.run_vectors(root, changed_only=True)["num_chunks"])
            self.assertEqual("", (root / "vectors/index.jsonl").read_text(encoding="utf-8"))

    def test_changed_model_and_corrupt_results_are_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_input(root, "one")
            self.run_vectors(root)
            self.assertFalse(vectors.is_vectorized_current(source, source.parent, root / "vectors", vectors.HashEmbedder(16)))
            output = root / "vectors/one.json"
            output.write_text("{broken", encoding="utf-8")
            self.assertEqual(1, self.run_vectors(root, changed_only=True)["num_files_vectorized"])
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("demo", result["records"][0]["metadata"]["source_dataset"])

    def test_wrong_embedding_count_fails_without_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_input(root, "one")
            with patch.object(vectors.HashEmbedder, "embed_texts", return_value=[]):
                summary = self.run_vectors(root)
            self.assertEqual(1, summary["num_files_failed"])
            self.assertFalse((root / "vectors/one.json").exists())
            self.assertEqual(0, summary["num_chunks"])

    def test_input_modified_during_embedding_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_input(root, "one")
            encode = vectors.HashEmbedder.embed_texts
            def modify_input(embedder, texts):
                source.write_text(source.read_text(encoding="utf-8") + " ", encoding="utf-8")
                return encode(embedder, texts)
            with patch.object(vectors.HashEmbedder, "embed_texts", modify_input):
                result = self.run_vectors(root)
            self.assertEqual(1, result["num_files_failed"])
            self.assertFalse((root / "vectors/one.json").exists())

    def test_all_cli_discovers_datasets_without_loading_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_input(root, "one")
            (root / "preprocessed/another").mkdir()
            with patch.object(vectors, "PROJECT_ROOT", root), patch.object(vectors, "configure_logging"), patch.object(vectors, "make_embedder") as model, patch.object(vectors, "run_batch", return_value={}) as batch, patch("sys.argv", ["vectorization", "--dataset", "all", "--dry-run"]):
                vectors.main()
            model.assert_not_called()
            self.assertEqual({"demo", "another"}, {c.kwargs["dataset"] for c in batch.call_args_list})


if __name__ == "__main__":
    unittest.main()
