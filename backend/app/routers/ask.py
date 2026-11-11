"""POST /ask RAG question answering endpoint."""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from typing import Any, Dict, Iterator, List, Tuple
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..deps import AppState, get_state
from ..excerpts import extract_relevant_excerpt
from ..evaluation import evidence_snapshot
from ..evidence_context import supplement_neighbors
from ..generation import generate_answer, generate_small_talk, stream_answer
from ..query_rewrite import plan_question
from ..query_plan import QuestionPlan, SearchTask, finish_plan, rule_plan
from ..notice_listing import list_notices
from ..source_scope import checked_rows, scope_reason
from ..query_transform import transform_query
from ..rate_limit import enforce_ask_rate_limit
from ..retrieval import _dedupe_key, _rag_self_intro_first_stage_overrides, search
from ..schemas import AskRequest, AskResponse, Attachment, Source
from ..search_status import SearchResults, SearchUnavailable
from ..retrieval_trace import RetrievalTrace, source_identity
from ..syllabus import extract_request, search_syllabus

logger = logging.getLogger(__name__)
router = APIRouter()

NO_INFO_ANSWER = "관련 정보를 찾을 수 없습니다."
SEARCH_UNAVAILABLE_MESSAGE = "검색 서비스를 일시적으로 확인할 수 없습니다. 잠시 후 다시 시도해 주세요."


class _SearchUnavailableHTTP(HTTPException):
    def __init__(self, diagnostics: dict):
        super().__init__(status_code=503, detail=SEARCH_UNAVAILABLE_MESSAGE)
        self.diagnostics = diagnostics


def _retrieval_fields(rows: list | None) -> dict:
    return {"retrieval_status": "partial" if rows.degraded else "complete"} if isinstance(rows, SearchResults) else {}


def _is_no_info_answer(answer: str) -> bool:
    """Recognize a bare refusal and conservative all-refusal explanations.

    Citations, questions and positive sentences keep a partial answer usable.
    This is a fallback classifier, not a semantic correctness judgment.
    """
    core = answer.strip().strip("\"'“”").rstrip(".").strip()
    if core == NO_INFO_ANSWER.rstrip("."):
        return True
    if NO_INFO_ANSWER.rstrip(".") not in core or re.search(r"\[\d+\]|[?？]", core):
        return False
    sentences = [s.strip() for s in re.split(r"[.!。\n]+", core) if s.strip()]
    for sentence in sentences:
        if sentence == NO_INFO_ANSWER.rstrip("."):
            continue
        # Do not swallow a mixed clause ("노선은 A이며 시간은 확인되지 않음").
        if re.search(r"\d|이며|이고|지만|반면|[,;]", sentence):
            return False
        if not re.fullmatch(
            r"(?:따라서\s+)?[^.!?\n]+(?:확인되지 않습니다|확인할 수 없습니다|"
            r"답변을 드릴 수 없습니다|답변할 수 없습니다)", sentence
        ):
            return False
    return bool(sentences)


def _similarity(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("similarity") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _score(row: Dict[str, Any], key: str) -> float:
    metadata = row.get("metadata") or {}
    try:
        return float(row.get(key, metadata.get(key)) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _attachment_name(name: Any, url: str, index: int) -> str:
    normalized = str(name or "").strip()
    if normalized:
        return normalized
    filename = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip()
    return filename or f"attachment-{index}"


def _row_attachments(row: Dict[str, Any]) -> list[Attachment]:
    metadata = row.get("metadata") or {}
    candidates: list[tuple[Any, Any]] = []
    raw_attachments = metadata.get("attachments")
    if isinstance(raw_attachments, list):
        for item in raw_attachments:
            if isinstance(item, dict):
                candidates.append((item.get("name"), item.get("url")))

    candidates.append(
        (metadata.get("attachment_name"), metadata.get("attachment_url"))
    )

    attachments: list[Attachment] = []
    seen_urls: set[str] = set()
    for name, raw_url in candidates:
        url = str(raw_url or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        attachments.append(
            Attachment(
                name=_attachment_name(name, url, len(attachments) + 1),
                url=url,
            )
        )
    return attachments


def _row_to_source(row: Dict[str, Any]) -> Source:
    metadata = row.get("metadata") or {}
    title = (
        metadata.get("doc_title")
        or metadata.get("source_file")
        or metadata.get("title")
        or row.get("title")
        or row.get("source_slug")
        or "(제목 없음)"
    )
    uri = (
        row.get("uri")
        or row.get("url")
        or metadata.get("doc_url")
        or metadata.get("source_page_url")
        or metadata.get("attachment_url")
        or ""
    )
    return Source(
        title=str(title),
        uri=str(uri),
        content=str(row.get("content") or ""),
        similarity=float(row.get("similarity") or 0.0),
        priority_score=_score(row, "priority_score"),
        dataset_priority=_score(row, "dataset_priority"),
        final_score=_score(row, "final_score"),
        rerank_score=_score(row, "rerank_score"),
        rerank_final_score=_score(row, "rerank_final_score"),
        attachments=_row_attachments(row),
        **source_identity(row),
    )


def _search_one(state: AppState, search_query: str, task: SearchTask | None = None, *, capture_trace: bool = False) -> List[Dict[str, Any]]:
    """One embed+search() call for a single (already standalone) query
    string -- the same call _retrieve() always made, just factored out so
    it can run once per decomposed sub-query."""
    embedding = state.embedder.encode_query(search_query)
    return search(
        state.supabase,
        rpc_names=state.settings.rpc_names,
        embedding=embedding,
        top_k=state.settings.rag_top_k,
        first_stage_k=state.settings.rag_first_stage_k,
        per_rpc_first_stage_k=_rag_self_intro_first_stage_overrides(search_query),
        rpc_concurrency=state.settings.rag_rpc_concurrency,
        min_similarity=state.settings.rag_min_similarity,
        priority_weight=state.settings.rag_priority_weight,
        dataset_priority_weight=state.settings.rag_dataset_priority_weight,
        source_kind_weight=state.settings.rag_source_kind_weight,
        reranker=state.reranker,
        reranker_weight=state.settings.reranker_weight,
        max_chunks_per_url=state.settings.rag_max_chunks_per_url,
        max_lexical_chunks_per_url=state.settings.rag_max_lexical_chunks_per_url,
        query_text=search_query,
        per_rpc_metadata_filters={"match_rag_documents": {"ingestion_dataset": task.target.department_id}}
        if task and task.target.department_id else None,
        title_anchor=state.settings.rag_title_anchor_enabled,
        scope_check=(lambda row: scope_reason(row, task) is None) if task else None,
        capture_trace=capture_trace,
    )


def _search_all(state: AppState, search_queries: List[str], tasks: list[SearchTask] | None = None, *, capture_trace: bool = False) -> List[List[Dict[str, Any]]]:
    """Search each sub-query at the same time instead of one after another.

    Each search is mostly waiting on Supabase round trips, so a two-part
    question took about twice as long as a single one. Results come back in
    query order and are identical to running them in sequence (measured
    2026-11-02: 2-part questions 11-21s -> 9-13s on CPU)."""
    def run(index: int):
        try:
            options = {"capture_trace": True} if capture_trace else {}
            return _search_one(state, search_queries[index], tasks[index], **options) if tasks else _search_one(state, search_queries[index], **options)
        except SearchUnavailable as exc:
            # A failed sub-query must not discard other sub-queries' evidence.
            return SearchResults(channels=exc.channels, failed_queries=[search_queries[index]], trace=exc.trace)
    if len(search_queries) <= 1:
        return [run(i) for i in range(len(search_queries))]
    with ThreadPoolExecutor(max_workers=len(search_queries)) as pool:
        return list(pool.map(run, range(len(search_queries))))


def _merge_rows(rows_by_query: List[List[Dict[str, Any]]], top_k: int) -> List[Dict[str, Any]]:
    """Round-robin merge of one already-ranked rows list per sub-query.

    Interleaving (rather than a global sort by final_score) matters
    specifically for compound queries: each sub-query's final_score was
    computed against its own reranker pass, on its own scale, so a plain
    global sort would tend to let whichever intent scores systematically
    higher crowd out the other -- exactly the failure mode decomposition
    was meant to fix. Round-robin guarantees every sub-query gets a fair
    share of the final slots. A single-query call (the common case) is
    just one list, so this is a no-op pass-through.
    """
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row_group in zip_longest(*rows_by_query):
        for row in row_group:
            if row is None:
                continue
            key = _dedupe_key(row)
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
            if len(merged) >= top_k:
                return merged
    return merged


def _retrieve(
    *,
    state: AppState,
    user_id: str,
    question: str,
    chat_history: List[Dict[str, str]],
    sub_questions: List[str],
    plan: QuestionPlan | None = None,
    capture_trace: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Source]]:
    """Retrieval for the JSON answer response, one search per planned sub-query.

    `question` alone is frequently not enough to search with well:
    - with prior conversation, a follow-up like "그럼 거기 대표 전화번호는요?"
      carries no literal keyword for what "거기" refers to;
    - a compound question ("도서관 전화번호나 증명서 발급기 위치") packs two
      intents into one sentence that a single embedding/lexical search
      represents as one diluted vector, typically favoring whichever intent
      dominates;
    - a colloquial abbreviation ("컴공") may not literally appear anywhere
      near the formal name ("컴퓨터·인공지능공학부") official documents use.
    plan_search_queries() resolves all three *only for the search step* --
    the original `question` and chat_history are still what gets sent to
    answer generation unchanged (see ask()/generate_answer
    below), so none of this touches generation or the JSON response.
    """
    plan = plan or finish_plan(rule_plan(question, chat_history), question, sub_questions)
    if plan.route in {"clarification", "refused"}:
        return [{"_direct_answer": plan.clarification, "_answerable": False}], []
    tasks = plan.tasks
    search_queries = [transform_query(task.text) or task.text for task in tasks]
    logger.info(
        "ask: user=%s question=%r sub_questions=%r search_queries=%r history_turns=%d",
        user_id,
        question,
        sub_questions,
        search_queries,
        len(chat_history),
    )

    health = SearchResults()
    request_trace = RetrievalTrace() if capture_trace else None
    task_traces = []

    def finish_rows(rows):
        if request_trace is not None:
            health.trace = {"schema_version": 1, "tasks": task_traces, "request": request_trace.export()}
        return SearchResults(rows, channels=health.channels, failed_queries=health.failed_queries, trace=health.trace)

    try:
        rows_by_query = [[] for _ in tasks]
        degraded_tasks: set[int] = set()
        normal_indices = []
        direct_answers = []
        for index, task in enumerate(tasks):
            if task.conditions.kind in {"latest", "open"}:
                result = list_notices(state.supabase, task)
                rows_by_query[index] = result.rows
                if request_trace is not None:
                    task_traces.append({"task_index": index, "route": "notice_listing", "capture_scope": "returned_rows_only"})
                    request_trace.snapshot("direct_returned", result.rows, task_index=index)
                direct_answers.append((index, result.answer))
                continue
            syllabus_request = extract_request(task.text, chat_history)
            if syllabus_request is None and len(tasks) == 1:
                syllabus_request = extract_request(question, chat_history)
            if task.conditions.kind == "courses" and syllabus_request is None:
                direct_answers.append((index, "어느 교수님의 어느 학기·과정 강의 목록이 필요하신가요?"))
                continue
            if syllabus_request is not None:
                result = search_syllabus(state.supabase, syllabus_request, top_k=state.settings.rag_top_k)
                rows_by_query[index] = checked_rows(result.rows, task)
                if request_trace is not None:
                    task_traces.append({"task_index": index, "route": "syllabus", "capture_scope": "returned_rows_only"})
                    request_trace.snapshot("direct_returned", rows_by_query[index], task_index=index)
                if result.answer is not None:
                    direct_answers.append((index, result.answer))
            else:
                normal_indices.append(index)
        results = _search_all(state, [search_queries[i] for i in normal_indices], [tasks[i] for i in normal_indices],
                              **({"capture_trace": True} if capture_trace else {}))
        for index, found in zip(normal_indices, results):
            if isinstance(found, SearchResults) and found.degraded:
                degraded_tasks.add(index)
            health.channels.extend({**c, "task_index": index} for c in getattr(found, "channels", []))
            health.failed_queries.extend(getattr(found, "failed_queries", []))
            rows_by_query[index] = checked_rows(found, tasks[index])
            if request_trace is not None:
                task_traces.append({"task_index": index, "route": "hybrid", "query": search_queries[index],
                                    "trace": getattr(found, "trace", None)})
                request_trace.snapshot("task_scope_checked", rows_by_query[index], task_index=index)
        if health.degraded and not any(rows_by_query):
            finish_rows([])
            raise SearchUnavailable(health.channels, trace=health.trace)
        if len(tasks) == 1 and direct_answers:
            rows = rows_by_query[0]
            if request_trace is not None:
                request_trace.snapshot("direct_selected", rows)
            return finish_rows([{"_direct_answer": direct_answers[0][1], "_answerable": bool(rows)}, *rows]), [_row_to_source(row) for row in rows]
        if direct_answers and not any(rows_by_query):
            return finish_rows([{"_direct_answer": "\n\n".join(answer for _, answer in direct_answers), "_answerable": False}]), []
        # Preserve unresolved parts of compound questions in the evidence.
        for index, answer in direct_answers:
            if not rows_by_query[index]:
                for group in rows_by_query:
                    for row in group:
                        row.setdefault("metadata", {})["unresolved_request"] = answer
        for group in rows_by_query:
            for row in group:
                metadata = row.setdefault("metadata", {})
                if health.degraded:
                    metadata["retrieval_incomplete"] = True
                if health.failed_queries:
                    note = "다음 요청은 검색 장애로 확인하지 못했습니다: " + "; ".join(health.failed_queries)
                    metadata["unresolved_request"] = "\n".join(filter(None, (metadata.get("unresolved_request"), note)))
    except SearchUnavailable as exc:
        logger.warning("retrieval: cannot establish absence while search channels failed")
        raise _SearchUnavailableHTTP(health.diagnostics()) from exc
    except Exception:
        logger.exception("retrieval failed")
        raise HTTPException(status_code=502, detail="retrieval failed")

    if request_trace is not None:
        request_trace.snapshot("request_merge_input", [row for group in rows_by_query for row in group])
    rows = _merge_rows(rows_by_query, state.settings.rag_top_k)
    selected_keys = {_dedupe_key(row) for row in rows}
    coverage = []
    for index, task in enumerate(tasks):
        found = rows_by_query[index]
        status = ("candidates_selected" if any(_dedupe_key(row) in selected_keys for row in found)
                  else "not_selected" if found
                  else "search_unavailable" if index in degraded_tasks
                  else "no_candidates")
        coverage.append({"question": task.text, "status": status, "candidate_count": len(found)})
    for row in rows:
        row.setdefault("metadata", {})["request_coverage"] = coverage
        row["metadata"]["comparison_targets"] = plan.comparison_targets
    if request_trace is not None:
        request_trace.snapshot("request_selected", rows, top_k=state.settings.rag_top_k)

    def neighbor_in_scope(parent: dict, candidate: dict) -> bool:
        metadata = parent.get("metadata") or {}
        return any(
            task.target.model_dump() == metadata.get("validated_target")
            and task.conditions.model_dump(mode="json") == metadata.get("query_conditions")
            and task.text == metadata.get("request_query")
            and scope_reason(candidate, task) is None
            for task in tasks
        )

    rows = supplement_neighbors(
        state.supabase, rows,
        max_sources=getattr(state.settings, "rag_context_neighbor_sources", 4),
        scope_check=neighbor_in_scope,
    )
    if request_trace is not None:
        request_trace.snapshot("context_components", [c for r in rows for c in r.get("_context_chunks", [])])
        request_trace.snapshot("generation_sources", rows)
    for row in rows:
        row.setdefault("metadata", {})["request_response_format"] = plan.response_format

    sources = [_row_to_source(r) for r in rows]
    top_similarity = max((_similarity(row) for row in rows), default=0.0)
    logger.info("ask: final_sources=%d top_similarity=%.4f", len(sources), top_similarity)
    if logger.isEnabledFor(logging.DEBUG):
        source_summaries = [
            {
                "index": index,
                "rpc": (row.get("metadata") or {}).get("rpc_name"),
                "title": source.title[:120],
                "similarity": round(source.similarity, 4),
                "priority_score": round(source.priority_score, 4),
                "dataset_priority": round(source.dataset_priority, 4),
                "final_score": round(source.final_score, 4),
                "rerank_score": round(source.rerank_score, 4),
                "rerank_final_score": round(source.rerank_final_score, 4),
            }
            for index, (row, source) in enumerate(zip(rows, sources), start=1)
        ]
        logger.debug("ask: final_source_summaries=%s", source_summaries)
    return finish_rows(rows), sources


@router.post("/ask", response_model=AskResponse, response_model_exclude_none=True)
def ask(
    payload: AskRequest,
    state: AppState = Depends(get_state),
    user_id: str = Depends(enforce_ask_rate_limit),
):
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")
    chat_history = [turn.model_dump() for turn in payload.chat_history]
    if payload.include_evidence and payload.stream:
        raise HTTPException(status_code=400, detail="include_evidence requires stream=false")
    if payload.stream:
        return StreamingResponse(
            _ask_events(state=state, user_id=user_id, question=question, chat_history=chat_history),
            media_type="text/event-stream",
            # Proxies (ngrok, Vercel) must pass each event on as it is written.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    plan = plan_question(
        openai_client=state.openai,
        model=state.settings.openai_model,
        question=question,
        chat_history=chat_history,
    )

    def unavailable(rows: SearchResults, sources: List[Source], answer: str | None = None):
        if payload.include_evidence:
            return JSONResponse(status_code=503, content={
                "detail": SEARCH_UNAVAILABLE_MESSAGE,
                "evaluation_evidence": evidence_snapshot(state, plan, rows, sources, question=question, answer=answer),
            })
        raise _SearchUnavailableHTTP(rows.diagnostics())

    def respond(answer: str, sources: List[Source], answerable: bool, rows: list | None = None) -> AskResponse | JSONResponse:
        if isinstance(rows, SearchResults) and rows.degraded and not answerable:
            return unavailable(rows, sources, answer)
        return AskResponse(
            answer=answer, sources=sources, answerable=answerable,
            evaluation_evidence=evidence_snapshot(state, plan, rows if rows is not None else [], sources, question=question, answer=answer) if payload.include_evidence else None,
            **_retrieval_fields(rows),
        )

    if plan.route in {"clarification", "refused"}:
        return respond(plan.clarification, [], False)
    sub_questions = plan.queries
    if not sub_questions:
        # 인사·잡담: 찾아볼 게 없으니 검색 없이 짧게 답한다(전에는 "관련 정보를 찾을 수 없습니다").
        logger.info("ask: user=%s question=%r small_talk", user_id, question)
        try:
            reply = generate_small_talk(
                openai_client=state.openai,
                model=state.settings.openai_model,
                question=question,
                timeout=state.settings.openai_timeout_seconds,
                chat_history=chat_history,
            )
        except Exception:
            logger.exception("small talk generation failed")
            raise HTTPException(status_code=502, detail="generation failed")
        return respond(reply or NO_INFO_ANSWER, [], bool(reply))

    try:
        rows, sources = _retrieve(
            state=state,
            user_id=user_id,
            question=question,
            chat_history=chat_history,
            sub_questions=sub_questions,
            plan=plan,
            **({"capture_trace": True} if payload.include_evidence else {}),
        )
    except _SearchUnavailableHTTP as exc:
        diagnostics = exc.diagnostics
        return unavailable(SearchResults(channels=diagnostics["channels"],
                                          failed_queries=diagnostics["failed_queries"], trace=diagnostics.get("trace")), [])

    if rows and "_direct_answer" in rows[0]:
        return respond(rows[0]["_direct_answer"], sources, rows[0]["_answerable"], rows)
    if not sources:
        return respond(NO_INFO_ANSWER, [], False, rows)

    try:
        answer = generate_answer(
            openai_client=state.openai,
            model=state.settings.openai_model,
            system_prompt=state.system_prompt,
            question=question,
            rows=rows,
            max_chars_per_chunk=state.settings.max_chars_per_chunk,
            timeout=state.settings.openai_timeout_seconds,
            temperature=state.settings.openai_temperature,
            max_tokens=state.settings.openai_max_tokens,
            chat_history=chat_history,
        )
    except Exception:
        logger.exception("generation failed")
        raise HTTPException(status_code=502, detail="generation failed")

    sources = _with_excerpts(sources, question=question, answer=answer, state=state)
    answer = answer or NO_INFO_ANSWER
    return respond(answer, sources, not _is_no_info_answer(answer), rows)


def _with_excerpts(sources: List[Source], *, question: str, answer: str, state: AppState) -> List[Source]:
    """Trim each source card's text to the part the answer used."""
    return [
        source.model_copy(
            update={
                "content": extract_relevant_excerpt(
                    source.content,
                    question=question,
                    answer=answer,
                    source_index=index,
                    max_chars=state.settings.source_excerpt_max_chars,
                )
            }
        )
        for index, source in enumerate(sources, start=1)
    ]


def _sse(event: Dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _ask_events(
    *,
    state: AppState,
    user_id: str,
    question: str,
    chat_history: List[Dict[str, str]],
) -> Iterator[str]:
    """ask() for the chat screen, as server-sent events.

    Each step is announced when it actually starts ({"type": "status",
    "stage": ...}; the screen picks the words), the answer arrives as
    {"type": "token"} pieces, a failed number check sends "revising" and a
    {"type": "replace"} with the rewritten answer, and {"type": "done"}
    carries the same answer/sources/answerable as the JSON response. A
    failure after the stream started can't change the HTTP status any more,
    so it arrives as {"type": "error"}.
    """
    settings = state.settings
    yield _sse({"type": "status", "stage": "planning"})
    plan = plan_question(
        openai_client=state.openai,
        model=settings.openai_model,
        question=question,
        chat_history=chat_history,
    )
    if plan.route in {"clarification", "refused"}:
        yield _sse({"type": "done", "answer": plan.clarification, "sources": [], "answerable": False})
        return
    sub_questions = plan.queries
    if not sub_questions:
        logger.info("ask: user=%s question=%r small_talk", user_id, question)
        try:
            reply = generate_small_talk(
                openai_client=state.openai,
                model=settings.openai_model,
                question=question,
                timeout=settings.openai_timeout_seconds,
                chat_history=chat_history,
            )
        except Exception:
            logger.exception("small talk generation failed")
            yield _sse({"type": "error", "detail": "generation failed"})
            return
        yield _sse({"type": "done", "answer": reply or NO_INFO_ANSWER, "sources": [], "answerable": bool(reply)})
        return

    yield _sse({"type": "status", "stage": "searching", "count": len(sub_questions)})
    try:
        rows, sources = _retrieve(
            state=state,
            user_id=user_id,
            question=question,
            chat_history=chat_history,
            sub_questions=sub_questions,
            plan=plan,
        )
    except HTTPException as exc:
        yield _sse({"type": "error", "detail": exc.detail, "status_code": exc.status_code})
        return
    if rows and "_direct_answer" in rows[0]:
        yield _sse({"type": "done", "answer": rows[0]["_direct_answer"],
                    "sources": [source.model_dump() for source in sources], "answerable": rows[0]["_answerable"]})
        return
    if not sources:
        if isinstance(rows, SearchResults) and rows.degraded:
            yield _sse({"type": "error", "detail": SEARCH_UNAVAILABLE_MESSAGE, "status_code": 503})
            return
        yield _sse({"type": "done", "answer": NO_INFO_ANSWER, "sources": [], "answerable": False,
                    **_retrieval_fields(rows)})
        return

    yield _sse({"type": "status", "stage": "writing"})
    answer = ""
    try:
        for kind, text in stream_answer(
            openai_client=state.openai,
            model=settings.openai_model,
            system_prompt=state.system_prompt,
            question=question,
            rows=rows,
            max_chars_per_chunk=settings.max_chars_per_chunk,
            timeout=settings.openai_timeout_seconds,
            temperature=settings.openai_temperature,
            max_tokens=settings.openai_max_tokens,
            chat_history=chat_history,
        ):
            if kind == "token":
                yield _sse({"type": "token", "text": text})
            elif kind == "revising":
                yield _sse({"type": "status", "stage": "revising"})
            elif kind == "replace":
                yield _sse({"type": "replace", "text": text})
            else:
                answer = text
    except Exception:
        logger.exception("generation failed")
        yield _sse({"type": "error", "detail": "generation failed"})
        return

    sources = _with_excerpts(sources, question=question, answer=answer, state=state)
    answer = answer or NO_INFO_ANSWER
    if isinstance(rows, SearchResults) and rows.degraded and _is_no_info_answer(answer):
        yield _sse({"type": "error", "detail": SEARCH_UNAVAILABLE_MESSAGE, "status_code": 503})
        return
    yield _sse({
        "type": "done",
        "answer": answer,
        "sources": [source.model_dump() for source in sources],
        "answerable": not _is_no_info_answer(answer),
        **_retrieval_fields(rows),
    })
