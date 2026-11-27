"""Offline stage diagnosis must distinguish missing records from missing evidence."""

from copy import deepcopy

import pytest

from eval.tools.inspect_retrieval_trace import inspect_case

TARGET = {"source_id": "s1", "chunk_row_id": "c1"}
CANDIDATE = {**TARGET, "key": "main|row:c1", "rank": 1}


def snapshot(name, found=True):
    return {"stage": name, "count": int(found), "candidates": [dict(CANDIDATE)] if found else []}


def case(stage_names=(), request_stages=(), *, mode="applied", complete=True, partial=False, sources=()):
    return {"id": "example", "evaluation_evidence": {
        "answer_mode": "search", "sources": list(sources),
        "retrieval": {"status": "partial" if partial else "complete", "trace": {
            "tasks": [{"task_index": 0, "route": "hybrid", "trace": {
                "complete": complete, "stages": [snapshot(s) for s in stage_names],
                "decisions": [], "reranker": {"status": mode}}}],
            "request": {"complete": True, "stages": [snapshot(s) for s in request_stages], "decisions": []},
        }},
    }}


# "request" is reserved by pytest (the fixture), so the request-level stages are "request_stages".
@pytest.mark.parametrize("stages,request_stages,mode,expected", [
    ([], [], "no_candidates", "not_observed_in_candidates"),
    (["vector_returned"], [], "no_candidates", "filtered_before_candidate_pool"),
    (["channel_merged"], [], "disabled", "not_admitted_to_candidate_pool"),
    (["rerank_input"], [], "applied", "not_selected_after_rerank"),
    (["rerank_input"], [], "fallback", "not_selected_after_initial_ranking"),
    (["rerank_input"], [], "score_count_mismatch", "inconclusive_reranker"),
    (["task_selected"], ["task_scope_checked"], "applied", "removed_during_request_merge"),
    (["task_selected"], [], "applied", "excluded_by_task_scope"),
])
def test_classifies_the_observed_loss_stage(stages, request_stages, mode, expected):
    assert inspect_case(case(stages, request_stages, mode=mode), TARGET)["classification"] == expected


def test_does_not_call_truncated_or_failed_channels_an_absent_candidate():
    assert inspect_case(case(complete=False), TARGET)["classification"] == "inconclusive_trace_incomplete"
    assert inspect_case(case(partial=True), TARGET)["classification"] == "inconclusive_channel_failure"
    assert inspect_case({"id": "old", "answer": "old answer"}, TARGET)["classification"] == "trace_not_captured"


def test_separates_per_source_caps_from_relevance_rank():
    row = case(["rerank_input", "rerank_ordered"])
    row["evaluation_evidence"]["retrieval"]["trace"]["tasks"][0]["trace"]["decisions"] = [
        {**CANDIDATE, "stage": "selection_cap", "reason": "per_source_cap"}]
    assert inspect_case(row, TARGET)["classification"] == "excluded_by_source_cap"


def final_source(*, shown="제출 서류: 복학원", cited=False):
    return {**TARGET, "index": 1, "content": "제출 서류: 복학원", "shown_content": shown,
            "cited_in_answer": cited}


def test_distinguishes_selection_window_loss_from_missing_citation():
    omitted = case(sources=[final_source(shown="신청 방법 안내")])
    assert inspect_case(omitted, TARGET, quote="제출 서류: 복학원")["classification"] == "evidence_not_shown"
    visible = case(sources=[final_source()])
    assert inspect_case(visible, TARGET, quote="제출 서류: 복학원")["classification"] == "shown_but_not_cited"
    cited = case(sources=[final_source(cited=True)])
    assert inspect_case(cited, TARGET, quote="제출 서류: 복학원")["classification"] == "shown_and_cited"
    # Citation presence remains a signal; it is not a semantic fact score.
    assert "does not prove factual coverage" in inspect_case(cited, TARGET)["limitation"]


def test_neighbor_chunk_is_mapped_to_its_parent_citation_index():
    source = final_source(cited=True)
    source["chunk_row_id"] = "anchor-chunk"
    source["context_chunks"] = [dict(CANDIDATE)]
    report = inspect_case(case(sources=[source]), TARGET, quote="제출 서류: 복학원")
    assert report["classification"] == "shown_and_cited"
    assert report["final_sources"][0]["matched_as"] == "neighbor"


def test_citing_a_different_chunk_of_the_same_source_does_not_cite_the_expected_passage():
    expected = final_source(cited=False)
    other = {**final_source(cited=True), "index": 2, "chunk_row_id": "c2",
             "content": "다른 조항", "shown_content": "다른 조항"}
    report = inspect_case(case(sources=[expected, other]), {"source_id": "s1"}, quote="제출 서류: 복학원")
    assert report["classification"] == "shown_but_not_cited"


def test_failure_envelope_and_direct_answers_do_not_imply_generation_omission():
    original = case(sources=[final_source()])
    error = {"id": "failed", "error": {"evaluation_evidence": original["evaluation_evidence"]}}
    assert inspect_case(error, TARGET)["classification"] == "request_failed_after_retrieval"
    direct = deepcopy(original)
    direct["evaluation_evidence"]["answer_mode"] = "direct"
    assert inspect_case(direct, TARGET)["classification"] == "direct_response"


def test_source_dataset_filter_prevents_cross_dataset_id_collisions():
    row = case(["vector_returned"])
    candidate = row["evaluation_evidence"]["retrieval"]["trace"]["tasks"][0]["trace"]["stages"][0]["candidates"][0]
    candidate["source_dataset"] = "rule"
    report = inspect_case(row, {**TARGET, "dataset": "pknu_main"})
    assert report["classification"] == "not_observed_in_candidates"
