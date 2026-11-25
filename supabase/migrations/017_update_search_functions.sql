-- Existing backend RPC contracts, including lexical per-source diversity (3 chunks).
-- INVOKER retains table RLS; qualified relations and a fixed search path are used.
-- HNSW uses halfvec expression indexes; keep full-precision inputs and scores.
begin;
set local search_path = pg_catalog, public, extensions;

-- Define each RPC once per family from the same SQL templates. Signatures,
-- function settings, scoring, filters and the three-chunk source cap are unchanged.
-- Keep this migration self-contained so historical replay needs no external files.
do $search_functions$
declare
    f record;
begin
    for f in
        select * from (values
            ('rag', 'rag'),
            ('rule', 'rule'),
            ('pknu_notice', 'pknu_notice'),
            ('pknu_student_life', 'pknu_student_life')
        ) as families(rpc_family, table_family)
    loop
        execute format($definition$
create or replace function public.%1$I(
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
    from public.%2$I as c
    join public.%3$I as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;
        $definition$, 'match_' || f.rpc_family || '_documents',
                       f.table_family || '_chunks', f.table_family || '_sources');

        execute format($definition$
create or replace function public.%1$I(
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
        from public.%2$I as c
        join public.%3$I as s on s.id = c.source_id
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
        $definition$, 'match_' || f.rpc_family || '_documents_lexical',
                       f.table_family || '_chunks', f.table_family || '_sources');
    end loop;
end
$search_functions$;

notify pgrst, 'reload schema';
commit;
