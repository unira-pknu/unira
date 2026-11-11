"""Locate an expected source/chunk in a captured evaluation, without API/DB calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


def inspect_case(row: dict, target: dict, *, quote: str | None = None) -> dict:
    evidence = row.get("evaluation_evidence")
    if not evidence:
        error = row.get("error")
        evidence = error.get("evaluation_evidence", {}) if isinstance(error, dict) else {}
    if not isinstance(evidence, dict):
        evidence = {}
    retrieval = evidence.get("retrieval") or {}
    bundle = retrieval.get("trace") or {}
    tasks = bundle.get("tasks") or []
    traces = [(t.get("task_index"), t.get("trace")) for t in tasks if t.get("route") == "hybrid"]
    traces.append((None, bundle.get("request")))

    def matches(item):
        for key, value in target.items():
            if value is None:
                continue
            if key == "dataset":
                if str(value) not in {str(item.get("source_dataset")), str(item.get("ingestion_dataset"))}:
                    return False
            elif key == "source_id" and item.get("source_id") is None and item.get("source_ids"):
                if str(value) not in {str(v) for v in item["source_ids"]}:
                    return False
            elif item.get(key) is None or str(item[key]) != str(value):
                return False
        return True

    stages, decisions = [], []
    complete = bool(bundle) and all(t and t.get("complete") is True for _, t in traces)
    for task_index, trace in traces:
        if not trace:
            continue
        for stage in trace.get("stages", []):
            hits = [item for item in stage["candidates"] if matches(item)]
            stages.append({"task_index": task_index, "stage": stage["stage"], "count": stage["count"],
                           "matched": len(hits), "candidates": hits})
        decisions.extend({"task_index": task_index, **item} for item in trace.get("decisions", []) if matches(item))

    def seen(name):
        return any(s["stage"] == name and s["matched"] for s in stages)

    final = []
    def contains(text, expected):
        return re.sub(r"\s+", "", expected) in re.sub(r"\s+", "", text or "")

    for source in evidence.get("sources", []):
        direct_match = matches(source)
        neighbors = [c for c in source.get("context_chunks", []) if matches(c)]
        if direct_match or neighbors:
            final.append({"index": source["index"], "matched_as": "anchor" if direct_match else "neighbor",
                          "cited_in_answer": source.get("cited_in_answer"),
                          "quote_in_content": contains(source.get("content"), quote) if quote else None,
                          "quote_in_shown_content": contains(source.get("shown_content"), quote) if quote else None})
    classification = "inconclusive"
    shown_matches = [s for s in final if not quote or s["quote_in_shown_content"]]
    if final:
        if row.get("error"):
            classification = "request_failed_after_retrieval"
        elif evidence.get("answer_mode") == "direct":
            classification = "direct_response"
        elif quote and not any(s["quote_in_content"] for s in final):
            classification = "quote_not_in_final_evidence"
        elif quote and not any(s["quote_in_shown_content"] for s in final):
            classification = "evidence_not_shown"
        elif any(s["cited_in_answer"] is True for s in shown_matches):
            classification = "shown_and_cited" if quote else "selected_and_cited"
        elif shown_matches and all(s["cited_in_answer"] is False for s in shown_matches):
            classification = "shown_but_not_cited" if quote else "selected_but_not_cited"
        else:
            classification = "final_evidence_citation_unknown"
    elif not bundle:
        classification = "trace_not_captured"
    elif not complete:
        classification = "inconclusive_trace_incomplete"
    elif seen("task_selected"):
        classification = "removed_during_request_merge" if seen("task_scope_checked") else "excluded_by_task_scope"
    elif seen("rerank_input"):
        relevant = [t for idx, t in traces if t and any(
            s["task_index"] == idx and s["stage"] == "rerank_input" and s["matched"] for s in stages)]
        modes = {t.get("reranker", {}).get("status") for t in relevant}
        if "score_count_mismatch" in modes or None in modes:
            classification = "inconclusive_reranker"
        elif any(d["stage"] == "selection_cap" for d in decisions):
            classification = "excluded_by_source_cap"
        else:
            classification = "not_selected_after_rerank" if "applied" in modes else "not_selected_after_initial_ranking"
    elif seen("applicability_checked"):
        classification = "excluded_by_attachment_filter"
    elif seen("department_focus"):
        classification = "excluded_by_applicability_filter"
    elif seen("expanded_candidates"):
        classification = "excluded_by_department_focus"
    elif seen("channel_merged"):
        classification = "not_admitted_to_candidate_pool"
    elif any(s["matched"] for s in stages):
        classification = "filtered_before_candidate_pool"
    elif retrieval.get("status") == "partial":
        classification = "inconclusive_channel_failure"
    elif not any(t.get("route") == "hybrid" for t in tasks):
        classification = "not_observed_in_direct_results"
    else:
        classification = "not_observed_in_candidates"
    return {"case_id": row.get("id"), "target": target, "classification": classification,
            "trace_complete": complete, "retrieval_status": retrieval.get("status"),
            "stages": stages, "decisions": decisions, "final_sources": final,
            "limitation": "Citation presence does not prove factual coverage; inspect the answer and gold checklist."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--source-id")
    parser.add_argument("--chunk-row-id")
    parser.add_argument("--chunk-id")
    parser.add_argument("--chunk-index", type=int)
    parser.add_argument("--dataset")
    parser.add_argument("--department-id")
    parser.add_argument("--quote", help="Verbatim expected source passage; compared after whitespace normalization")
    args = parser.parse_args()
    if args.quote is not None and not args.quote.strip():
        parser.error("--quote must contain a non-whitespace source passage")
    if not (args.source_id or args.chunk_row_id):
        parser.error("provide --source-id or --chunk-row-id; chunk_id/index alone are not global identifiers")
    target = {key: getattr(args, key) for key in (
        "source_id", "chunk_row_id", "chunk_id", "chunk_index", "dataset", "department_id") if getattr(args, key) is not None}
    with args.results.open(encoding="utf-8") as handle:
        cases = []
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if row.get("id") == args.case_id:
                    cases.append(row)
    if len(cases) != 1:
        parser.error("case ID must occur exactly once in results")
    print(json.dumps(inspect_case(cases[0], target, quote=args.quote), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
