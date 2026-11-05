import unittest
from pathlib import Path

from scripts.preprocessing.body_image_layout import enrich_entry, review_template, validate_review
from scripts.preprocessing.body_image_review import compile_body_image_blocks
from scripts.preprocessing.classify_body_image_layout import classify
from scripts.preprocessing.generate_body_image_layout_labels import balanced_sample, summary
from scripts.vectorization.vectorization import extract_chunk_records


class BodyImageLayoutTests(unittest.TestCase):
    def test_legacy_paragraphs_are_wrapped_as_unknown(self):
        entry = {"source_path": "files/ce/output/json/a.json", "images": [{
            "sha256": "abc", "status": "needs_review", "warnings": ["OCR_REVIEW_REQUIRED"],
            "blocks": [{"type": "ocr_paragraph", "text": "본문"}],
        }]}
        self.assertTrue(enrich_entry(entry))
        image = entry["images"][0]
        self.assertEqual(image["layout_type"], "unknown")
        self.assertEqual(image["extractor"], "paragraph_ocr")
        self.assertEqual(image["result"]["paragraphs"][0]["text"], "본문")

    def test_table_blocks_are_preserved(self):
        entry = {"source_path": "files/ce/output/json/a.json", "images": [{
            "sha256": "abc", "status": "needs_review", "warnings": [],
            "blocks": [{"type": "ocr_table", "rows": []}],
        }]}
        enrich_entry(entry)
        image = entry["images"][0]
        self.assertEqual(image["layout_type"], "table")
        self.assertEqual(image["result"]["tables"][0]["type"], "ocr_table")

    def test_review_template_is_keyed_by_source_and_hash(self):
        entry = {"source_path": "files/ce/output/json/a.json", "images": [{"sha256": "abc"}]}
        review = review_template(entry)
        validate_review(review)
        self.assertEqual(review["images"][0]["review_key"], "files/ce/output/json/a.json#abc")
        self.assertEqual(review["rag_inclusion_policy"]["corrected"], "include")
        self.assertEqual(review["rag_inclusion_policy"]["needs_review"], "exclude")

    def test_corrected_review_requires_structured_correction(self):
        entry = {"source_path": "files/ce/output/json/a.json", "images": [{"sha256": "abc"}]}
        review = review_template(entry)
        review["images"][0]["decision"] = "corrected"
        with self.assertRaisesRegex(ValueError, "correction"):
            validate_review(review)
        review["images"][0]["correction"]["tables"].append({
            "table_index": 0,
            "cells": [{"row": 0, "column": 0, "text": "교정 셀"}],
        })
        validate_review(review)

    def test_corrected_text_is_an_allowed_correction_overlay(self):
        entry = {"source_path": "files/ce/output/json/a.json", "images": [{"sha256": "abc"}]}
        review = review_template(entry)
        review["images"][0]["decision"] = "corrected"
        review["images"][0]["correction"]["corrected_text"] = "교정된 이미지 본문"
        validate_review(review)

    def test_corrected_text_can_be_stored_directly_on_review_item(self):
        entry = {"source_path": "files/ce/output/json/a.json", "images": [{"sha256": "abc"}]}
        review = review_template(entry)
        review["images"][0]["decision"] = "corrected"
        review["images"][0]["corrected_text"] = "교정된 이미지 본문"
        validate_review(review)

    def test_needs_review_is_valid_and_excluded_by_policy(self):
        entry = {"source_path": "files/ce/output/json/a.json", "images": [{"sha256": "abc"}]}
        review = review_template(entry)
        review["images"][0]["decision"] = "needs_review"
        validate_review(review)
        self.assertEqual(review["rag_inclusion_policy"]["needs_review"], "exclude")


class BodyImageLayoutClassifierTests(unittest.TestCase):
    def test_existing_table_is_high_confidence_table(self):
        result = classify({"result": {"paragraphs": [], "tables": [{"type": "ocr_table"}]}, "warnings": []}, {
            "dark_ratio": .1, "horizontal_line_groups": 2, "vertical_line_groups": 2,
            "connector_horizontal_line_rows": 0, "connector_vertical_line_rows": 0,
            "wide_horizontal_line_rows": 0, "wide_vertical_line_rows": 0, "closed_grid": True,
        })
        self.assertEqual(result["layout_type"], "table")

    def test_empty_ocr_is_no_text(self):
        result = classify({"status": "empty", "blocks": [], "warnings": []}, {
            "dark_ratio": .01, "horizontal_line_groups": 0, "vertical_line_groups": 0,
            "connector_horizontal_line_rows": 0, "connector_vertical_line_rows": 0,
            "wide_horizontal_line_rows": 0, "wide_vertical_line_rows": 0, "closed_grid": False,
        })
        self.assertEqual(result["layout_type"], "no_text")

    def test_empty_ocr_with_visual_ink_stays_unknown(self):
        result = classify({"status": "empty", "blocks": [], "warnings": []}, {
            "dark_ratio": .12, "horizontal_line_groups": 0, "vertical_line_groups": 0,
            "connector_horizontal_line_rows": 0, "connector_vertical_line_rows": 0,
            "wide_horizontal_line_rows": 0, "wide_vertical_line_rows": 0, "closed_grid": False,
        })
        self.assertEqual(result["layout_type"], "unknown")

    def test_ambiguous_input_stays_unknown(self):
        result = classify({"blocks": [], "warnings": []}, {
            "dark_ratio": .1, "horizontal_line_groups": 0, "vertical_line_groups": 0,
            "connector_horizontal_line_rows": 0, "connector_vertical_line_rows": 0,
            "wide_horizontal_line_rows": 0, "wide_vertical_line_rows": 0, "closed_grid": False,
        })
        self.assertEqual(result["layout_type"], "unknown")

    def test_grid_like_image_without_verified_cells_is_not_guessed_as_diagram(self):
        image = {"blocks": [{"type": "ocr_paragraph", "text": "x", "bbox": [i * 10, i * 10, i * 10 + 5, i * 10 + 5]}
                            for i in range(4)], "width": 100, "height": 100, "warnings": []}
        result = classify(image, {"dark_ratio": .1, "horizontal_line_groups": 6, "vertical_line_groups": 6,
                                  "connector_horizontal_line_rows": 8, "connector_vertical_line_rows": 8,
                                  "wide_horizontal_line_rows": 8, "wide_vertical_line_rows": 8, "closed_grid": False})
        self.assertEqual(result["layout_type"], "unknown")
        self.assertIn("GRID_LIKE_LAYOUT_REQUIRES_REVIEW", result["classification_evidence"])


def candidate(dataset, digest, *, size="small", confidence="high", warning="none", image_only="no", suggestion="unknown"):
    return {
        "source_path": f"files/{dataset}/output/json/{digest}.json", "image_sha256": digest,
        "dataset": dataset, "suggested_layout_type": suggestion,
        "image_only_requires_ocr": image_only == "yes",
        "sampling_stratum": {"size": size, "confidence": confidence, "warnings": warning, "image_only": image_only},
    }


class BodyImageLayoutLabelsTests(unittest.TestCase):
    def test_sample_is_deterministic_and_deduplicated(self):
        items = [candidate("a", "1"), candidate("b", "2", size="large"), candidate("a", "1", confidence="low")]
        selected = balanced_sample(items, 3)
        self.assertEqual({item["image_sha256"] for item in selected}, {"1", "2"})

    def test_summary_has_required_dimensions(self):
        result = summary([candidate("a", "1", image_only="yes", suggestion="diagram")])
        self.assertEqual(result["dataset"], {"a": 1})
        self.assertEqual(result["suggested_layout_type"], {"diagram": 1})


class BodyImageReviewRagTests(unittest.TestCase):
    def setUp(self):
        self.source_path = "files/ce/output/json/category/post.json"

    def test_missing_review_preserves_only_original_document_blocks(self):
        images = [{
            "sha256": "a" * 64,
            "status": "extracted",
            "layout_type": "prose",
            "extractor": "paragraph_ocr",
            "result": {"paragraphs": [{"text": "must not auto-merge"}], "tables": []},
        }]
        base = [{"type": "body", "text": "original body"}]
        blocks, provenance, warnings = compile_body_image_blocks(
            self.source_path, images, [], base
        )
        self.assertEqual(blocks, base)
        self.assertFalse(warnings)
        self.assertEqual(provenance[0]["review_decision"], None)
        self.assertFalse(provenance[0]["included"])
        self.assertEqual(provenance[0]["exclusion_reason"], "NO_REVIEW_FILE_OR_IMAGE_ENTRY")

    def test_decision_priority_and_corrected_text(self):
        decisions = ["accepted", "corrected", "excluded", "reprocess", "needs_review"]
        images = [{
            "sha256": str(index) * 64,
            "status": "needs_review",
            "layout_type": "prose",
            "extractor": "paragraph_ocr",
            "result": {"paragraphs": [{"text": f"ocr-{decision}"}], "tables": []},
        } for index, decision in enumerate(decisions, start=1)]
        reviews = [{
            "source_path": self.source_path,
            "image_sha256": str(index) * 64,
            "decision": decision,
            "corrected_text": "reviewed correction" if decision == "corrected" else None,
        } for index, decision in enumerate(decisions, start=1)]
        blocks, provenance, warnings = compile_body_image_blocks(
            self.source_path, images, reviews,
            [{"type": "body", "text": "original body"}]
        )
        added = [block["text"] for block in blocks[1:]]
        self.assertEqual(added, ["ocr-accepted", "reviewed correction"])
        self.assertEqual([item["included"] for item in provenance], [True, True, False, False, False])
        self.assertEqual([item["review_decision"] for item in provenance], decisions)
        self.assertFalse(warnings)

    def test_table_structure_is_serialized_with_cell_positions_and_spans(self):
        images = [{
            "sha256": "f" * 64,
            "status": "extracted",
            "layout_type": "table",
            "extractor": "table_ocr",
            "result": {"paragraphs": [], "tables": [{"rows": [{"cells": [
                {"row": 0, "column": 0, "rowspan": 2, "colspan": 1, "text": "학년"},
                {"row": 0, "column": 1, "rowspan": 1, "colspan": 2, "text": "과목"},
            ]}]}]},
        }]
        reviews = [{
            "source_path": self.source_path,
            "image_sha256": "f" * 64,
            "decision": "accepted",
        }]
        blocks, _, _ = compile_body_image_blocks(
            self.source_path, images, reviews, []
        )
        self.assertIn("rowspan=2", blocks[0]["text"])
        self.assertIn("colspan=2", blocks[0]["text"])

    def test_corrected_structure_replaces_ocr_result(self):
        digest = "e" * 64
        images = [{
            "sha256": digest,
            "layout_type": "diagram",
            "extractor": "diagram_ocr",
            "result": {"paragraphs": [{"text": "uncorrected OCR"}], "tables": []},
        }]
        reviews = [{
            "source_path": self.source_path,
            "image_sha256": digest,
            "decision": "corrected",
            "corrected_structure": {"diagram": {"nodes": [
                {"id": "n1", "text": "교정한 노드"},
            ], "edges": [], "unverified_edges": []}},
        }]
        blocks, _, _ = compile_body_image_blocks(self.source_path, images, reviews, [])
        self.assertEqual(len(blocks), 1)
        self.assertIn("교정한 노드", blocks[0]["text"])

    def test_image_provenance_reaches_vectorized_chunk_metadata(self):
        image_provenance = [{
            "source_path": self.source_path,
            "image_sha256": "b" * 64,
            "review_decision": "accepted",
            "extractor": "paragraph_ocr",
            "layout_type": "prose",
            "included": True,
        }]
        document = {
            "slug": "post",
            "source_file": "post.json",
            "provenance": {"doc_title": "Reviewed document"},
            "body_image_provenance": image_provenance,
            "chunks": [{"chunk_id": 1, "text": "Reviewed image text has enough distinct words for retrieval."}],
        }
        records = extract_chunk_records(
            document, Path("files/ce/preprocessed/json/category/post.json"), Path.cwd()
        )
        self.assertEqual(records[0]["metadata"]["body_image_provenance"], image_provenance)


if __name__ == "__main__":
    unittest.main()
