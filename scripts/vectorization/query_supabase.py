"""Query Supabase PostgreSQL RAG chunks with the same embedding model."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

try:
    from psycopg import sql
    from psycopg.rows import dict_row
except ModuleNotFoundError:
    sql = None
    dict_row = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.db import connect_postgres  # noqa: E402

from scripts.vectorization.common import DEFAULT_SENTENCE_MODEL as DEFAULT_MODEL, EXPECTED_DIMENSIONS, load_sentence_model, encode_normalized
DATASET_MATCH_FUNCTIONS = {
    "ce": "match_rag_documents",
    "pknu_notice": "match_pknu_notice_documents",
    "pknu_student_life": "match_pknu_main_documents",
    "pknu_main": "match_pknu_main_documents",
    "pknu_syllabus": "match_pknu_syllabus_documents",
    "rule": "match_rule_documents",
}
from scripts.vectorization.datasets import database_tables
DATASET_TABLES = database_tables(PROJECT_ROOT)
for _dataset, _tables in DATASET_TABLES.items():
    DATASET_MATCH_FUNCTIONS.setdefault(_dataset, "match_rag_documents")

FORM_REQUEST_KEYWORDS = (
    "서식",
    "양식",
    "신청서",
    "제출서류",
    "별지",
    "다운로드",
    "파일",
    "원서",
    "확인서",
)
NOTICE_TOPIC_KEYWORDS = {
    "non_curricular": ("비교과", "프로그램", "특강", "교육", "마일리지"),
    "academic": ("학사", "수강", "휴학", "복학", "졸업", "성적", "시험", "전과"),
    "tuition_scholarship": ("등록", "등록금", "장학", "장학금", "납부", "고지서"),
    "recruitment": ("초빙", "채용", "모집", "교원", "직원", "강사"),
    "notice": ("공지", "공지사항", "안내"),
}
APPENDIX_TABLE_REQUEST_KEYWORDS = (
    "별표",
    "기준표",
    "금액표",
    "배점표",
    "목록",
    "등급표",
)


def connect() -> Any:
    if dict_row is None:
        raise RuntimeError("psycopg is required for Supabase queries. Install backend requirements first.")
    return connect_postgres(row_factory=dict_row, prepare_threshold=None)


def embed_query(question: str, model_name: str) -> list[float]:
    model = load_sentence_model(model_name)
    if hasattr(model, "get_embedding_dimension"):
        get_dimensions = model.get_embedding_dimension
    else:
        get_dimensions = model.get_sentence_embedding_dimension
    dimensions = int(get_dimensions() or 0)
    if dimensions != EXPECTED_DIMENSIONS:
        raise ValueError(
            f"Model {model_name} produces {dimensions} dimensions, expected {EXPECTED_DIMENSIONS}."
        )

    vector = encode_normalized(model, [question])[0]
    return [round(float(value), 8) for value in vector]


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


def validate_function_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid function name: {value}")
    return value


def validate_table_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid table name: {value}")
    return value


def contains_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = " ".join(text.split()).lower()
    return any(keyword.lower() in normalized for keyword in keywords)


def query_intent_boosts(question: str, dataset: str) -> dict[str, Any]:
    form_boost = 0.14 if contains_any_keyword(question, FORM_REQUEST_KEYWORDS) else 0.0
    appendix_table_boost = 0.10 if contains_any_keyword(question, APPENDIX_TABLE_REQUEST_KEYWORDS) else 0.0
    notice_topic = ""
    notice_topic_boost = 0.0
    if dataset == "pknu_notice":
        for topic, keywords in NOTICE_TOPIC_KEYWORDS.items():
            if contains_any_keyword(question, keywords):
                notice_topic = topic
                notice_topic_boost = 0.08
                break
    if dataset not in {"rule", "pknu_notice", "pknu_main", "pknu_student_life"}:
        form_boost = 0.0
        appendix_table_boost = 0.0
    return {
        "form_boost": form_boost,
        "appendix_table_boost": appendix_table_boost,
        "notice_topic": notice_topic,
        "notice_topic_boost": notice_topic_boost,
    }


def search(
    question: str,
    model_name: str,
    top_k: int,
    min_similarity: float,
    dataset: str,
    match_function: str | None = None,
) -> list[dict[str, Any]]:
    if sql is None:
        raise RuntimeError("psycopg is required for Supabase queries. Install backend requirements first.")

    embedding = vector_literal(embed_query(question, model_name))
    function_name = validate_function_name(match_function or DATASET_MATCH_FUNCTIONS[dataset])
    query = sql.SQL("""
        select *
        from {match_function}(
            %(embedding)s::extensions.vector(384),
            %(top_k)s,
            %(min_similarity)s,
            '{{}}'::jsonb
        )
    """).format(match_function=sql.Identifier("public", function_name))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                {
                    "embedding": embedding,
                    "top_k": top_k,
                    "min_similarity": min_similarity,
                },
            )
            return list(cur.fetchall())


def search_with_priority(
    question: str,
    model_name: str,
    top_k: int,
    min_similarity: float,
    dataset: str,
    rank_by: str,
    priority_weight: float,
) -> list[dict[str, Any]]:
    if sql is None:
        raise RuntimeError("psycopg is required for Supabase queries. Install backend requirements first.")

    tables = DATASET_TABLES[dataset]
    sources_table = validate_table_name(tables["sources"])
    chunks_table = validate_table_name(tables["chunks"])
    intent_boosts = query_intent_boosts(question, dataset)

    if rank_by == "priority":
        query = sql.SQL("""
            with scored as (
                select
                    s.*,
                    case
                        when %(form_boost)s > 0.0 and (
                            coalesce(s.metadata->>'document_kind', s.metadata->>'attachment_kind', '') = 'form'
                            or coalesce(s.metadata->>'is_form', '') = 'true'
                            or coalesce(s.metadata->>'source_file', '') ~ '별지|서식'
                            or coalesce(s.metadata->>'attachment_name', '') ~ '별지|서식'
                        ) then %(form_boost)s
                        when %(appendix_table_boost)s > 0.0 and (
                            coalesce(s.metadata->>'document_kind', s.metadata->>'attachment_kind', '') = 'appendix_table'
                            or coalesce(s.metadata->>'is_appendix_table', '') = 'true'
                            or coalesce(s.metadata->>'source_file', '') ~ '별표'
                            or coalesce(s.metadata->>'attachment_name', '') ~ '별표'
                        ) then %(appendix_table_boost)s
                        when %(notice_topic_boost)s > 0.0 and (
                            coalesce(s.metadata->>'notice_topic', '') = %(notice_topic)s
                        ) then %(notice_topic_boost)s
                        else 0.0
                    end as intent_boost
                from public.{sources_table} as s
                where s.status = 'active'
            )
            select
                null::text as id,
                s.id as source_id,
                s.source_slug,
                s.source_type,
                s.title,
                s.url,
                s.parent_url,
                null::integer as chunk_id,
                null::integer as chunk_index,
                coalesce(left(string_agg(c.content, E'\n\n' order by c.chunk_index), 1200), '') as content,
                s.metadata,
                null::double precision as similarity,
                s.priority_score,
                s.priority_details,
                s.priority_score + s.intent_boost as final_score
            from scored as s
            left join public.{chunks_table} as c on c.source_id = s.id
            group by
                s.id, s.source_slug, s.source_type, s.title, s.url, s.parent_url,
                s.metadata, s.priority_score, s.priority_details, s.intent_boost, s.updated_at
            order by final_score desc, s.priority_score desc, s.updated_at desc
            limit %(top_k)s
        """).format(
            sources_table=sql.Identifier(sources_table),
            chunks_table=sql.Identifier(chunks_table),
        )
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    {
                        "top_k": top_k,
                        **intent_boosts,
                    },
                )
                return list(cur.fetchall())

    embedding = vector_literal(embed_query(question, model_name))
    order_expression = sql.SQL("similarity desc")
    if rank_by == "hybrid":
        order_expression = sql.SQL("final_score desc, similarity desc")

    query = sql.SQL("""
        with scored as (
            select
                c.id,
                c.source_id,
                s.source_slug,
                s.source_type,
                s.title,
                s.url,
                s.parent_url,
                c.chunk_id,
                c.chunk_index,
                c.content,
                s.metadata || c.metadata as metadata,
                1.0 - (c.embedding <=> %(embedding)s::extensions.vector(384)) as similarity,
                s.priority_score,
                s.priority_details,
                case
                    when %(form_boost)s > 0.0 and (
                        coalesce((s.metadata || c.metadata)->>'document_kind', (s.metadata || c.metadata)->>'attachment_kind', '') = 'form'
                        or coalesce((s.metadata || c.metadata)->>'is_form', '') = 'true'
                        or coalesce((s.metadata || c.metadata)->>'source_file', '') ~ '별지|서식'
                        or coalesce((s.metadata || c.metadata)->>'attachment_name', '') ~ '별지|서식'
                    ) then %(form_boost)s
                    when %(appendix_table_boost)s > 0.0 and (
                        coalesce((s.metadata || c.metadata)->>'document_kind', (s.metadata || c.metadata)->>'attachment_kind', '') = 'appendix_table'
                        or coalesce((s.metadata || c.metadata)->>'is_appendix_table', '') = 'true'
                        or coalesce((s.metadata || c.metadata)->>'source_file', '') ~ '별표'
                        or coalesce((s.metadata || c.metadata)->>'attachment_name', '') ~ '별표'
                    ) then %(appendix_table_boost)s
                    when %(notice_topic_boost)s > 0.0 and (
                        coalesce((s.metadata || c.metadata)->>'notice_topic', '') = %(notice_topic)s
                    ) then %(notice_topic_boost)s
                    else 0.0
                end as intent_boost
            from public.{chunks_table} as c
            join public.{sources_table} as s on s.id = c.source_id
            where s.status = 'active'
              and 1.0 - (c.embedding <=> %(embedding)s::extensions.vector(384)) >= %(min_similarity)s
        )
        select
            *,
            (similarity * (1.0 - %(priority_weight)s))
                + (priority_score * %(priority_weight)s)
                + intent_boost as final_score
        from scored
        order by {order_expression}
        limit %(top_k)s
    """).format(
        sources_table=sql.Identifier(sources_table),
        chunks_table=sql.Identifier(chunks_table),
        order_expression=order_expression,
    )
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                {
                    "embedding": embedding,
                    "top_k": top_k,
                    "min_similarity": min_similarity,
                    "priority_weight": priority_weight,
                    **intent_boosts,
                },
            )
            return list(cur.fetchall())


def print_results(rows: list[dict[str, Any]]) -> None:
    for rank, row in enumerate(rows, start=1):
        metadata = row.get("metadata") or {}
        title = row.get("title") or metadata.get("doc_title") or metadata.get("source_file") or row["source_slug"]
        url = (
            row.get("url")
            or row.get("parent_url")
            or metadata.get("doc_url")
            or metadata.get("source_page_url")
            or metadata.get("attachment_url")
            or ""
        )
        content = " ".join(str(row["content"]).split())
        snippet = content[:300] + ("..." if len(content) > 300 else "")

        scores = []
        if row.get("similarity") is not None:
            scores.append(f"similarity={row['similarity']:.4f}")
        if row.get("priority_score") is not None:
            scores.append(f"priority={row['priority_score']:.4f}")
        if row.get("final_score") is not None:
            scores.append(f"final={row['final_score']:.4f}")
        print(f"\n#{rank} {' '.join(scores)}")
        print(f"title: {title}")
        if url:
            print(f"url: {url}")
        print(f"source_slug: {row['source_slug']} chunk_index: {row.get('chunk_index')}")
        if row.get("priority_details"):
            print(f"priority_details: {row['priority_details']}")
        print(f"snippet: {snippet}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Semantic search against Supabase RAG chunks.")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--dataset", choices=sorted(DATASET_MATCH_FUNCTIONS), default="ce")
    parser.add_argument("--match-function", help="Override public schema match function name.")
    parser.add_argument("--model-name", default=DEFAULT_MODEL)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-similarity", type=float, default=0.0)
    parser.add_argument(
        "--rank-by",
        choices=["similarity", "priority", "hybrid"],
        default="similarity",
        help=(
            "similarity: 기존 RPC 검색, priority: source priority 순 조회, "
            "hybrid: similarity와 priority_score를 섞어 정렬"
        ),
    )
    parser.add_argument(
        "--include-priority",
        action="store_true",
        help="similarity 정렬에서도 priority_score를 함께 출력하기 위해 직접 SQL 검색을 사용한다.",
    )
    parser.add_argument(
        "--priority-weight",
        type=float,
        default=0.15,
        help="--rank-by hybrid에서 priority_score 반영 비율. 기본값: 0.15",
    )
    args = parser.parse_args()

    if args.top_k <= 0:
        raise ValueError("--top-k must be positive")
    if not 0.0 <= args.priority_weight <= 1.0:
        raise ValueError("--priority-weight must be between 0.0 and 1.0")
    if args.rank_by != "priority" and not args.question:
        raise ValueError("question is required unless --rank-by priority is used")
    if args.match_function and (args.rank_by != "similarity" or args.include_priority):
        raise ValueError("--match-function can only be used with the default similarity RPC search")

    if args.rank_by != "similarity" or args.include_priority:
        rows = search_with_priority(
            args.question or "",
            args.model_name,
            args.top_k,
            args.min_similarity,
            args.dataset,
            args.rank_by,
            args.priority_weight,
        )
    else:
        rows = search(
            args.question or "",
            args.model_name,
            args.top_k,
            args.min_similarity,
            args.dataset,
            args.match_function,
        )
    print_results(rows)


if __name__ == "__main__":
    main()
