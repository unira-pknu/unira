"""Archive a completed, source-aware full evaluation without calling any API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .eval_ask import _read_jsonl
from .provenance import backend_configurations, file_sha256, timestamp


def _result_rows(path: Path) -> list[dict]:
    # str.splitlines() also splits U+0085/U+2028/U+2029 inside valid JSON
    # strings. JSONL records are delimited by physical newlines only.
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def build_baseline(questions_path: Path, results_path: Path, graded_path: Path) -> dict:
    questions = _read_jsonl(questions_path)
    results = _result_rows(results_path)
    graded = _result_rows(graded_path)
    summary_path = graded_path.with_suffix(".summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    provenance = summary.get("provenance") or {}
    manifest = provenance.get("run_manifest") or {}
    ids = [row["id"] for row in questions]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("baseline requires a non-empty question set with unique IDs")
    if any(row.get("label_status", "ready") != "ready" for row in questions):
        raise ValueError("baseline requires reviewed ready labels")
    if [row.get("id") for row in results] != ids or [row.get("id") for row in graded] != ids:
        raise ValueError("baseline requires all questions exactly once, in their original order")
    if manifest.get("questions_sha256") != file_sha256(questions_path) or manifest.get("expected_case_ids") != ids:
        raise ValueError("question set differs from the captured run manifest")
    if not manifest.get("finished_at") or manifest.get("selected_case_count") != manifest.get("available_case_count"):
        raise ValueError("run was interrupted or limited; a full run is required")
    if provenance.get("schema_version") != 2 or not provenance.get("judge_prompt_sha256"):
        raise ValueError("source-aware grading provenance is required")
    if manifest.get("results_sha256") != file_sha256(results_path) or provenance.get("results_sha256") != file_sha256(results_path):
        raise ValueError("results were modified after execution/grading")
    if provenance.get("graded_sha256") != file_sha256(graded_path):
        raise ValueError("graded output was modified after grading")
    if not manifest.get("data_snapshot") or not manifest.get("backend_revision"):
        raise ValueError("rerun eval_ask with --data-snapshot and --backend-revision identifying the evaluated backend")
    configurations = backend_configurations(results)
    if len(configurations) > 1:
        raise ValueError("backend settings/prompt/revision changed during the run")
    if configurations and configurations[0].get("backend_revision") not in {None, manifest["backend_revision"]}:
        raise ValueError("operator-supplied backend revision differs from the captured revision")
    if summary.get("total_case_count") != len(ids):
        raise ValueError("summary does not cover the full question set")
    incomplete = []
    for result, row in zip(results, graded):
        original = {key: value for key, value in row.items() if key not in {"judged", "skipped"}}
        if original != result:
            raise ValueError(f"graded evidence/answer differs from raw result: {row['id']}")
        if result.get("error"):
            continue  # API failures remain measured failures in strict accuracy.
        evidence = row.get("evaluation_evidence") or {}
        if evidence.get("schema_version") != 1 or evidence.get("body_scope") != "retrieved_chunks":
            raise ValueError(f"missing full evidence capture: {row['id']}")
        if row.get("skipped") or not row.get("judged") or row["judged"].get("judge_error"):
            incomplete.append(row["id"])
    if incomplete:
        preview = ", ".join(incomplete[:5])
        raise ValueError(f"incomplete grading: {len(incomplete)} case(s), including {preview}; regrade the existing raw results")
    return {
        "schema_version": 1, "status": "measured", "recorded_at": timestamp(),
        "questions": {"path": str(questions_path), "sha256": file_sha256(questions_path), "count": len(ids)},
        "backend_configurations": configurations,
        "artifacts": {"results": str(results_path), "graded": str(graded_path), "summary": str(summary_path),
                      "summary_sha256": file_sha256(summary_path)},
        "summary": summary,
        "limitations": [
            "LLM judge estimates require human review of errors and a sample of passing cases.",
            "Evidence covers captured retrieved chunks, not entire original documents or live database recall.",
            "Unknown judgments are excluded from assessed rates; review coverage and strict rates together.",
            "Data snapshot and backend revision are operator-supplied identifiers, not independent DB verification.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=Path("eval/cases/regression.jsonl"))
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--graded", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = build_baseline(args.questions, args.results, args.graded)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Baselines are immutable by default: choose a fresh run name to compare runs.
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(baseline, output, ensure_ascii=False, indent=2)
    print(f"saved baseline={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
