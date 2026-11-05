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
from scripts.preprocessing.file_preprocessing import (
    decoded_archive_member_name, is_safe_archive_member, archive_member_output_path,
    save_preprocessed_archive,
)

class AttachmentSchemaTests(unittest.TestCase):
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
