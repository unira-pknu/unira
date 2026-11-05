"""Read-only Supabase audit queries for pipeline check."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.db import connect_postgres  # noqa: E402


def connect() -> psycopg.Connection:
    return connect_postgres(row_factory=dict_row, prepare_threshold=None)


QUERIES: list[tuple[str, str]] = [
    ("rag_sources_count", "select count(*)::bigint as n from rag_sources"),
    ("rag_chunks_count", "select count(*)::bigint as n from rag_chunks"),
    (
        "rag_sources_by_status",
        "select status, count(*)::bigint as n from rag_sources group by status order by n desc",
    ),
    (
        "rag_sources_by_category",
        "select category, count(*)::bigint as n from rag_sources group by category order by n desc",
    ),
    (
        "rag_sources_by_file_ext",
        "select file_ext, count(*)::bigint as n from rag_sources group by file_ext order by n desc",
    ),
    (
        "rag_chunks_by_embedding_dim",
        "select embedding_dim, count(*)::bigint as n from rag_chunks group by embedding_dim order by n desc",
    ),
    (
        "empty_chunks",
        "select count(*)::bigint as n from rag_chunks where content is null or length(trim(content)) = 0",
    ),
    (
        "recent_sources",
        """
        select id, title, category, url, file_ext, source_type, status, updated_at
        from rag_sources
        order by updated_at desc
        limit 10
        """,
    ),
    (
        "recent_chunks_excerpt",
        """
        select s.title, s.category, s.url, s.file_ext, c.chunk_index,
               left(c.content, 300) as excerpt
        from rag_sources s
        join rag_chunks c on c.source_id = s.id
        order by s.updated_at desc
        limit 10
        """,
    ),
    (
        "duplicate_title_url",
        """
        select title, url, count(*)::bigint as n
        from rag_sources
        group by title, url
        having count(*) > 1
        order by n desc
        limit 20
        """,
    ),
    (
        "json_vs_attachment_hint",
        """
        select
          count(*) filter (where file_ext = 'json')::bigint as json_ext_sources,
          count(*) filter (where file_ext in ('pdf','hwp','hwpx','docx','doc'))::bigint as attachment_ext_sources,
          count(*) filter (where source_type = 'web')::bigint as web_sources,
          count(*) filter (where metadata->>'doc_url' is not null and metadata->>'doc_url' <> '')::bigint as has_doc_url_meta
        from rag_sources
        """,
    ),
]

# Whole-corpus ingestion checks across all five table families.
QUERIES.extend([
    (
        'department_attributes',
        """
select department_id,department_name,college_name,count(*) as sources,
       count(*) filter(where department_id is null or department_name is null
          or metadata->>'department_id' is distinct from department_id) as bad_attributes
from public.rag_sources group by department_id,department_name,college_name
order by department_id
        """,
    ),
    (
        'classification_leaks',
        """
select 'rag' as family,count(*) as misplaced_sources from public.rag_sources
where coalesce(metadata->>'ingestion_dataset',metadata->>'source_dataset')
      in ('pknu_main','pknu_notice','pknu_student_life','pknu_syllabus','rule')
union all select 'pknu_main',count(*) from public.pknu_main_sources
where metadata->>'source_dataset'='pknu_notice'
union all select 'pknu_syllabus',count(*) from public.pknu_syllabus_sources
where coalesce(metadata->>'ingestion_dataset',metadata->>'source_dataset','')<>'pknu_syllabus'
        """,
    ),
    (
        'ingestion_sources_by_family',
        """
select 'rag' as family,count(*) as sources from public.rag_sources
union all select 'rule',count(*) from public.rule_sources
union all select 'pknu_notice',count(*) from public.pknu_notice_sources
union all select 'pknu_main',count(*) from public.pknu_main_sources
union all select 'pknu_syllabus',count(*) from public.pknu_syllabus_sources
        """,
    ),
    (
        'ingestion_chunks_by_dataset_model',
        """
with chunks as (
 select 'rag' as family,metadata,embedding_model,embedding_dim,content,embedding from public.rag_chunks
 union all select 'rule',metadata,embedding_model,embedding_dim,content,embedding from public.rule_chunks
 union all select 'pknu_notice',metadata,embedding_model,embedding_dim,content,embedding from public.pknu_notice_chunks
 union all select 'pknu_main',metadata,embedding_model,embedding_dim,content,embedding from public.pknu_main_chunks
 union all select 'pknu_syllabus',metadata,embedding_model,embedding_dim,content,embedding from public.pknu_syllabus_chunks
)
select family,coalesce(nullif(metadata->>'source_dataset',''),'[missing]') as source_dataset,
       embedding_model,embedding_dim,count(*) as chunks,
       count(*) filter(where btrim(content)='') as empty_content,
       count(*) filter(where embedding_dim<>384 or extensions.vector_dims(embedding)<>384) as bad_dimensions
from chunks group by family,source_dataset,embedding_model,embedding_dim
order by family,source_dataset
        """,
    ),
    (
        'ingestion_orphan_chunks',
        """
select 'rag' as family,count(*) as orphan_chunks from public.rag_chunks c left join public.rag_sources s on s.id=c.source_id where s.id is null
union all select 'rule',count(*) from public.rule_chunks c left join public.rule_sources s on s.id=c.source_id where s.id is null
union all select 'pknu_notice',count(*) from public.pknu_notice_chunks c left join public.pknu_notice_sources s on s.id=c.source_id where s.id is null
union all select 'pknu_main',count(*) from public.pknu_main_chunks c left join public.pknu_main_sources s on s.id=c.source_id where s.id is null
union all select 'pknu_syllabus',count(*) from public.pknu_syllabus_chunks c left join public.pknu_syllabus_sources s on s.id=c.source_id where s.id is null
        """,
    ),
    (
        'ingestion_duplicate_chunk_positions',
        """
with keys as (
 select 'rag' as family,source_id,chunk_index from public.rag_chunks
 union all select 'rule',source_id,chunk_index from public.rule_chunks
 union all select 'pknu_notice',source_id,chunk_index from public.pknu_notice_chunks
 union all select 'pknu_main',source_id,chunk_index from public.pknu_main_chunks
 union all select 'pknu_syllabus',source_id,chunk_index from public.pknu_syllabus_chunks
)
select family,source_id,chunk_index,count(*) from keys group by family,source_id,chunk_index having count(*)>1
        """,
    ),
])


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out: dict[str, object] = {}
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                for key, sql in QUERIES:
                    cur.execute(sql)
                    rows = cur.fetchall()
                    if len(rows) == 1 and len(rows[0]) == 1:
                        out[key] = list(rows[0].values())[0]
                    else:
                        out[key] = rows
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
