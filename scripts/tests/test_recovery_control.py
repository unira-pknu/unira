"""Regression coverage for recovery checkpoints and atomic statement retries."""
from contextlib import contextmanager
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.vectorization.recovery_control import (
    LoadOptions, checkpoint_ids, recovery_lock, resume_plan, write_chunk_batches,
    replace_with_retry,
)
from scripts.vectorization import reconcile_ce
from scripts.vectorization import reconcile_active_sources
from scripts.preprocessing import review_text_quality
from scripts.preprocessing import quality_audit
from scripts.vectorization import resume_excel
from scripts.vectorization import verify_recovery_search
from scripts.vectorization import publish_recovery
from scripts.recovery import cli as recovery_cli
from scripts.recovery import common as recovery_common
from scripts.artifact_paths import resolve_artifact_path, reference_path


class TransientFailure(RuntimeError):
    pass


class RecoveryControlTests(unittest.TestCase):
    def test_quality_audit_separates_condition_indicators_from_universal_missing_fields(self):
        summary = quality_audit.ScopeSummary()
        summary.add("rag", "a", "ce", 0, "2021학년도 이전 입학생의 학과별 졸업 기준", {"department_id": "ce"})
        summary.add("rag", "b", "ce", 0, "입학연도별 안내", {"applicable_admission_years": [2021]})
        summary.add("rag", "c", "ce", 0, "도서관 연락처 안내", {})
        summary.add("rule", "d", "rule", 0, "공포한 날부터 시행", {"published_at": "2026-11-01"})
        report = summary.export()
        cohort = report["conditions"]["admission_year"]
        self.assertEqual(cohort["condition_indicated"], 2)
        self.assertEqual(cohort["structured_condition_missing"], 1)
        self.assertEqual(cohort["indicated_missing_rate"], 0.5)
        self.assertEqual(cohort["not_indicated"], 2)
        self.assertEqual(report["publisher_department_metadata_present"], 1)
        self.assertEqual(report["conditions"]["department"]["structured_condition_missing"], 1)
        self.assertEqual(report["conditions"]["effective_date"]["structured_condition_missing"], 1)

    def test_quality_audit_does_not_accept_unconfirmed_dates_or_login_numbers_as_cohorts(self):
        value = quality_audit.usable_scope({"effective_at": "2026-11-01", "rule_effective_status": "unconfirmed",
                                           "applicable_admission_years": [True]})
        self.assertFalse(value["effective_date"])
        self.assertFalse(value["admission_year"])
        self.assertIsNone(quality_audit.COHORT_CUE.search("포털 로그인 학번을 입력합니다"))
        self.assertIsNotNone(quality_audit.COHORT_CUE.search("2021학년도 이전 입학생"))
        empty = quality_audit.ScopeSummary().export()
        self.assertIsNone(empty["conditions"]["admission_year"]["indicated_missing_rate"])

    def test_quality_audit_does_not_hide_conflicting_duplicate_current_chunks(self):
        expected = {("rag", "source", 0): {"current_record": "not_scanned", "historical_text_sha256": "old"}}
        quality_audit._observe(expected, "rag", "source", 0, "본문 �", active=False)
        self.assertTrue(expected[("rag", "source", 0)]["current_replacement"])
        self.assertIs(expected[("rag", "source", 0)]["active"], False)
        quality_audit._observe(expected, "rag", "source", 0, "다른 본문", active=False)
        self.assertEqual(expected[("rag", "source", 0)]["current_record"], "ambiguous_duplicate")
        self.assertIsNone(expected[("rag", "source", 0)]["current_replacement"])

    def test_quality_audit_database_sets_read_only_snapshot_before_selecting_rows(self):
        commands = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, statement, params=None):
                commands.append(str(statement))

            def __iter__(self):
                return iter([])

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, statement):
                commands.append(statement)
                return SimpleNamespace(fetchone=lambda: {"observed_at": "test", "transaction_snapshot": "1:2:"})

            def cursor(self, **kwargs):
                return Cursor()

        with patch("scripts.db.connect_postgres", return_value=Connection()), redirect_stderr(io.StringIO()):
            result = quality_audit.audit_database({})
        self.assertEqual(commands[0], "set transaction isolation level repeatable read read only")
        self.assertEqual(result["snapshot"]["transaction_snapshot"], "1:2:")
        self.assertEqual(result["summary"]["documents"], 0)
        self.assertIsNone(result["summary"]["conditions"]["department"]["indicated_missing_rate"])

    def test_quality_audit_cli_bypasses_writer_locks_and_preserves_frozen_receipts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/quality"
            recovery_common.atomic(run / "plan.json", {"policy": "original-text-quality-review-v1"})
            recovery_common.receipt(run, status="review_required")
            before = {p.name: p.read_bytes() for p in run.iterdir()}
            handler = SimpleNamespace(audit=lambda selected, **kwargs: {"read_only": True})
            with patch.object(recovery_cli, "ROOT", root), patch.object(
                recovery_cli.importlib, "import_module", return_value=handler
            ), patch.object(recovery_cli, "execute") as execute, patch.object(
                recovery_cli, "recovery_lock"
            ) as lock, redirect_stdout(io.StringIO()):
                self.assertEqual(recovery_cli.main(["--task", "quality", "--run", str(run), "--phase", "audit"]), 0)
                execute.assert_not_called()
                lock.assert_not_called()
            self.assertEqual({p.name: p.read_bytes() for p in run.iterdir()}, before)

    def test_quality_audit_rechecks_changed_preprocessing_instead_of_using_stage_guards(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/quality"
            target = root / "preprocessed/ce/doc.json"
            recovery_common.atomic(target, {"chunks": [{"text": "이전 �"}]})
            old_hash = recovery_common.digest(target)
            recovery_common.atomic(run / "plan.json", {"policy": "original-text-quality-review-v1", "garbled_chunks": 1,
                "items": [{"dataset": "ce", "id": "source", "target": "files/preprocessed/ce/doc.json",
                           "target_sha256": old_hash, "affected": [{"chunk_index": 0}], "index_hashes": {"0": "old"}}]})
            recovery_common.receipt(run, status="review_required", plan_sha256=recovery_common.digest(run / "plan.json"))
            recovery_common.atomic(target, {"chunks": [{"text": "변경된 본문"}]})
            receipt_before = (run / "receipt.json").read_bytes()
            with patch("scripts.vectorization.datasets.table_family", return_value="rag"):
                historical, selected = quality_audit.historical_selection(root, run)
            self.assertEqual(historical["current_preprocessed"]["changed"], 1)
            self.assertEqual(historical["missing_decisions"], 1)
            self.assertFalse(selected[("rag", "source", 0)]["preprocessed_body_replacement"])
            self.assertEqual((run / "receipt.json").read_bytes(), receipt_before)

    def test_moved_artifacts_keep_signed_evidence_and_historical_run_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old_run = root / "files/_archive/example"
            old_run.mkdir(parents=True)
            document = b'{"policy_version":2,"staged":"files/_archive/example/preprocessed/sample.json"}'
            (old_run / "plan.json").write_bytes(document)
            (root / "var/recovery").mkdir(parents=True)
            old_run.rename(root / "var/recovery/example")
            recovery_common.atomic(root / "var/storage_paths.json", {
                "moved_roots": ["files/_archive", "files/preprocessed", "files/vectorized"],
                "legacy_recovery_runs": ["example"],
            })
            actual = resolve_artifact_path(root, "files/_archive/example/plan.json")
            self.assertEqual(actual, root / "var/recovery/example/plan.json")
            self.assertEqual(actual.read_bytes(), document)
            self.assertEqual(reference_path(actual.parent, root), "files/_archive/example")
            self.assertEqual(reference_path(root / "var/recovery/new_run", root), "var/recovery/new_run")
            self.assertEqual(resolve_artifact_path(root, "files/preprocessed/ce/source.json"),
                             root / "preprocessed/ce/source.json")
            with patch.object(recovery_cli, "ROOT", root):
                self.assertEqual(recovery_cli.run_path("files/_archive/example"), actual.parent)

    def test_artifact_alias_cannot_escape_workspace_or_choose_ambiguous_copy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for parent in ("files/preprocessed", "preprocessed"):
                folder = root / parent
                folder.mkdir(parents=True)
                (folder / "source.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Both legacy and canonical"):
                resolve_artifact_path(root, "files/preprocessed/source.json")
            with self.assertRaisesRegex(ValueError, "outside workspace"):
                resolve_artifact_path(root, "files/preprocessed/../../../outside.json")

    def test_relocated_backup_uses_frozen_ledger_even_when_path_length_changes(self):
        from scripts.preprocessing import repair_archive_collisions as repair
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/example"
            saved = run / "backup/_long_paths/original.json"
            recovery_common.atomic(saved, {"original": True})
            ledger = {"files/preprocessed/ce/source.json":
                      "files/_archive/example/backup/_long_paths/original.json"}
            recovery_common.atomic(run / "backup_locations.json", ledger)
            recovery_common.atomic(root / "var/storage_paths.json", {
                "moved_roots": ["files/_archive", "files/preprocessed"], "legacy_recovery_runs": ["example"],
            })
            frozen = (run / "backup_locations.json").read_bytes()
            with patch.object(repair, "ROOT", root):
                self.assertEqual(repair.publication_backup_path(run, root / "preprocessed/ce/source.json"), saved)
            self.assertEqual((run / "backup_locations.json").read_bytes(), frozen)

    def test_unified_inspection_does_not_import_handlers_or_write_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/formats"
            recovery_common.atomic(run / "plan.json", {"policy_version": "attachment-signature-v1"})
            recovery_common.receipt(run, status="complete")
            before = {p.name: p.read_bytes() for p in run.iterdir()}
            with patch.object(recovery_cli, "ROOT", root), \
                    patch.object(recovery_cli.importlib, "import_module") as imports, redirect_stdout(io.StringIO()):
                self.assertEqual(recovery_cli.main(["--list"]), 0)
                self.assertEqual(recovery_cli.main(["--task", "formats", "--run", str(run)]), 0)
                self.assertEqual(recovery_cli.main(["--task", "formats", "--run", str(run),
                                                   "--phase", "process", "--plan"]), 0)
                fresh = root / "var/recovery/new"
                self.assertEqual(recovery_cli.main(["--task", "zip", "--run", str(fresh),
                                                   "--phase", "prepare", "--plan"]), 0)
                imports.assert_not_called()
            self.assertFalse(fresh.exists())
            self.assertEqual({p.name: p.read_bytes() for p in run.iterdir()}, before)

    def test_unified_cli_rejects_foreign_plan_before_lock_or_receipt_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/rules"
            recovery_common.atomic(run / "plan.json", {"policy": "rule-provenance-v1"})
            recovery_common.receipt(run, status="complete")
            before = (run / "receipt.json").read_bytes()
            with patch.object(recovery_cli, "ROOT", root), \
                    patch.object(recovery_cli, "recovery_lock") as lock, \
                    patch.object(recovery_cli.importlib, "import_module") as imports, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    recovery_cli.main(["--task", "formats", "--phase", "process", "--run", str(run)])
                self.assertEqual(error.exception.code, 2)
                imports.assert_not_called()
                lock.assert_not_called()
            self.assertEqual((run / "receipt.json").read_bytes(), before)
            self.assertFalse((run / "recovery_job.json").exists())

    def test_unified_cli_rejects_irrelevant_options_paths_and_reversed_phases(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(recovery_cli, "ROOT", root), \
                    patch.object(recovery_cli, "execute") as execute, redirect_stderr(io.StringIO()):
                for extra in (["--workers", "2"], ["--phase", "process", "scan"]):
                    with self.assertRaises(SystemExit):
                        recovery_cli.main(["--task", "formats", "--run", "var/recovery/new", *extra])
                for path in ("preprocessed", "var/recovery", "../outside"):
                    with self.assertRaises(SystemExit):
                        recovery_cli.main(["--task", "formats", "--run", path, "--phase", "scan", "--plan"])
                execute.assert_not_called()
            self.assertFalse((root / "files").exists())

    def test_unified_sequence_uses_new_run_and_legacy_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/formats"
            called = []

            def scan(target, extensions):
                called.append(("scan", extensions))
                recovery_common.atomic(target / "plan.json", {"policy_version": "attachment-signature-v1"})
                recovery_common.receipt(target, status="planned")

            def process(target, recover, retry):
                called.append(("process", recover, retry))
                # The unified runner holds the exact lock used by old commands.
                with self.assertRaises(RuntimeError):
                    with recovery_lock(target / ".lock"):
                        self.fail("Concurrent recovery acquired the same run")
                recovery_common.receipt(target, status="complete")

            handler = SimpleNamespace(scan=scan, process=process)
            with patch.object(recovery_cli, "ROOT", root), \
                    patch.object(recovery_cli.importlib, "import_module", return_value=handler), redirect_stdout(io.StringIO()):
                self.assertEqual(recovery_cli.main(["--task", "formats", "--run", str(run),
                    "--phase", "scan", "process", "--file-ext", "PDF", "--retry-failed"]), 0)
            self.assertEqual(called, [("scan", {".pdf"}), ("process", False, True)])
            self.assertEqual(recovery_common.read(run / "recovery_job.json")["task"], "formats")
            self.assertEqual(recovery_common.read(run / "receipt.json")["status"], "complete")

    def test_unified_sequence_stops_on_failure_and_keeps_failed_phase(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/zip"
            recovery_common.atomic(run / "plan.json", {"policy_version": 2})
            handler = SimpleNamespace()

            def apply(*args):
                raise ValueError("Original hash changed")

            handler.apply = apply
            handler.publish = lambda *args: self.fail("Publication continued after failed DB validation")
            with patch.object(recovery_cli, "ROOT", root), \
                    patch.object(recovery_cli.importlib, "import_module", return_value=handler), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "Original hash changed"):
                    recovery_cli.main(["--task", "zip", "--run", str(run), "--phase", "apply", "publish"])
            state = recovery_common.read(run / "receipt.json")
            self.assertEqual(state["failed_phase"], "apply")
            self.assertEqual(state["status"], "needs_attention")
            self.assertFalse(state["search_verified"])
            with recovery_lock(run / ".lock"):
                pass  # Failure released the shared lock.

    def test_unified_waiting_dependency_stops_sequence_with_nonzero_exit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/zip"
            recovery_common.atomic(run / "plan.json", {"policy_version": 2})
            handler = SimpleNamespace(
                apply=lambda target, *args: recovery_common.receipt(target, status="waiting_for_ingestion"),
                publish=lambda *args: self.fail("Pending dependencies must stop publication"))
            with patch.object(recovery_cli, "ROOT", root), \
                    patch.object(recovery_cli.importlib, "import_module", return_value=handler), redirect_stdout(io.StringIO()):
                self.assertEqual(recovery_cli.main(["--task", "zip", "--run", str(run),
                                                   "--phase", "apply", "publish"]), 2)
            self.assertEqual(recovery_common.read(run / "receipt.json")["status"], "waiting_for_ingestion")

    def test_unified_missing_resume_preview_validates_checkpoints_without_imports(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/missing"
            recovery_common.atomic(run / "missing_sources.json", [{"id": "selected"}])
            recovery_common.atomic(run / "load.json", {"completed": ["foreign"], "status": "loaded"})
            with patch.object(recovery_cli, "ROOT", root), \
                    patch.object(recovery_cli.importlib, "import_module") as imports, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    recovery_cli.main(["--task", "missing", "--run", str(run), "--phase", "resume", "--plan"])
                self.assertEqual(error.exception.code, 2)
                imports.assert_not_called()
            self.assertFalse((run / ".pipeline.lock").exists())
            self.assertFalse((run / "recovery_job.json").exists())

    def test_unified_legacy_entry_uses_the_same_dispatcher(self):
        with patch.object(recovery_cli, "main", return_value=0) as main:
            self.assertEqual(recovery_cli.legacy_main("zip", ["apply", "--run", "var/recovery/zip"]), 0)
            main.assert_called_once_with(["--task", "zip", "--phase", "apply", "--run", "var/recovery/zip"])

    def test_shared_receipt_retains_failure_history_after_successful_retry(self):
        with TemporaryDirectory() as directory:
            run = Path(directory)
            recovery_common.receipt(run, status="needs_attention", failed_phase="apply", error="locked")
            recovery_common.receipt(run, status="complete")
            state = recovery_common.read(run / "receipt.json")
            self.assertNotIn("failed_phase", state)
            self.assertNotIn("error", state)
            self.assertEqual(state["recovered_events"][0]["error"], "locked")

    def test_long_recovery_backups_keep_distinct_paths_and_auditable_original_links(self):
        import json
        import shutil
        from scripts.preprocessing import repair_archive_collisions as repair
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery" / ("run_" + "r" * 70)
            run.mkdir(parents=True)
            source = root / "preprocessed" / ("a" * 70) / ("b" * 35 + ".pdf.json")
            source.parent.mkdir(parents=True)
            source.write_bytes(b"original recovery bytes")
            with patch.object(repair, "ROOT", root):
                backup = repair.publication_backup_path(run, source)
                self.assertLess(len(str(backup)), 240)
                self.assertIn("_long_paths", backup.parts)
                backup.parent.mkdir(parents=True)
                shutil.copy2(source, backup)
                self.assertEqual(backup.read_bytes(), source.read_bytes())
                self.assertEqual(backup.stat().st_mtime_ns, source.stat().st_mtime_ns)
                self.assertEqual(repair.publication_backup_path(run, source), backup)
                other = repair.publication_backup_path(run, source.with_name(source.name.replace(".pdf.json", ".hwp.json")))
                self.assertNotEqual(other, backup)
                ledger = json.loads((run / "backup_locations.json").read_text(encoding="utf-8"))
                self.assertEqual(ledger[source.relative_to(root).as_posix()], backup.relative_to(root).as_posix())
                short = root / "original.json"
                self.assertEqual(repair.publication_backup_path(root / "run", short), root / "run/backup/original.json")

    def test_empty_excel_accepts_header_only_forms_but_rejects_new_data_rows(self):
        import json
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "preprocessed/form.json"
            path.parent.mkdir(parents=True)
            target = {"preprocessed": "preprocessed/form.json", "slug": "form", "source_path": "files/form.xlsx"}
            document = {"slug": "form", "source_path": "files/form.xlsx",
                        "provenance": {"spreadsheet_policy_version": "spreadsheet-rows-v1"},
                        "chunks": [{"text": "Name[A]: Name", "structured_table": {
                            "kind": "spreadsheet", "rows": [{"row_kind": "header"}]}}]}
            path.write_text(json.dumps(document), encoding="utf-8")
            with patch.object(resume_excel, "ROOT", root):
                self.assertEqual(resume_excel.validate_empty_target(target), document)
                document["chunks"][0]["structured_table"]["rows"][0]["row_kind"] = "data"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "searchable rows"):
                    resume_excel.validate_empty_target(target)
                document["chunks"] = [{"text": "x"}]
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "requires review"):
                    resume_excel.validate_empty_target(target)

    def test_recovery_publication_apply_requires_prior_jobs_before_reading_selected_data(self):
        with patch.object(publish_recovery, "require_finished", side_effect=RuntimeError("earlier ingestion loading")), \
                patch.object(publish_recovery, "checked") as checked:
            with self.assertRaisesRegex(RuntimeError, "loading"):
                publish_recovery.apply(Path("unused"), [Path("receipt.json")])
            checked.assert_not_called()

    def test_recovery_publication_preserves_a_different_database_body_or_vector(self):
        import json
        existing = {"source": {"file_path": "files/original.pdf", "status": "active",
                               "metadata": {"ingestion_dataset": "ce"}},
                    "chunks": [{"chunk_index": 0, "content": "newer database text", "embedding": json.dumps([0.5] * 384)}]}
        with TemporaryDirectory() as directory:
            vector = Path(directory) / "local.json"
            vector.write_text(json.dumps({"records": [{"chunk_index": 0, "text": "old local text", "embedding": [0.5] * 384}]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "DB differs"):
                publish_recovery.validate_existing(existing, "ce", "files/original.pdf", vector)
            vector.write_text(json.dumps({"records": [{"chunk_index": 0, "text": "newer database text", "embedding": [0.1] * 384}]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "DB differs"):
                publish_recovery.validate_existing(existing, "ce", "files/original.pdf", vector)

    def test_recovery_publication_cannot_reactivate_an_inactive_source(self):
        existing = {"source": {"file_path": "files/original.pdf", "status": "inactive",
                               "metadata": {"ingestion_dataset": "ce"}}, "chunks": []}
        with self.assertRaisesRegex(ValueError, "inactive"):
            publish_recovery.validate_existing(existing, "ce", "files/original.pdf", Path("unused"))

    def test_recovery_publication_refuses_a_same_path_with_another_source_id(self):
        conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: [("other", "files/original.pdf", "ce")]))
        with self.assertRaisesRegex(ValueError, "Another source identity"):
            publish_recovery.check_path_identity(conn, "rag", "selected", "ce", "files/original.pdf")

    def test_quality_zip_member_keeps_exact_original_and_ignores_member_directories(self):
        import zipfile
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("nested/source.xls", b"original bytes")
            item = {"raw_path": "source.zip", "raw_sha256": review_text_quality.digest(archive),
                    "source_path": "source.zip!/nested/source.xls"}
            with patch.object(review_text_quality, "ROOT", root):
                copied = review_text_quality.copy_review_original(item, root / "review")
                self.assertEqual(copied, root / "review/original/source.xls")
                self.assertEqual(copied.read_bytes(), b"original bytes")
                item["source_path"] = "source.zip!/wrong/source.xls"
                with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
                    review_text_quality.copy_review_original(item, root / "review")

    def test_text_quality_count_mismatch_preserves_reviewable_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "var/recovery/quality"
            with patch.object(review_text_quality, "ROOT", root):
                with self.assertRaisesRegex(ValueError, "expected 7 chunks/2 PDFs"):
                    review_text_quality.scan(run, 7, 2, set())
                self.assertEqual(review_text_quality.read(run / "receipt.json")["status"],
                                 "selection_review_required")
                self.assertTrue((run / "plan.json").exists())
                with self.assertRaisesRegex(ValueError, "Existing evidence retained"):
                    review_text_quality.scan(run, 0, 0, set())

    def test_search_verification_does_not_accept_partial_rpc_failure_as_success(self):
        import hashlib
        body = "A grounded source body"
        entry = {"id": "expected", "dataset": "ce", "source_path": "files/ce/guide.pdf", "title": "Guide",
                 "chunks": [[0, hashlib.sha256(body.encode()).hexdigest()]], "uri": "https://ce.pknu.ac.kr/guide"}
        row = {"source_id": "expected", "chunk_index": 0, "content": body, "title": "Guide", "url": entry["uri"],
               "metadata": {"ingestion_dataset": "ce", "source_path": entry["source_path"]}}
        hits, errors = verify_recovery_search.validate_case(entry, [row], ["lexical RPC failed: timeout"])
        self.assertEqual(len(hits), 1)
        status, _ = verify_recovery_search.verification_status([{"family": "rag", "target_hit": bool(hits), "errors": errors}], ["rag"])
        self.assertEqual(status, "failed")
        _, bad_body = verify_recovery_search.validate_case(entry, [{**row, "content": "unrelated body"}], [])
        self.assertIn("chunk_position_or_body_hash_mismatch", bad_body)

    def test_search_verification_requires_the_selected_source_not_another_inserted_source(self):
        entry = {"id": "selected", "dataset": "ce", "source_path": "files/ce/guide.pdf", "chunks": []}
        hits, errors = verify_recovery_search.validate_case(entry, [{"source_id": "another_inserted_source"}], [])
        status, _ = verify_recovery_search.verification_status([{"family": "rag", "target_hit": bool(hits), "errors": errors}], ["rag"])
        self.assertEqual(status, "failed")

    def test_search_cases_do_not_repeat_one_hashed_manual_across_departments(self):
        entries = [{"id": "a", "family": "rag", "source_path": "files/ce/manual_Reabcdef123456.pdf", "title": "Notice A"},
                   {"id": "b", "family": "rag", "source_path": "files/ee/manual.pdf", "title": "Notice B"},
                   {"id": "c", "family": "rag", "source_path": "files/ce/another.pdf", "title": "Notice C"}]
        self.assertEqual(verify_recovery_search.source_name(entries[0]), "manual")
        selected = verify_recovery_search.candidates(entries, "rag", 3)
        self.assertEqual({e["id"] for e in selected}, {"a", "c"})

    def test_checkpoint_replacement_retries_transient_windows_lock(self):
        error = PermissionError("reader holds the checkpoint")
        error.winerror = 32
        with patch("scripts.vectorization.recovery_control.os.replace", side_effect=[error, None]) as replace, \
                patch("scripts.vectorization.recovery_control.time.sleep") as sleep:
            replace_with_retry(Path("new.tmp"), Path("receipt.json"))
        self.assertEqual(replace.call_count, 2)
        sleep.assert_called_once()

    def test_checkpoint_failure_preserves_old_bytes_and_removes_only_its_temporary(self):
        from scripts.preprocessing import repair_archive_collisions
        with TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text('{"status":"loading"}', encoding="utf-8")
            error = PermissionError("persistent permission denial")
            error.winerror = 5
            with patch("scripts.vectorization.recovery_control.os.replace", side_effect=error), \
                    patch("scripts.vectorization.recovery_control.time.sleep"), \
                    self.assertRaises(PermissionError):
                repair_archive_collisions.atomic(path, {"status": "complete"})
            self.assertEqual(path.read_text(encoding="utf-8"), '{"status":"loading"}')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_checkpoint_non_lock_errors_are_not_retried(self):
        with patch("scripts.vectorization.recovery_control.os.replace", side_effect=FileNotFoundError("missing source")) as replace, \
                patch("scripts.vectorization.recovery_control.time.sleep") as sleep, self.assertRaises(FileNotFoundError):
            replace_with_retry(Path("missing.tmp"), Path("receipt.json"))
        replace.assert_called_once()
        sleep.assert_not_called()

    def test_original_replacement_character_is_evidence_not_a_guessed_correction(self):
        text = "원본에 이미 \ufffd 문자가 있습니다."
        self.assertEqual(review_text_quality.classify_original("json", text), "replacement_present_in_source_text")
        self.assertIn("\ufffd", text)
        self.assertEqual(review_text_quality.classify_original("unknown", text), "source_or_decoder_unresolved")

    def test_cleaner_ocr_requires_visual_review_and_does_not_prove_original_damage(self):
        self.assertEqual(review_text_quality.classify_original("pdf", "", native="\ufffd", ocr="수강신청 절차"),
                         "text_layer_or_ocr_problem_visual_review_required")

    def test_deactivation_allows_db_timestamp_trigger_but_no_other_source_changes(self):
        prior = {"id": "source", "status": "active", "title": "keep title", "updated_at": "before"}
        current = {**prior, "status": "inactive", "updated_at": "after"}
        reconcile_active_sources.validate_deactivation(prior, current)
        with self.assertRaises(ValueError):
            reconcile_active_sources.validate_deactivation(prior, {**current, "title": "wrong title"})

    def active_source_fixture(self, sid, *, content="original body", year="2026"):
        import hashlib
        from scripts.vectorization.load_to_supabase import DEFAULT_EMBEDDING_MODEL
        return {"source": {"id": sid, "file_path": "files/ce/original.pdf", "title": "Notice",
                           "url": "https://ce.pknu.ac.kr/notice", "file_ext": ".pdf",
                           "metadata": {"ingestion_dataset": "ce", "document_id": "notice-1", "year": year}},
                "chunks": [{"chunk_index": 0, "content": content,
                            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                            "embedding": [0.1] * 384, "embedding_model": DEFAULT_EMBEDDING_MODEL, "embedding_dim": 384}]}

    def test_exact_duplicate_prefers_indexed_source_without_using_ingestion_time(self):
        old = self.active_source_fixture("old")
        new = self.active_source_fixture("new")
        old["source"]["processed_at"] = "2099-01-01"
        new["source"]["processed_at"] = "2000-01-01"
        self.assertEqual(reconcile_active_sources.choose_representative([old, new], {"new"}), "new")

    def test_same_original_path_with_different_body_or_year_is_preserved(self):
        first = self.active_source_fixture("one")
        for second in (self.active_source_fixture("two", content="unique additional body"),
                       self.active_source_fixture("two", year="2025")):
            self.assertIsNone(reconcile_active_sources.choose_representative([first, second], {"one"}))

    def test_equal_paths_without_document_identity_do_not_prove_duplication(self):
        value = self.active_source_fixture("one")
        value["source"]["metadata"].pop("document_id")
        self.assertIsNone(reconcile_active_sources.duplicate_signature(value))

    def test_duplicate_with_corrupt_hash_or_embedding_is_not_auto_selected(self):
        for field, bad in (("content_hash", "wrong"), ("embedding", [float("nan")] * 384)):
            value = self.active_source_fixture("one")
            value["chunks"][0][field] = bad
            self.assertIsNone(reconcile_active_sources.duplicate_signature(value))

    def test_excel_resume_rejects_an_existing_source_from_another_dataset_or_path(self):
        entry = {"dataset": "ce", "source_path": "files/ce/selected.xlsx"}
        for existing in ({"file_path": "files/ce/other.xlsx", "metadata": {"ingestion_dataset": "ce"}},
                         {"file_path": entry["source_path"], "metadata": {"ingestion_dataset": "other"}}):
            with self.subTest(existing=existing), self.assertRaisesRegex(ValueError, "another dataset/path"):
                resume_excel.validate_scope(entry, existing)
        resume_excel.validate_scope(entry, {"file_path": entry["source_path"], "metadata": {"ingestion_dataset": "ce"}})
        resume_excel.validate_scope(entry, None)

    def test_excel_resume_body_signature_detects_reordering_and_changes(self):
        original = [(0, "a" * 64), (2, "b" * 64)]
        self.assertNotEqual(resume_excel.sequence_hash(original), resume_excel.sequence_hash(list(reversed(original))))
        self.assertNotEqual(resume_excel.sequence_hash(original), resume_excel.sequence_hash([(0, "a" * 64), (2, "c" * 64)]))

    def test_excel_checkpoint_rejects_unknown_duplicate_or_inconsistent_counts(self):
        entry = {"id": "one", "dataset": "ce", "slug": "original", "family": "rag", "chunks": 3}
        inventory = {entry["id"]: entry}
        good = {"completed_sources": [entry], "chunks_loaded": 3}
        self.assertEqual(resume_excel.checkpoint_sources(good, inventory), {"one"})
        for progress in (
            {**good, "completed_sources": [entry, entry]},
            {**good, "completed_sources": [{**entry, "id": "unselected"}]},
            {**good, "completed_sources": [{**entry, "dataset": "other"}]},
            {**good, "completed_sources": [{**entry, "chunks": 4}]},
            {**good, "chunks_loaded": 2},
            {**good, "completed_sources": [None]},
        ):
            with self.subTest(progress=progress), self.assertRaises(ValueError):
                resume_excel.checkpoint_sources(progress, inventory)

    def test_excel_probe_uses_same_row_instructor_year_and_term(self):
        request = SimpleNamespace(instructor="유승호", year=2026, term="2")
        fields = {"staff_name": "유승호", "year": "2026", "term": "2", "course_name": "소프트웨어공학"}
        document = {"chunks": [{"structured_table": {"rows": [
            {"fields": fields}, {"fields": fields},
            {"fields": {**fields, "staff_name": "다른 교수", "course_name": "다른 강좌"}},
            {"fields": {**fields, "year": "2025", "course_name": "과거 강좌"}},
            {"fields": {**fields, "term": "1", "course_name": "다른 학기"}},
            {"fields": {}},
        ]}}]}
        self.assertEqual(resume_excel.expected_courses(document, request), {"소프트웨어공학"})
        with self.assertRaisesRegex(ValueError, "no matching"):
            resume_excel.expected_courses({"chunks": []}, request)

    def test_excel_incomplete_load_cannot_finalize_or_touch_database(self):
        entry = {"id": "one", "dataset": "ce", "slug": "original", "family": "rag", "chunks": 3}
        for progress in (
            {"status": "loading", "completed_sources": [entry], "chunks_loaded": 3},
            {"status": "complete", "completed_sources": [], "chunks_loaded": 0},
        ):
            with patch.object(resume_excel, "read", side_effect=[{"entries": [entry]}, progress]), \
                    patch("scripts.db.connect_postgres") as connect, patch.object(resume_excel, "receipt") as receipt:
                with self.assertRaisesRegex(ValueError, "Finish/resume"):
                    resume_excel.finalize(Path("unused"), Path("unused-backend"), "question", None)
                connect.assert_not_called()
                receipt.assert_not_called()

    def test_ce_processing_order_handles_local_kst_without_claiming_document_freshness(self):
        timeline = reconcile_ce.processing_timeline("2026-11-08T09:00:00", "2026-11-08T10:00:00",
            "2099-01-01T00:00:00Z", ["2026-11-08T01:30:00Z"])
        self.assertEqual(timeline["processing_order"], "db_embedding_later")
        self.assertEqual(timeline["document_revision_order"], "unconfirmed")
        unknown = reconcile_ce.processing_timeline(None, "invalid", None, [None])
        self.assertEqual(unknown["processing_order"], "unknown")

    def test_ce_chunk_evidence_exposes_duplicate_bodies_without_deduplicating_content(self):
        evidence = reconcile_ce.chunk_evidence(["original row", "original row"])
        self.assertEqual(evidence["duplicate_chunk_bodies"], 1)
        self.assertEqual(evidence["total_chunk_chars_including_overlap"], 24)
        self.assertNotEqual(evidence["body_sha256"], reconcile_ce.chunk_evidence(["original row"])["body_sha256"])

    def test_ce_edited_comparison_cannot_be_used_to_skip_original_review(self):
        with TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "comparisons.json").write_text("[]", encoding="utf-8")
            (run / "receipt.json").write_text('{"comparisons_sha256":"changed"}', encoding="utf-8")
            with patch.object(reconcile_ce, "load_plan", return_value={"items": []}):
                with self.assertRaisesRegex(ValueError, "Comparison evidence changed"):
                    reconcile_ce.stage(run)

    def test_ce_reconciliation_never_selects_local_by_timestamp(self):
        local = {"valid": True, "faithful": True, "processed_at": "2099-01-01"}
        database = {"valid": True, "faithful": False, "processed_at": "2000-01-01"}
        self.assertEqual(reconcile_ce.recommend(local, database), "review")
        self.assertEqual(reconcile_ce.recommend(local, {**database, "faithful": True}), "database")

    def test_ce_partial_text_and_repeated_rows_are_not_complete_original_coverage(self):
        text = "original row with useful evidence " * 5
        partial = reconcile_ce.coverage(text, ["original row with useful evidence"] * 5)
        self.assertFalse(partial["exact_full_coverage"])
        self.assertTrue(reconcile_ce.coverage(text, [text])["exact_full_coverage"])

    def test_ce_local_replacement_requires_original_review_and_matching_hash(self):
        versions = {"local": {"valid": True, "faithful": True}, "database": {"valid": True, "faithful": False}}
        decision = {"winner": "local", "raw_sha256": "raw"}
        with self.assertRaisesRegex(ValueError, "original_reviewed"):
            reconcile_ce.approve(decision, raw_hash="raw", **versions)
        reviewed = {**decision, "original_reviewed": True, "reason": "Read original pages; DB OCR dropped the table"}
        self.assertEqual(reconcile_ce.approve(reviewed, raw_hash="raw", **versions), "local")
        with self.assertRaisesRegex(ValueError, "current original"):
            reconcile_ce.approve(reviewed, raw_hash="changed", **versions)

    def test_ce_invalid_db_embedding_cannot_be_selected_even_with_review(self):
        decision = {"winner": "database", "raw_sha256": "raw", "original_reviewed": True, "reason": "review"}
        with self.assertRaisesRegex(ValueError, "invalid"):
            reconcile_ce.approve(decision, raw_hash="raw", local={"valid": True}, database={"valid": False})

    def test_ce_verified_db_can_be_preserved_without_replacing_it(self):
        decision = {"winner": "database", "raw_sha256": "raw"}
        self.assertEqual(reconcile_ce.approve(decision, raw_hash="raw", local={"valid": False},
                                             database={"valid": True, "faithful": True}), "database")

    def test_ce_pgvector_float32_roundtrip_is_compared_at_storage_precision(self):
        import struct
        stored = struct.unpack("<f", struct.pack("<f", 0.12345678))[0]
        self.assertEqual(reconcile_ce.vector_key([stored]), reconcile_ce.vector_key([0.12345678]))
        self.assertNotEqual(reconcile_ce.vector_key([stored]), reconcile_ce.vector_key([0.9]))

    def test_ce_changed_database_aborts_before_any_chunk_delete(self):
        @contextmanager
        def connection(**kwargs):
            class Cursor:
                def execute(self, query, params=None):
                    statements.append(query)
            class Connection:
                @contextmanager
                def cursor(self):
                    yield Cursor()
            yield Connection()
        statements = []
        plan = {"items": [{"id": "selected", "db_fingerprint": reconcile_ce.fingerprint({"old": True})}]}
        with patch("scripts.preprocessing.repair_archive_collisions.require_finished"), \
                patch("scripts.db.connect_postgres", connection), \
                patch.object(reconcile_ce, "checked", return_value=(plan, [])), \
                patch.object(reconcile_ce, "check_local_selection"), \
                patch.object(reconcile_ce, "db_snapshot", return_value={"newer": True}):
            with self.assertRaisesRegex(ValueError, "DB changed"):
                reconcile_ce.apply(Path("unused"), [])
        self.assertFalse(any("delete" in statement.lower() for statement in statements))

    def test_ce_database_winner_never_rewrites_database_chunks(self):
        statements = []
        @contextmanager
        def connection(**kwargs):
            class Cursor:
                def execute(self, query, params=None):
                    statements.append(query)
            class Connection:
                @contextmanager
                def cursor(self):
                    yield Cursor()
            yield Connection()
        unchanged = {"source": {"processed_at": "2026-11-08"}, "chunks": ["newer DB body"]}
        plan = {"items": [{"id": "selected", "db_fingerprint": reconcile_ce.fingerprint(unchanged),
                           "target": "preprocessed/ce/example.json", "target_sha256": "frozen"}]}
        docs = [{"id": "selected", "winner": "database"}]
        with patch("scripts.preprocessing.repair_archive_collisions.require_finished"), \
                patch("scripts.db.connect_postgres", connection), \
                patch.object(reconcile_ce, "checked", return_value=(plan, docs)), \
                patch.object(reconcile_ce, "check_local_selection"), \
                patch.object(reconcile_ce, "db_snapshot", return_value=unchanged), \
                patch.object(reconcile_ce, "digest", return_value="frozen"), \
                patch.object(reconcile_ce, "receipt"), \
                patch.object(reconcile_ce, "verify_database"), \
                patch("scripts.vectorization.load_to_supabase.copy_upsert") as upsert:
            reconcile_ce.apply(Path("unused"), [])
        upsert.assert_not_called()
        self.assertFalse(any("delete" in statement.lower() for statement in statements))

    def test_checkpoint_rejects_duplicate_and_unselected_ids(self):
        for progress in (
            {'completed':['a','a']},
            {'completed':['a'],'concurrently_present':['a']},
            {'completed':['unknown']},
        ):
            with self.subTest(progress=progress), self.assertRaises(ValueError):
                checkpoint_ids(progress,{'a','b'})

    def test_partial_load_is_resumed_before_verification(self):
        phases=resume_plan({'status':'needs_attention','failed_phase':'load'},
                           {'status':'loading','completed':['a']},{'a','b'})
        self.assertEqual(phases,['load','priorities','verify','search'])

    def test_completed_load_is_not_repeated_after_search_failure(self):
        state={'status':'needs_attention','failed_phase':'search','priorities_updated':2}
        progress={'status':'loaded','completed':['a','b']}
        self.assertEqual(resume_plan(state,progress,{'a','b'}),['verify','search'])

    def test_incomplete_loaded_checkpoint_is_not_trusted(self):
        with self.assertRaises(ValueError):
            resume_plan({}, {'status':'loaded','completed':['a']}, {'a','b'})

    def test_complete_requires_database_and_search_verification(self):
        progress={'status':'loaded','completed':['a']}
        with self.assertRaises(ValueError):
            resume_plan({'status':'complete'},progress,{'a'})
        state={'status':'complete','verification':{'rag':'passed'},
               'search_verification':{'status':'passed'}}
        self.assertEqual(resume_plan(state,progress,{'a'}),[])

    def test_load_options_reject_invalid_limits(self):
        for values in ({'workers':0},{'workers':5},{'chunk_batch_size':0},
                       {'statement_timeout_seconds':-1},{'max_batch_retries':-1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                LoadOptions(**values)

    def test_lock_blocks_duplicate_run_and_releases_after_failure(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'recovery.lock'
            with self.assertRaisesRegex(ValueError,'operator stopped'):
                with recovery_lock(path):
                    with self.assertRaisesRegex(RuntimeError,'already running'):
                        with recovery_lock(path):
                            self.fail('Duplicate pipeline acquired the lock')
                    raise ValueError('operator stopped')
            with recovery_lock(path):
                pass

    def test_retry_rolls_back_partial_statement_without_duplicate_chunks(self):
        rows=[]
        attempts=[]
        retried=[]
        checkpoints=[]

        @contextmanager
        def transaction():
            before=rows[:]
            try:
                yield
            except BaseException:
                rows[:]=before
                raise

        def write(part):
            attempts.append(part[:])
            if len(attempts)==1:
                rows.extend(part[:2])
                raise TransientFailure('statement timed out after partial work')
            rows.extend(part)

        with transaction():
            write_chunk_batches([1,2,3,4,5,6],batch_size=4,max_retries=2,
                savepoint=transaction,write=write,retryable=(TransientFailure,),
                on_retry=lambda error,attempt,size:retried.append((attempt,size)))
        checkpoints.append('source_committed')
        self.assertEqual(rows,[1,2,3,4,5,6])
        self.assertEqual(attempts,[[1,2,3,4],[1,2],[3,4],[5,6]])
        self.assertEqual(retried,[(1,2)])
        self.assertEqual(checkpoints,['source_committed'])

    def test_exhausted_retry_rolls_back_whole_source_before_checkpoint(self):
        rows=[]
        checkpoints=[]

        @contextmanager
        def transaction():
            before=rows[:]
            try:
                yield
            except BaseException:
                rows[:]=before
                raise

        def write(part):
            rows.extend(part)
            if 4 in part:
                raise TransientFailure('persistent failure')

        with self.assertRaises(TransientFailure):
            with transaction():
                write_chunk_batches([1,2,3,4],batch_size=2,max_retries=1,
                    savepoint=transaction,write=write,retryable=(TransientFailure,),
                    on_retry=lambda *args:None)
            checkpoints.append('source_committed')
        self.assertEqual(rows,[])
        self.assertEqual(checkpoints,[])

    def test_non_retryable_errors_are_not_hidden(self):
        @contextmanager
        def savepoint():
            yield

        def write(part):
            raise ValueError('invalid data')

        def no_retry(*args):
            self.fail('Non-retryable error was retried')

        with self.assertRaisesRegex(ValueError,'invalid data'):
            write_chunk_batches([1],batch_size=1,max_retries=2,savepoint=savepoint,
                write=write,retryable=(TransientFailure,),on_retry=no_retry)


if __name__=='__main__':
    unittest.main()
