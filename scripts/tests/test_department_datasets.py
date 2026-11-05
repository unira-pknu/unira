"""Every registry department can be preprocessed, vectorized and loaded on its own."""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.crawlers.common.storage import department_datasets, get_dataset_paths, get_dataset_root
from scripts.vectorization import load_to_supabase, vectorization
from scripts.preprocessing import preprocessing_pipeline
from scripts.preprocessing import collected
from scripts.preprocessing.storage import get_preprocessed_root

ROOT = preprocessing_pipeline.PROJECT_ROOT


class DepartmentDatasetTests(unittest.TestCase):
    def test_force_reprocesses_current_files_and_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "sample.txt").write_text("content", encoding="utf-8")
            entries = [("ce", source, root / "preprocessed/ce"),
                       ("rule", source, root / "preprocessed/rule")]
            with patch.object(collected, "scopes", return_value=entries), patch.object(collected, "run_batch") as batch, patch.object(collected, "run_rule_dataset") as rule:
                collected.run(root, dry_run=True, pdf_ocr="auto", force=True)
            self.assertFalse(batch.call_args.kwargs["changed_only"])
            self.assertFalse(rule.call_args.kwargs["changed_only"])

    def test_parallel_workers_cover_each_dataset_once(self):
        scopes = [(name, Path(name), Path("preprocessed") / name)
                  for name in ("ce", "me", "pknu_main", "pknu_syllabus", "rule")]
        with patch.object(collected, "scopes", return_value=scopes):
            assignments = [scope[0] for index in range(4)
                           for scope in collected.worker_scopes(ROOT, 4, index)]
        self.assertCountEqual([scope[0] for scope in scopes], assignments)
        self.assertEqual(len(assignments), len(set(assignments)))

    def test_preprocessing_outputs_and_vectorization_inputs_use_central_storage(self):
        from scripts.crawlers.common.reader import infer_dataset_from_path
        for name, dataset in preprocessing_pipeline.WEB_DATASETS.items():
            expected = ROOT / "files" / "preprocessed" / name
            self.assertEqual(expected, dataset.preprocessed_root)
            self.assertEqual(expected, vectorization.DATASET_PATHS[name]["input_root"])
            self.assertEqual(name, infer_dataset_from_path(expected / "json/sample.json"))
        for name, _, output in collected.scopes(ROOT):
            self.assertEqual(ROOT / "files/preprocessed" / name, output)
            self.assertEqual(output, vectorization.DATASET_PATHS[name]["input_root"])
            if name in load_to_supabase.DATASET_TABLES:
                self.assertEqual(vectorization.DATASET_PATHS[name]["output_root"] / "index.jsonl",
                                 load_to_supabase.DATASET_TABLES[name]["index"])
        for name in ("..", "../outside", "C:\\outside"):
            with self.assertRaises(ValueError):
                get_preprocessed_root(ROOT, name)

    def test_collected_scan_excludes_derived_and_operational_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("2026/1/course.pdf", "_state/catalog.json",
                         "preprocessed/course.json", "_derived/all/output.json",
                         "_incoming/incomplete.pdf"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"sample")
            self.assertEqual([root / "2026/1/course.pdf"], list(collected.source_files(root)))

    def test_collected_keeps_attachment_metadata_join_and_output_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "output"
            attachment = source / "files/category/sample.pdf"
            document = source / "json/category/sample.json"
            for path in (attachment, document):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"type":"post","url":"https://example.org"}', encoding="utf-8")
            with patch.object(collected, "scopes", return_value=[("ce", source, root / "preprocessed")]), patch.object(collected, "run_batch") as batch:
                collected.run(root, dry_run=True, pdf_ocr="never")
            calls = {call.kwargs["layout"]: call.kwargs for call in batch.call_args_list}
            self.assertEqual(source / "json", calls["by_ext"]["output_json_root"])
            self.assertEqual(root / "preprocessed/files", calls["by_ext"]["output_root"])
            self.assertEqual(root / "preprocessed/json", calls["flat"]["output_root"])
            self.assertEqual([attachment], calls["by_ext"]["target_files"])

    def test_ce_paths_and_tables_are_unchanged(self):
        web = preprocessing_pipeline.WEB_DATASETS["ce"]
        self.assertEqual(get_dataset_paths(ROOT, "ce").output, web.output_root)
        self.assertEqual(get_preprocessed_root(ROOT, "ce"), web.preprocessed_root)
        self.assertEqual(ROOT / "files/vectorized/ce", vectorization.DATASET_PATHS["ce"]["output_root"])
        for dataset, paths in vectorization.DATASET_PATHS.items():
            self.assertEqual(ROOT / "files/vectorized" / dataset, paths["output_root"])
        self.assertEqual("rag_sources", load_to_supabase.DATASET_TABLES["ce"]["sources"])

    def test_every_department_maps_to_the_department_tables(self):
        for name in department_datasets():
            with self.subTest(dataset=name):
                self.assertIn(name, preprocessing_pipeline.WEB_DATASETS)
                self.assertIn(name, vectorization.DATASET_PATHS)
                tables = load_to_supabase.DATASET_TABLES[name]
                self.assertEqual(("rag_sources", "rag_chunks"), (tables["sources"], tables["chunks"]))
                self.assertIn(name, preprocessing_pipeline.DATASET_CHOICES)

    def test_pilot_departments_use_their_registry_folders(self):
        me = preprocessing_pipeline.WEB_DATASETS["me"].output_root.relative_to(ROOT)
        self.assertEqual(("files", "department", "공과대학", "기계공학부", "기계학전공", "me", "output"), me.parts)

    def test_daily_all_is_still_the_four_datasets(self):
        self.assertEqual(["ce", "pknu_notice", "pknu_student_life", "rule"], preprocessing_pipeline.selected_datasets("all"))


if __name__ == "__main__":
    unittest.main()
