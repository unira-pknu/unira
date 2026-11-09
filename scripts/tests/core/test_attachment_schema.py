from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock
from io import BytesIO

from scripts.preprocessing.extractors.hwp import (
    extract_hwp_blocks, _hwp_command,
)

from scripts.crawlers.common.schema import (
    attachment_error,
    attachment_text,
    build_attachment,
    normalize_attachment,
    sanitize_attachment_filename,
    unique_attachment_filename,
    validate_attachment,
)
from scripts.preprocessing.rule import extract_rule_json_blocks
from scripts.preprocessing import repair_archive_collisions as archive_repair
from scripts.preprocessing import repair_attachment_formats as format_repair
from scripts.preprocessing.file_formats import FileFormat, detect_file_format, extract_formatted_blocks
from scripts.preprocessing.file_preprocessing import (
    decoded_archive_member_name, is_safe_archive_member, archive_member_output_path,
    legacy_archive_member_output_path, save_preprocessed_archive,
    save_preprocessed_archive_member, validate_archive_member_targets, write_archive_result,
)

class AttachmentSchemaTests(unittest.TestCase):
    def test_retry_recovers_unvisited_successes_from_the_completed_history(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            before = {"source_path": "files/previous.pdf", "original_sha256": "same", "status": "processed"}
            changed = {"source_path": "files/changed.pdf", "original_sha256": "old", "status": "processed"}
            current = {"source_path": "files/current.pdf", "original_sha256": "current", "status": "unrecoverable"}
            (run / "results_before_retry_20261108.json").write_text(json.dumps({"results": [before, changed]}), encoding="utf-8")
            (run / "results.json").write_text(json.dumps({"results": [current]}), encoding="utf-8")
            plan = {"selected": [{"source_path": "files/previous.pdf", "original_sha256": "same"},
                                 {"source_path": "files/changed.pdf", "original_sha256": "new"},
                                 {"source_path": "files/current.pdf", "original_sha256": "current"}]}
            found = format_repair.previous_results(run, plan)
            self.assertEqual(found, {before["source_path"]: before, current["source_path"]: current})

    def test_attachment_retry_revalidates_artifact_and_searchable_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/retry"
            run.mkdir(parents=True)
            original = root / "files/original.pdf"
            original.write_bytes(b"verified original")
            artifact = run / "recovered.pdf"
            artifact.write_bytes(original.read_bytes())
            target = run / "preprocessed.json"
            document = {"source_path": "files/original.pdf", "chunks": [{"text": "Extracted original body"}]}
            target.write_text(json.dumps(document), encoding="utf-8")
            entry = {"source_path": "files/original.pdf", "original_sha256": format_repair.digest(original)}
            result = {**entry, "status": "processed", "processed_artifact": str(artifact),
                      "processed_sha256": format_repair.digest(artifact),
                      "documents": [{"source_path": entry["source_path"], "preprocessed_path": str(target), "chunks": 1}]}
            with patch.object(format_repair, "ROOT", root):
                self.assertTrue(format_repair.successful_result_is_current(result, entry, run))
                target.write_text(json.dumps({**document, "metadata_only": True}), encoding="utf-8")
                self.assertFalse(format_repair.successful_result_is_current(result, entry, run))
                target.write_text(json.dumps(document), encoding="utf-8")
                artifact.write_bytes(b"changed recovery copy")
                self.assertFalse(format_repair.successful_result_is_current(result, entry, run))

    def test_handysoft_envelope_is_a_known_unsupported_format_not_a_broken_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "original.pdf"
            path.write_bytes(b"Handysoft Approval Document File" + b"\0" * 80)
            detected = detect_file_format(path)
            self.assertEqual((detected.kind, detected.state), ("handysoft", "unverified"))

    def test_public_article_with_comment_password_dialog_is_not_a_login_wall(self):
        public = b'<input type="password" name="comment_password"><td class="bdvEdit">' + b'Public official attachment announcement. ' * 4 + b'</td>'
        self.assertIsNone(format_repair.public_page_error(public, "https://ce.pknu.ac.kr/ce/1814?no=1"))
        self.assertEqual(format_repair.public_page_error(b'<form><input type="password"></form>', "https://ce.pknu.ac.kr/login"), "LOGIN_REQUIRED")

    def test_public_recovery_uses_exact_host_tls_policy_without_disabling_verification(self):
        import requests
        with tempfile.TemporaryDirectory() as directory, requests.Session() as session:
            response = MagicMock(status_code=200, headers={})
            response.iter_content.return_value = [b"public attachment"]
            with patch.object(session, "get", return_value=response) as get:
                format_repair.fetch_file(session, "https://ce.pknu.ac.kr/boardDownload.do?no=1", Path(directory) / "result.bin")
            self.assertTrue(session._pknu_tls_mounted)
            self.assertIs(session.verify, True)
            self.assertNotIn("verify", get.call_args.kwargs)
            self.assertIs(get.call_args.kwargs["allow_redirects"], False)

    def test_zip_resume_defers_without_writes_while_prior_ingestion_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/zip_repair"
            run.mkdir(parents=True)
            (run / "receipt.json").write_text('{"status":"needs_attention","error":"old error"}', encoding="utf-8")
            with patch.object(archive_repair, "ROOT", root), patch.object(archive_repair, "preflight") as preflight, \
                 patch.object(archive_repair, "dependency_states", return_value=[{"path": "previous/receipt.json", "status": "loading"}]), \
                 patch.object(archive_repair, "apply") as apply, patch.object(archive_repair, "publish") as publish:
                archive_repair.resume(run, [root / "previous/receipt.json"])
            preflight.assert_called_once_with(run)
            apply.assert_not_called()
            publish.assert_not_called()
            state = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "waiting_for_ingestion")
            self.assertNotIn("error", state)

    def test_attachment_format_scan_joins_download_receipts_and_excludes_body_images(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "files/source"
            source.mkdir(parents=True)
            image = BytesIO()
            Image.new("RGB", (8, 8), "white").save(image, format="JPEG")
            payload = image.getvalue()
            wrong = source / "download.zip"
            wrong.write_bytes(payload)
            (source / "body.jpg").write_bytes(payload)
            checksum = hashlib.sha256(payload).hexdigest()
            (source / "notice.json").write_text(json.dumps({"url": "https://www.pknu.ac.kr/main/163?no=1", "title": "첨부 안내",
                "attachments": [{"saved_path": "files/source/download.zip", "downloaded": True, "name": "download.zip",
                                 "sha256": checksum, "url": "https://www.pknu.ac.kr/boardDownload.do?no=2"}]}), encoding="utf-8")
            run = root / "var/recovery/repair"
            with patch.object(format_repair, "ROOT", root), patch.object(format_repair, "scopes", return_value=[("ce", source, None)]):
                format_repair.scan(run)
            plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(len(plan["selected"]), 1)
            selected = plan["selected"][0]
            self.assertEqual(selected["detected"]["kind"], "jpeg")
            self.assertEqual(selected["download_history"][0]["expected_sha256"], checksum)
            self.assertEqual(selected["reasons"], ["extension_mismatch"])
            self.assertEqual(wrong.read_bytes(), payload)
            old_history = json.loads((source / "notice.json").read_text(encoding="utf-8"))
            old_history["attachments"][0]["saved_path"] = "files/legacy/download.zip"
            (source / "notice.json").write_text(json.dumps(old_history), encoding="utf-8")
            with patch.object(format_repair, "ROOT", root), patch.object(format_repair, "scopes", return_value=[("ce", source, None)]):
                format_repair.scan(root / "var/recovery/by_hash")
            rebound = json.loads((root / "var/recovery/by_hash/plan.json").read_text(encoding="utf-8"))
            current = next(r for r in rebound["selected"] if r["source_path"] == "files/source/download.zip")
            self.assertEqual(current["history_binding"], "sha256")
            self.assertEqual(current["download_history"][0]["saved_path"], "files/legacy/download.zip")

    def test_attachment_recovery_uses_hash_verified_cached_bytes_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cached = root / "files/cache.zip"
            cached.parent.mkdir(parents=True)
            with zipfile.ZipFile(cached, "w") as archive:
                archive.writestr("guide.txt", "신청 기간 안내")
            checksum = hashlib.sha256(cached.read_bytes()).hexdigest()
            entry = {"source_path": "files/broken.zip", "download_history": [{"expected_sha256": checksum}]}
            histories = {"files/cache.zip": [{"expected_sha256": checksum}]}
            with patch.object(format_repair, "ROOT", root), patch("requests.Session") as session:
                recovered, detected, evidence = format_repair.recover(entry, root / "var/recovery/repair", histories)
            self.assertEqual(hashlib.sha256(recovered.read_bytes()).hexdigest(), checksum)
            self.assertEqual(detected.kind, "zip")
            self.assertEqual(evidence["status"], "verified_cached_copy")
            session.assert_not_called()

    def test_attachment_jpeg_with_zip_suffix_is_detected_without_changing_original(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "download.zip"
            image = BytesIO()
            Image.new("RGB", (8, 8), "white").save(image, format="JPEG")
            path.write_bytes(image.getvalue())
            original = path.read_bytes()
            detected = detect_file_format(path)
            self.assertEqual((detected.kind, detected.extension, detected.state), ("jpeg", ".jpg", "valid"))
            self.assertFalse(detected.matches(".zip"))
            self.assertTrue(detected.matches(".jpeg"))
            with self.assertRaisesRegex(ValueError, "confirmed attachment"):
                extract_formatted_blocks(path, detected)
            with patch("scripts.preprocessing.file_formats.shutil.which", return_value="tesseract"), patch(
                    "scripts.preprocessing.extractors.image_ocr.extract_image", return_value={"blocks": [{"text": "신청 기간 안내"}]}) as ocr:
                blocks = extract_formatted_blocks(path, detected, attachment_image=True)
            self.assertEqual(blocks[0]["text"], "신청 기간 안내")
            ocr.assert_called_once()
            self.assertEqual(path.read_bytes(), original)

    def test_attachment_hwp_ole_identity_routes_zip_named_file_to_hwp_extractor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "download.zip"
            original = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"fixture"
            path.write_bytes(original)
            ole = MagicMock()
            ole.__enter__.return_value = ole
            ole.listdir.return_value = [["FileHeader"], ["BodyText", "Section0"]]
            ole.openstream.return_value.read.return_value = b"HWP Document File\0"
            with patch("olefile.OleFileIO", return_value=ole):
                detected = detect_file_format(path)
            self.assertEqual((detected.kind, detected.extension), ("hwp", ".hwp"))
            def extract(typed_path):
                self.assertEqual(typed_path.suffix, ".hwp")
                self.assertEqual(typed_path.read_bytes(), original)
                return [{"type": "paragraph", "text": "수강신청 절차 안내"}]
            with patch("scripts.preprocessing.extractors.common.extract_blocks", side_effect=extract):
                self.assertTrue(extract_formatted_blocks(path, detected))
            self.assertEqual(path.read_bytes(), original)

    def test_attachment_container_signatures_distinguish_office_hwpx_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for member, kind, ext in (("word/document.xml", "docx", ".docx"),
                                      ("xl/workbook.xml", "xlsx", ".xlsx"),
                                      ("ppt/presentation.xml", "pptx", ".pptx")):
                path = root / (kind + ".zip")
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("[Content_Types].xml", "<Types/>")
                    archive.writestr(member, "<document/>")
                detected = detect_file_format(path)
                self.assertEqual((detected.kind, detected.extension, detected.state), (kind, ext, "valid"))
            hwpx = root / "hwpx.zip"
            with zipfile.ZipFile(hwpx, "w") as archive:
                archive.writestr("mimetype", "application/hwp+zip")
                archive.writestr("Contents/section0.xml", "<section/>")
            self.assertEqual(detect_file_format(hwpx).kind, "hwpx")
            bundle = root / "ordinary_bundle.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("word/document.xml", "<document/>")
            self.assertEqual(detect_file_format(bundle).kind, "zip")
            broken = root / "broken.zip"
            broken.write_bytes(b"PK\x03\x04truncated")
            self.assertEqual(detect_file_format(broken).state, "damaged")
            crc = root / "crc.zip"
            with zipfile.ZipFile(crc, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("body.txt", b"unique body bytes")
            crc.write_bytes(crc.read_bytes().replace(b"unique body bytes", b"broken body bytes"))
            self.assertEqual(detect_file_format(crc).state, "damaged")

    def test_attachment_recovery_does_not_follow_external_or_login_redirects(self):
        with tempfile.TemporaryDirectory() as directory:
            for destination, code in (("https://outside.test/file", "SOURCE_DEFERRED"),
                                      ("https://www.pknu.ac.kr/login", "LOGIN_REQUIRED")):
                session, response = MagicMock(), MagicMock()
                response.status_code = 302
                response.headers = {"Location": destination}
                session.get.return_value = response
                with self.subTest(destination=destination), self.assertRaises(format_repair.RecoveryError) as caught:
                    format_repair.fetch_file(session, "https://www.pknu.ac.kr/boardDownload.do?no=1", Path(directory) / "file.zip")
                self.assertEqual(caught.exception.code, code)
                session.get.assert_called_once()
                response.close.assert_called_once()
            self.assertFalse((Path(directory) / "file.zip").exists())

    def test_attachment_recovery_rejects_success_status_html_login_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = {"source_path": "files/broken.zip", "download_history": [
                {"url": "https://www.pknu.ac.kr/boardDownload.do?no=1", "expected_sha256": None}]}
            def fetch(session, url, target, **kwargs):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('<html><input type="password"></html>', encoding="utf-8")
                return {"final_url": url}
            with patch.object(format_repair, "ROOT", root), patch.object(format_repair, "fetch_file", side_effect=fetch), patch.object(
                    format_repair, "refreshed_urls", return_value=([], [])):
                with self.assertRaises(format_repair.RecoveryError):
                    format_repair.recover(entry, root / "var/recovery/repair", {}, network=True)
            attempts = list((root / "var/recovery/repair").rglob("attempts.json"))
            self.assertEqual(len(attempts), 1)
            details = json.loads(attempts[0].read_text(encoding="utf-8"))
            self.assertTrue(any(row.get("code") == "LOGIN_REQUIRED" for row in details))
            self.assertFalse(list((root / "var/recovery/repair").rglob("recovered.*")))

    def test_attachment_processing_reports_three_outcomes_and_never_uses_metadata_as_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/repair"
            run.mkdir(parents=True)
            selected = []
            for name, state in (("success.pdf", "valid"), ("empty.pdf", "valid"), ("broken.zip", "damaged")):
                source = root / "files" / name
                source.write_bytes(b"original fixture " + name.encode())
                selected.append({"source_path": "files/" + name, "dataset": "ce", "declared_ext": source.suffix,
                                 "original_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "reasons": [],
                                 "detected": FileFormat("pdf" if state == "valid" else "zip", source.suffix, state, "fixture").to_dict(),
                                 "download_history": []})
            (run / "plan.json").write_text(json.dumps({"selected": selected, "history_issues": []}), encoding="utf-8")
            def extract(path, detected, **kwargs):
                return ([{"type": "paragraph", "text": "수강신청 절차와 신청 기간에 대한 안내입니다."}]
                        if path.name == "success.pdf" else [{"type": "metadata", "text": "document title only"}])
            with patch.object(format_repair, "ROOT", root), patch.object(format_repair, "history_index", return_value=({}, {}, [])), patch.object(
                    format_repair, "extract_formatted_blocks", side_effect=extract):
                format_repair.process(run)
            result = json.loads((run / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(result["summary"], {"processed": 1, "body_extraction_failed": 1, "unrecoverable": 1})
            for entry in selected:
                self.assertEqual(hashlib.sha256((root / entry["source_path"]).read_bytes()).hexdigest(), entry["original_sha256"])
            self.assertFalse((root / "preprocessed").exists())

    def test_attachment_body_parse_failure_retries_original_source_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/repair"
            run.mkdir(parents=True)
            original = root / "files/document.zip"
            original.write_bytes(b"damaged inner document stream")
            recovered = run / "recovered.docx"
            recovered.write_bytes(b"new version downloaded from the recorded source")
            detected = FileFormat("docx", ".docx", "valid", "readable container")
            entry = {"source_path": "files/document.zip", "dataset": "ce", "declared_ext": ".zip",
                     "original_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                     "detected": detected.to_dict(), "reasons": ["extension_mismatch"],
                     "download_history": [{"history_path": "files/notice.json", "downloaded": True,
                                           "source_page_url": "https://www.pknu.ac.kr/main/163?no=1"}]}
            (run / "plan.json").write_text(json.dumps({"selected": [entry], "history_issues": []}), encoding="utf-8")
            recovery = {"status": "downloaded_valid", "requires_source_review": True}
            with patch.object(format_repair, "ROOT", root), patch.object(format_repair, "history_index", return_value=({}, {}, [])), patch.object(
                    format_repair, "recover", return_value=(recovered, detected, recovery)) as recover, patch.object(
                    format_repair, "extract_formatted_blocks", side_effect=[ValueError("corrupt document stream"),
                        [{"type": "paragraph", "text": "수강신청 절차와 신청 기간에 대한 안내입니다."}]]):
                format_repair.process(run, network=True)
            result = json.loads((run / "results.json").read_text(encoding="utf-8"))["results"][0]
            self.assertEqual(result["status"], "processed")
            self.assertTrue(result["recovery"]["requires_source_review"])
            self.assertIn("corrupt document stream", result["original_body_error"])
            recover.assert_called_once()
            self.assertFalse(recover.call_args.kwargs["cached"])
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), entry["original_sha256"])

    def test_zip_repair_plans_and_stages_only_collisions_without_touching_shared_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "files/source/sample.zip"
            archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("nested/guide.pdf", b"pdf")
                zf.writestr("nested/guide.hwp", b"hwp")
                zf.writestr("unaffected.txt", b"keep")
            old = root / "preprocessed/ce/files/zip/sample/nested/guide.json"
            old.parent.mkdir(parents=True)
            source = "files/source/sample.zip!/nested/guide.hwp"
            old.write_text(json.dumps({"source_path": source, "slug": "old-slug",
                                      "provenance": {"doc_url": "https://example.test/notice"}}), encoding="utf-8")
            before = old.read_bytes()
            run = root / "var/recovery/zip_repair"
            with patch.object(archive_repair, "ROOT", root):
                archive_repair.prepare(run)
                plan = archive_repair.read(run / "plan.json")
                self.assertEqual(len(plan["members"]), 2)
                self.assertFalse(plan["issues"])
                plan_bytes = (run / "plan.json").read_bytes()
                receipt_bytes = (run / "receipt.json").read_bytes()
                archive_repair.prepare(run)
                self.assertEqual((run / "plan.json").read_bytes(), plan_bytes)
                self.assertEqual((run / "receipt.json").read_bytes(), receipt_bytes)
                with patch("scripts.preprocessing.file_preprocessing.extract_blocks", return_value=[
                        {"type": "paragraph", "text": "수강신청 절차와 신청기간 안내"}]):
                    archive_repair.stage(run, plan)
                docs = archive_repair.read(run / "documents.json")
                self.assertEqual(len({d["target"] for d in docs}), 2)
                hwp = next(d for d in docs if d["member"].endswith(".hwp"))
                self.assertEqual(hwp["slug"], "old-slug")
                self.assertEqual(old.read_bytes(), before)
                self.assertFalse((old.parent / "guide.pdf.json").exists())
                self.assertFalse((root / "vectorized").exists())

    def test_zip_repair_rejects_apply_while_an_earlier_job_is_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "var/recovery/old/receipt.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text('{"status":"loading"}', encoding="utf-8")
            with patch.object(archive_repair, "ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "has not completed"):
                    archive_repair.require_finished([receipt])
                receipt.write_text('{"status":"complete"}', encoding="utf-8")
                archive_repair.require_finished([receipt])
                with self.assertRaises(ValueError):
                    archive_repair.require_finished([])

    def test_zip_repair_publication_preserves_unrelated_index_rows_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "files/source/sample.zip"
            archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("guide.pdf", b"pdf")
                zf.writestr("guide.hwp", b"hwp")
            previous = root / "preprocessed/ce/files/zip/sample/guide.json"
            previous.parent.mkdir(parents=True)
            previous.write_text(json.dumps({"source_path": "files/source/sample.zip!/guide.hwp",
                                            "slug": "legacy-slug", "provenance": {}}), encoding="utf-8")
            original = previous.read_bytes()
            old_record = {"source_slug": "legacy-slug", "chunk_index": 0, "text": "old body"}
            unrelated = {"source_slug": "unrelated", "chunk_index": 0, "text": "preserve this document"}
            vector_root = root / "vectorized/ce"
            vector_root.mkdir(parents=True)
            index = vector_root / "index.jsonl"
            index.write_text("\n".join(json.dumps(r) for r in (old_record, unrelated)) + "\n", encoding="utf-8")
            (vector_root / "manifest.json").write_text("{}", encoding="utf-8")
            gate = root / "var/recovery/previous/receipt.json"
            gate.parent.mkdir(parents=True)
            gate.write_text('{"status":"complete"}', encoding="utf-8")
            run = root / "var/recovery/zip_repair"
            with patch.object(archive_repair, "ROOT", root):
                archive_repair.prepare(run)
                plan = archive_repair.read(run / "plan.json")
                with patch("scripts.preprocessing.file_preprocessing.extract_blocks", return_value=[
                        {"type": "paragraph", "text": "수강신청 절차와 신청기간 안내"}]):
                    archive_repair.stage(run, plan)
                docs = archive_repair.read(run / "documents.json")
                for doc in docs:
                    vector = run / "vectorized/ce" / (doc["slug"] + ".json")
                    record = {"source_slug": doc["slug"], "chunk_index": 0, "text": doc["member"],
                              "metadata": {"source_path": doc["source_path"]}}
                    archive_repair.atomic(vector, {"records": [record], "source_preprocessed_path": doc["target"]})
                    doc["vector"] = archive_repair.relative(vector)
                    doc["vector_sha256"] = archive_repair.digest(vector)
                archive_repair.atomic(run / "documents.json", docs)
                archive_repair.receipt(run, vectorization_complete=True, database_verified=True)
                with patch("scripts.vectorization.load_to_supabase.preflight_index"):
                    archive_repair.publish(run, plan, [gate])
                    archive_repair.publish(run, plan, [gate])
                merged = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(merged), 3)
                self.assertIn(unrelated, merged)
                self.assertEqual(len({r["source_slug"] for r in merged}), 3)
                self.assertEqual((run / "backup" / previous.relative_to(root)).read_bytes(), original)
                self.assertFalse(previous.exists())
                for doc in docs:
                    self.assertEqual(archive_repair.digest(root / doc["target"]), doc["sha256"])

    def test_zip_same_stem_extensions_and_nested_directories_have_distinct_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sample.zip"
            output = root / "preprocessed"
            for layout in ("flat", "by_ext"):
                paths = [archive_member_output_path(archive, root, output, member, layout=layout)
                         for member in ("첨부/안내.pdf", "첨부/안내.hwp", "다른폴더/안내.pdf")]
                self.assertEqual(len(set(paths)), 3)
                self.assertEqual(paths[0].name, "안내.pdf.json")
                self.assertEqual(paths[1].name, "안내.hwp.json")
                self.assertEqual(paths[0].parent.name, "첨부")

    def test_zip_restores_both_members_preserving_only_the_matching_legacy_slug(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sample.zip"
            output = root / "preprocessed"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("첨부/안내.pdf", b"pdf original")
                zf.writestr("첨부/안내.hwp", b"hwp original")
            old = legacy_archive_member_output_path(archive, root, output, "첨부/안내.pdf")
            old.parent.mkdir(parents=True)
            original = json.dumps({"source_path": "sample.zip!/첨부/안내.hwp", "slug": "kept-hwp-slug"}).encode()
            old.write_bytes(original)
            def blocks(path, **kwargs):
                return [{"type": "paragraph", "text": "수강신청 절차와 신청기간 안내 " + path.suffix}]
            with patch("scripts.preprocessing.file_preprocessing.extract_blocks", side_effect=blocks):
                saved, _ = save_preprocessed_archive(archive, root, output, root, {}, 900, 120, "never", "kor+eng", 200)
            self.assertTrue(saved)
            pdf = json.loads(archive_member_output_path(archive, root, output, "첨부/안내.pdf").read_text(encoding="utf-8"))
            hwp = json.loads(archive_member_output_path(archive, root, output, "첨부/안내.hwp").read_text(encoding="utf-8"))
            self.assertNotEqual(pdf["slug"], "kept-hwp-slug")
            self.assertEqual(hwp["slug"], "kept-hwp-slug")
            self.assertEqual(pdf["source_path"], "sample.zip!/첨부/안내.pdf")
            self.assertEqual(hwp["provenance"]["archive_member_path"], "첨부/안내.hwp")
            self.assertEqual(pdf["provenance"]["archive_member_sha256"], hashlib.sha256(b"pdf original").hexdigest())
            self.assertEqual(old.with_name(old.name + ".legacy").read_bytes(), original)
            self.assertFalse(old.exists())
            with patch("scripts.preprocessing.file_preprocessing.extract_blocks", side_effect=blocks):
                save_preprocessed_archive(archive, root, output, root, {}, 900, 120, "never", "kor+eng", 200)
            again = json.loads(archive_member_output_path(archive, root, output, "첨부/안내.hwp").read_text(encoding="utf-8"))
            self.assertEqual(again["slug"], hwp["slug"])

    def test_zip_duplicate_and_windows_alias_names_fail_before_writing(self):
        for names in (["a.pdf", "a.pdf"], ["A.pdf", "a.pdf"], ["CON.pdf"],
                      ["a.pdf", "a.pdf.json/child.txt"]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                validate_archive_member_targets(names)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sample.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("A.pdf", b"first")
                zf.writestr("a.pdf", b"second")
            with patch("scripts.preprocessing.file_preprocessing.extract_blocks") as extract:
                with self.assertRaises(ValueError):
                    save_preprocessed_archive(archive, root, root / "out", root, {}, 900, 120, "never", "kor+eng", 200)
            extract.assert_not_called()
            self.assertFalse((root / "out").exists())

    def test_zip_refuses_foreign_output_and_atomic_failure_preserves_existing_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "member.pdf.json"
            original = b'{"source_path":"archive.zip!/other.pdf"}'
            path.write_bytes(original)
            with self.assertRaises(ValueError):
                write_archive_result(path, {"source_path": "archive.zip!/member.pdf"})
            self.assertEqual(path.read_bytes(), original)
            with patch("scripts.preprocessing.file_preprocessing.os.replace", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    write_archive_result(path, {"source_path": "archive.zip!/other.pdf", "chunks": ["new"]})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_zip_staging_uses_legacy_identity_without_changing_shared_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, extracted = root / "sample.zip", root / "guide.pdf"
            extracted.write_bytes(b"pdf bytes")
            previous = root / "shared" / "guide.json"
            previous.parent.mkdir()
            original = b'{"source_path":"sample.zip!/guide.pdf","slug":"stable-slug"}'
            previous.write_bytes(original)
            with patch("scripts.preprocessing.file_preprocessing.extract_blocks", return_value=[{"type": "paragraph", "text": "수강신청 절차와 등록금 납부기간 안내"}]):
                saved, output = save_preprocessed_archive_member(archive, extracted, "guide.pdf", root,
                    root / "staged", root, {"doc_url": "https://example.test/notice"},
                    900, 120, "never", "kor+eng", 200, previous_outputs=[previous], retire_previous=False)
            self.assertTrue(saved)
            self.assertEqual(previous.read_bytes(), original)
            doc = json.loads(Path(output).read_text(encoding="utf-8"))
            self.assertEqual(doc["slug"], "stable-slug")
            self.assertEqual(doc["provenance"]["doc_url"], "https://example.test/notice")
            self.assertEqual(doc["provenance"]["previous_source_path"], "sample.zip!/guide.pdf")

    def test_zip_replaces_verified_mojibake_result_after_successful_extraction(self):
        class KoreanZipInfo(zipfile.ZipInfo):
            def _encodeFilenameFlags(self):
                return self.filename.encode("cp949"), self.flag_bits & ~0x800

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sample.zip"
            output = root / "preprocessed"
            name = "첨부/수강신청.txt"
            original = name.encode("cp949").decode("cp437")
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(KoreanZipInfo(name), "수강신청 안내입니다.".encode("utf-8"))
            stale = archive_member_output_path(archive, root, output, original)
            stale.parent.mkdir(parents=True)
            stale.write_text(json.dumps({"source_path": f"sample.zip!/{original}"}), encoding="utf-8")
            saved, _ = save_preprocessed_archive(archive, root, output, root, {},
                                                900, 120, "never", "kor+eng", 200)
            corrected = archive_member_output_path(archive, root, output, name)
            self.assertTrue(saved)
            self.assertTrue(corrected.is_file())
            self.assertFalse(stale.exists())
            result = json.loads(corrected.read_text(encoding="utf-8"))
            self.assertEqual(f"sample.zip!/{name}", result["source_path"])
            self.assertGreater(result["num_chunks"], 0)

    def test_zip_restores_korean_legacy_names_and_preserves_utf8(self):
        name = "첨부/수강신청 안내.hwp"
        legacy = zipfile.ZipInfo(name.encode("cp949").decode("cp437"))
        self.assertEqual(name, decoded_archive_member_name(legacy))
        utf8 = zipfile.ZipInfo(name)
        utf8.flag_bits |= 0x800
        self.assertEqual(name, decoded_archive_member_name(utf8))
        plain = zipfile.ZipInfo("guide.txt")
        self.assertEqual("guide.txt", decoded_archive_member_name(plain))
        for unsafe in ("../첨부.hwp", "C:/첨부.hwp", "/첨부.hwp"):
            info = zipfile.ZipInfo(unsafe.encode("cp949").decode("cp437"))
            self.assertFalse(is_safe_archive_member(decoded_archive_member_name(info)))

    def test_hwp_uses_current_interpreter_instead_of_windows_launcher(self) -> None:
        with patch("scripts.preprocessing.extractors.hwp.importlib.util.find_spec", return_value=object()), patch(
            "scripts.preprocessing.extractors.hwp.shutil.which"
        ) as which:
            command = _hwp_command("hwp5html")
        self.assertEqual(command, [sys.executable, "-c", "from hwp5.hwp5html import main; main()"])
        which.assert_not_called()

    def test_hwp_html_success_does_not_require_text_converter(self) -> None:
        def convert(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_text(
                '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>yearbook</p>'
                '<table><tr><td>2026</td><td>42</td></tr></table></body></html>', encoding="utf-8"
            )
            self.assertIsNone(kwargs["timeout"])
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "yearbook.hwp"
            path.write_bytes(b"fixture")
            with patch("scripts.preprocessing.extractors.hwp._hwp_command", side_effect=lambda name: [name] if name == "hwp5html" else None), patch(
                "scripts.preprocessing.extractors.hwp.subprocess.run", side_effect=convert
            ) as run:
                blocks = extract_hwp_blocks(path)
            self.assertEqual([block["text"] for block in blocks], ["yearbook", "2026 | 42"])
            self.assertEqual(run.call_count, 1)

    def test_hwp_html_failure_does_not_fall_back_and_cleans_temporary_files(self) -> None:
        commands = []

        def convert(command, **kwargs):
            commands.append(command)
            Path(command[command.index("--output") + 1]).write_text("partial", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="conversion failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "yearbook.hwp"
            path.write_bytes(b"fixture")
            with patch("scripts.preprocessing.extractors.hwp._hwp_command", return_value=["hwp5html"]) as resolve, patch(
                "scripts.preprocessing.extractors.hwp.subprocess.run", side_effect=convert
            ) as run:
                with self.assertRaisesRegex(RuntimeError, "hwp5html failed: conversion failed"):
                    extract_hwp_blocks(path)
            self.assertEqual(run.call_count, 1)
            resolve.assert_called_once_with("hwp5html")
            self.assertFalse(Path(commands[0][-1]).exists())
            self.assertFalse(Path(commands[0][commands[0].index("--output") + 1]).exists())

    def test_hwp_missing_html_converter_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "yearbook.hwp"
            path.write_bytes(b"fixture")
            with patch("scripts.preprocessing.extractors.hwp._hwp_command", return_value=None), patch(
                "scripts.preprocessing.extractors.hwp.subprocess.run"
            ) as run:
                with self.assertRaisesRegex(RuntimeError, "hwp5html command not found"):
                    extract_hwp_blocks(path)
            run.assert_not_called()

    def test_hwp_missing_or_invalid_html_is_an_error(self) -> None:
        for output, message in (
            (None, "did not produce HTML"),
            ("<html>", "invalid XHTML"),
            ("<html />", "without a body"),
        ):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "yearbook.hwp"
                path.write_bytes(b"fixture")

                def convert(command, **kwargs):
                    if output is not None:
                        Path(command[command.index("--output") + 1]).write_text(output, encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

                with patch("scripts.preprocessing.extractors.hwp._hwp_command", return_value=["hwp5html"]), patch(
                    "scripts.preprocessing.extractors.hwp.subprocess.run", side_effect=convert
                ) as run:
                    with self.assertRaisesRegex(RuntimeError, message):
                        extract_hwp_blocks(path)
                self.assertEqual(run.call_count, 1)

    def test_download_success_has_relative_path_size_and_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            saved = root / "files" / "guide.pdf"
            saved.parent.mkdir(parents=True)
            payload = b"fixture-pdf"
            saved.write_bytes(payload)

            attachment = build_attachment(
                index=1,
                name="guide.pdf",
                url="https://example.test/guide.pdf",
                final_url="https://cdn.example.test/guide.pdf",
                saved_path=saved,
                downloaded=True,
                project_root=root,
                content_type="application/pdf; charset=binary",
            )

            self.assertEqual(validate_attachment(attachment, root), [])
            self.assertEqual(attachment["saved_path"], "files/guide.pdf")
            self.assertEqual(attachment["size_bytes"], len(payload))
            self.assertEqual(attachment["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(attachment["final_url"], "https://cdn.example.test/guide.pdf")
            self.assertEqual(attachment["downloaded_from_url"], attachment["final_url"])

    def test_http_failure_keeps_attachment_with_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            attachment = build_attachment(
                index=2,
                name="failed.hwp",
                url="https://example.test/failed.hwp",
                final_url="https://example.test/failed.hwp",
                project_root=root,
                error=attachment_error("HTTP_503", "service unavailable", True),
            )
            self.assertEqual(validate_attachment(attachment, root), [])
            self.assertFalse(attachment["downloaded"])
            self.assertIsNone(attachment["saved_path"])
            self.assertEqual(attachment["error"]["code"], "HTTP_503")
            self.assertTrue(attachment["error"]["retryable"])

    def test_empty_file_is_valid_download_with_zero_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            saved = root / "empty.pdf"
            saved.write_bytes(b"")
            attachment = build_attachment(
                index=1,
                name=saved.name,
                url="https://example.test/empty.pdf",
                saved_path=saved,
                downloaded=True,
                project_root=root,
                text=attachment_text("skipped", extractor="fixture", error="empty file"),
            )
            self.assertEqual(validate_attachment(attachment, root), [])
            self.assertEqual(attachment["size_bytes"], 0)
            self.assertEqual(attachment["text"]["status"], "skipped")

    def test_unsupported_extension_and_extraction_failure_are_typed(self) -> None:
        skipped = attachment_text(
            "skipped", characters=0, extractor="fixture", error="unsupported extension: .rar"
        )
        failed = attachment_text("failed", extractor="fixture", error="parser crashed")
        self.assertEqual(skipped["status"], "skipped")
        self.assertIsInstance(skipped["characters"], int)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "parser crashed")

    def test_text_extraction_success_is_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            saved = root / "rule.hwp"
            saved.write_bytes(b"rule")
            attachment = build_attachment(
                index=1,
                name="rule.hwp",
                url="https://example.test/rule.hwp",
                saved_path=saved,
                downloaded=True,
                project_root=root,
                text=attachment_text(
                    "success", content="extracted rule", extractor="fixture"
                ),
            )
            encoded = json.dumps(attachment, ensure_ascii=False)
            decoded = json.loads(encoded)
            self.assertEqual(decoded["text"]["characters"], len("extracted rule"))
            self.assertEqual(validate_attachment(decoded, root), [])

    def test_filename_sanitizing_and_collision_resolution_are_shared(self) -> None:
        self.assertEqual(sanitize_attachment_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_attachment_filename('bad<>:"name?.pdf'), "bad____name_.pdf")
        used: set[str] = set()
        self.assertEqual(unique_attachment_filename("Report.PDF", used), "Report.PDF")
        self.assertEqual(unique_attachment_filename("report.pdf", used), "report_2.pdf")

    def test_legacy_attachment_is_upgraded_without_losing_legacy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            saved = root / "legacy.pdf"
            saved.write_bytes(b"legacy")
            upgraded = normalize_attachment(
                {
                    "name": "legacy.pdf",
                    "url": "https://example.test/legacy.pdf",
                    "saved_path": saved.as_posix(),
                    "downloaded_from_url": "https://cdn.example.test/legacy.pdf",
                    "source_page_url": "https://example.test/post/1",
                },
                index=3,
                project_root=root,
            )
            self.assertEqual(validate_attachment(upgraded, root), [])
            self.assertEqual(upgraded["id"], "attachment-003")
            self.assertEqual(upgraded["source_page_url"], "https://example.test/post/1")

    def test_rule_preprocessing_prefers_canonical_attachment_text(self) -> None:
        doc = {
            "title": "rule",
            "category": "pknu_rule_law",
            "subcategory": "regulation",
            "type": "regulation",
            "date": "2026-09-02",
            "source_id": "law:1",
            "url": "https://example.test/rule/1",
            "html_text": "body text long enough to avoid requiring an HTML fallback",
            "attachments": [
                {
                    "name": "rule.pdf",
                    "url": "https://example.test/rule.pdf",
                    "saved_path": "files/rule.pdf",
                    "text": attachment_text(
                        "success", content="canonical attachment text", extractor="fixture"
                    ),
                }
            ],
            "file_preview_texts": [{"text": "legacy attachment text"}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            blocks, _ = extract_rule_json_blocks(doc, Path(temp_dir))
        block_texts = [block["text"] for block in blocks]
        self.assertIn("canonical attachment text", block_texts)
        self.assertNotIn("legacy attachment text", block_texts)


if __name__ == "__main__":
    unittest.main()
