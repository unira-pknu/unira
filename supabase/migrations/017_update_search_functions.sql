-- Existing backend RPC contracts, including lexical per-source diversity (3 chunks).
-- INVOKER retains table RLS; qualified relations and a fixed search path are used.
-- HNSW uses halfvec expression indexes; keep full-precision inputs and scores.
begin;
set local search_path = pg_catalog, public, extensions;
create or replace function public.match_rag_documents(
    query_embedding extensions.vector(384),
    match_count integer default 5,
    min_similarity double precision default 0.0,
    metadata_filter jsonb default '{}'::jsonb
)
returns table (
    id text,
    source_id text,
    source_slug text,
    source_type text,
    title text,
    url text,
    parent_url text,
    chunk_id integer,
    chunk_index integer,
    content text,
    metadata jsonb,
    similarity double precision
)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
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
        s.metadata
            || c.metadata
            || jsonb_build_object(
                'priority_score', coalesce(s.priority_score, 0.0),
                'priority_details', coalesce(s.priority_details, '{}'::jsonb)
            ) as metadata,
        1.0 - (c.embedding <=> query_embedding) as similarity
    from public.rag_chunks as c
    join public.rag_sources as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;

create or replace function public.match_rule_documents(
    query_embedding extensions.vector(384),
    match_count integer default 5,
    min_similarity double precision default 0.0,
    metadata_filter jsonb default '{}'::jsonb
)
returns table (
    id text,
    source_id text,
    source_slug text,
    source_type text,
    title text,
    url text,
    parent_url text,
    chunk_id integer,
    chunk_index integer,
    content text,
    metadata jsonb,
    similarity double precision
)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
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
        s.metadata
            || c.metadata
            || jsonb_build_object(
                'priority_score', coalesce(s.priority_score, 0.0),
                'priority_details', coalesce(s.priority_details, '{}'::jsonb)
            ) as metadata,
        1.0 - (c.embedding <=> query_embedding) as similarity
    from public.rule_chunks as c
    join public.rule_sources as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;

create or replace function public.match_pknu_notice_documents(
    query_embedding extensions.vector(384),
    match_count integer default 5,
    min_similarity double precision default 0.0,
    metadata_filter jsonb default '{}'::jsonb
)
returns table (
    id text,
    source_id text,
    source_slug text,
    source_type text,
    title text,
    url text,
    parent_url text,
    chunk_id integer,
    chunk_index integer,
    content text,
    metadata jsonb,
    similarity double precision
)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
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
        s.metadata
            || c.metadata
            || jsonb_build_object(
                'priority_score', coalesce(s.priority_score, 0.0),
                'priority_details', coalesce(s.priority_details, '{}'::jsonb)
            ) as metadata,
        1.0 - (c.embedding <=> query_embedding) as similarity
    from public.pknu_notice_chunks as c
    join public.pknu_notice_sources as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;

create or replace function public.match_pknu_student_life_documents(
    query_embedding extensions.vector(384),
    match_count integer default 5,
    min_similarity double precision default 0.0,
    metadata_filter jsonb default '{}'::jsonb
)
returns table (
    id text,
    source_id text,
    source_slug text,
    source_type text,
    title text,
    url text,
    parent_url text,
    chunk_id integer,
    chunk_index integer,
    content text,
    metadata jsonb,
    similarity double precision
)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
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
        s.metadata
            || c.metadata
            || jsonb_build_object(
                'priority_score', coalesce(s.priority_score, 0.0),
                'priority_details', coalesce(s.priority_details, '{}'::jsonb)
            ) as metadata,
        1.0 - (c.embedding <=> query_embedding) as similarity
    from public.pknu_student_life_chunks as c
    join public.pknu_student_life_sources as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;

set local search_path = pg_catalog, public, extensions;

-- ── Per-source diversity cap for lexical search ──────────────────────────
-- Discovered live: a single 49-chunk notice ("2025-1학기 학생회비 납부 및
-- 사용현황 공지") repeats its own department-name letterhead
-- ("국립부경대학교 컴퓨터·인공지능공학부") on effectively every chunk. Because
-- ts_rank_cd rewards term frequency, most of that document's 49 chunks
-- out-rank every other document that only mentions the department name
-- once -- so a plain "order by lexical_rank desc limit match_count" fills
-- the entire candidate pool with chunks from that one notice, and a
-- genuinely on-topic document (e.g. the department's own contact-info page)
-- never survives to the Python-side reranking at all.
--
-- Fix: rank chunks within each source first, and only let each source
-- contribute its own top 3 chunks into the pool that gets globally ranked
-- and truncated to match_count. Any document that is a source of real
-- redundancy (repeated boilerplate) is capped; a document that is
-- genuinely the best answer many times over (e.g. several distinct
-- relevant articles in one long regulation) still gets multiple chunks
-- through, just not enough to blot out every other source.

create or replace function public.match_rule_documents_lexical(
    query_text text,
    match_count integer default 30
)
returns table (
    id text,
    source_id text,
    source_slug text,
    source_type text,
    title text,
    url text,
    parent_url text,
    chunk_id integer,
    chunk_index integer,
    content text,
    metadata jsonb,
    lexical_rank double precision
)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
    with ranked as (
        select
            c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url, s.parent_url,
            c.chunk_id, c.chunk_index, c.content,
            s.metadata || c.metadata
                || jsonb_build_object(
                    'priority_score', coalesce(s.priority_score, 0.0),
                    'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                ) as metadata,
            ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', query_text)) as lexical_rank,
            row_number() over (
                partition by c.source_id
                order by ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', query_text)) desc
            ) as source_rank
        from public.rule_chunks as c
        join public.rule_sources as s on s.id = c.source_id
        where s.status = 'active'
          and c.content_tsv @@ websearch_to_tsquery('simple', query_text)
    )
    select id, source_id, source_slug, source_type, title, url, parent_url,
           chunk_id, chunk_index, content, metadata, lexical_rank
    from ranked
    where source_rank <= 3
    order by lexical_rank desc
    limit greatest(match_count, 0);
$$;

create or replace function public.match_rag_documents_lexical(
    query_text text,
    match_count integer default 30
)
returns table (
    id text,
    source_id text,
    source_slug text,
    source_type text,
    title text,
    url text,
    parent_url text,
    chunk_id integer,
    chunk_index integer,
    content text,
    metadata jsonb,
    lexical_rank double precision
)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
    with ranked as (
        select
            c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url, s.parent_url,
            c.chunk_id, c.chunk_index, c.content,
            s.metadata || c.metadata
                || jsonb_build_object(
                    'priority_score', coalesce(s.priority_score, 0.0),
                    'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                ) as metadata,
            ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', query_text)) as lexical_rank,
            row_number() over (
                partition by c.source_id
                order by ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', query_text)) desc
            ) as source_rank
        from public.rag_chunks as c
        join public.rag_sources as s on s.id = c.source_id
        where s.status = 'active'
          and c.content_tsv @@ websearch_to_tsquery('simple', query_text)
    )
    select id, source_id, source_slug, source_type, title, url, parent_url,
           chunk_id, chunk_index, content, metadata, lexical_rank
    from ranked
    where source_rank <= 3
    order by lexical_rank desc
    limit greatest(match_count, 0);
$$;

create or replace function public.match_pknu_notice_documents_lexical(
    query_text text,
    match_count integer default 30
)
returns table (
    id text,
    source_id text,
    source_slug text,
    source_type text,
    title text,
    url text,
    parent_url text,
    chunk_id integer,
    chunk_index integer,
    content text,
    metadata jsonb,
    lexical_rank double precision
)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
    with ranked as (
        select
            c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url, s.parent_url,
            c.chunk_id, c.chunk_index, c.content,
            s.metadata || c.metadata
                || jsonb_build_object(
                    'priority_score', coalesce(s.priority_score, 0.0),
                    'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                ) as metadata,
            ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', query_text)) as lexical_rank,
            row_number() over (
                partition by c.source_id
                order by ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', query_text)) desc
            ) as source_rank
        from public.pknu_notice_chunks as c
        join public.pknu_notice_sources as s on s.id = c.source_id
        where s.status = 'active'
          and c.content_tsv @@ websearch_to_tsquery('simple', query_text)
    )
    select id, source_id, source_slug, source_type, title, url, parent_url,
           chunk_id, chunk_index, content, metadata, lexical_rank
    from ranked
    where source_rank <= 3
    order by lexical_rank desc
    limit greatest(match_count, 0);
$$;

create or replace function public.match_pknu_student_life_documents_lexical(
    query_text text,
    match_count integer default 30
)
returns table (
    id text,
    source_id text,
    source_slug text,
    source_type text,
    title text,
    url text,
    parent_url text,
    chunk_id integer,
    chunk_index integer,
    content text,
    metadata jsonb,
    lexical_rank double precision
)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
    with ranked as (
        select
            c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url, s.parent_url,
            c.chunk_id, c.chunk_index, c.content,
            s.metadata || c.metadata
                || jsonb_build_object(
                    'priority_score', coalesce(s.priority_score, 0.0),
                    'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                ) as metadata,
            ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', query_text)) as lexical_rank,
            row_number() over (
                partition by c.source_id
                order by ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', query_text)) desc
            ) as source_rank
        from public.pknu_student_life_chunks as c
        join public.pknu_student_life_sources as s on s.id = c.source_id
        where s.status = 'active'
          and c.content_tsv @@ websearch_to_tsquery('simple', query_text)
    )
    select id, source_id, source_slug, source_type, title, url, parent_url,
           chunk_id, chunk_index, content, metadata, lexical_rank
    from ranked
    where source_rank <= 3
    order by lexical_rank desc
    limit greatest(match_count, 0);
$$;

notify pgrst, 'reload schema';
commit;
