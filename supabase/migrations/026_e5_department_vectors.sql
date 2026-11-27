-- Department-filtered rag e5 search reads one department's vectors in one place
-- (025 functions; rag only, other families unchanged).
--
-- Chunk ids are hashes, so one department's rows are spread over the whole of
-- rag_chunks_e5 (2.3M rows, 5 GB) and rag_chunks (18 GB). On the Small instance
-- neither stays in memory, and the first department-filtered search reads them
-- row by row (2026-11-27, EXPLAIN ANALYZE BUFFERS, cold cache, read-only):
--   exact path (025, up to 10000 chunks): nulife 9243 chunks 7.8 s, itc 9139 7.6 s,
--     28000 / 24000 blocks read, two thirds of them rag_chunks_e5 by primary key;
--   index path (022, larger departments): civil 0.3 s, aquamed 4.6 s (7100 blocks).
-- PostgREST cancels at 8 s, so in the 11-27 evaluation the rag vector channel
-- failed on most department questions and only keyword search answered.
--
-- rag_chunks_e5_dataset copies the e5 vector of every chunk of the departments
-- scripts/vectorization/backfill_e5.py --build-dataset-copy selects (up to 50000
-- chunks by default). The script inserts one department at a time, so its rows
-- sit on adjacent pages and the exact search below reads them in a few large
-- runs instead of one random read per chunk. Only the nearest rows are then
-- joined to rag_chunks and rag_sources, which still decide status, metadata
-- filters and whether the vector is current (content_hash).
--
-- The copy is derived data. Deleting a chunk deletes its copy (on delete cascade);
-- the regular backfill rounds also write the copy for departments that have one;
-- a chunk moved to another department is found again after the next
-- --build-dataset-copy. Departments without a copy keep the 025 behaviour.
--
-- Compressing the copy to bit vectors (binary_quantize) was measured first and
-- dropped: the top 10 kept only 3 to 9 of the exact top 10 even with 400
-- candidates reranked.
begin;
set local search_path = pg_catalog, public, extensions;
set local lock_timeout = '10s';
select pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('campus-rag/e5-embeddings'));
-- Load pgvector first so hnsw.iterative_scan in the SET clause is a real setting (see 022).
select '[1]'::extensions.vector(1) <=> '[1]'::extensions.vector(1);

create table if not exists public.rag_chunks_e5_dataset (
    id text primary key references public.rag_chunks(id) on delete cascade,
    dataset text not null,
    content_hash text not null,
    embedding extensions.halfvec(768) not null
);
alter table public.rag_chunks_e5_dataset enable row level security;
grant select, insert, update, delete on public.rag_chunks_e5_dataset to service_role;
create index if not exists rag_chunks_e5_dataset_dataset_idx on public.rag_chunks_e5_dataset (dataset);

create or replace function public.match_rag_documents_e5(
    query_embedding extensions.vector(768),
    match_count integer default 5,
    min_similarity double precision default 0.0,
    metadata_filter jsonb default '{}'::jsonb
) returns table (
    id text, source_id text, source_slug text, source_type text,
    title text, url text, parent_url text, chunk_id integer,
    chunk_index integer, content text, metadata jsonb,
    similarity double precision
) language plpgsql stable security invoker
set search_path = pg_catalog, public, extensions
set hnsw.iterative_scan = strict_order
set enable_hashjoin = off
set enable_mergejoin = off
set enable_sort = off
as $body$
declare
    dataset_name text;
    dataset_filter jsonb;
    department_chunks bigint := 0;
begin
    if jsonb_typeof(metadata_filter -> 'ingestion_dataset') = 'string' then
        dataset_name := metadata_filter ->> 'ingestion_dataset';
        if exists (select 1 from public.rag_chunks_e5_dataset v where v.dataset = dataset_name) then
            -- Nearest rows of the department from the copy, then the same joins and
            -- filters as the other paths. Extra rows cover copies that the joins
            -- reject (inactive source, other metadata keys, edited chunk).
            return query
            with nearest as materialized (
                select v.id, v.content_hash,
                       v.embedding <=> query_embedding::extensions.halfvec(768) as distance
                from public.rag_chunks_e5_dataset v
                where v.dataset = dataset_name
                order by v.embedding <=> query_embedding::extensions.halfvec(768)
                limit greatest(match_count, 0) * 4 + 20
            )
            select c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url,
                   s.parent_url, c.chunk_id, c.chunk_index, c.content,
                   s.metadata || c.metadata || jsonb_build_object(
                       'priority_score', coalesce(s.priority_score, 0.0),
                       'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                   ) as metadata,
                   1.0 - n.distance as similarity
            from nearest n
            join public.rag_chunks c on c.id = n.id and c.content_hash = n.content_hash
            join public.rag_sources s on s.id = c.source_id
            where s.status = 'active'
              and (s.metadata || c.metadata) @> metadata_filter
              and 1.0 - n.distance >= min_similarity
            order by n.distance
            limit greatest(match_count, 0);
            return;
        end if;
    end if;

    -- 025 unchanged from here: exact search for small departments, index otherwise.
    if metadata_filter ? 'ingestion_dataset' then
        dataset_filter := jsonb_build_object('ingestion_dataset', metadata_filter -> 'ingestion_dataset');
        select count(*) into department_chunks
        from (
            select 1
            from public.rag_sources s
            join public.rag_chunks c on c.source_id = s.id
            where s.status = 'active' and s.metadata @> dataset_filter
            limit 10001
        ) probe;
    end if;

    if dataset_filter is not null and department_chunks <= 10000 then
        return query
        with candidate as materialized (
            select c.id, c.source_id, c.chunk_id, c.chunk_index, c.content,
                   s.metadata || c.metadata || jsonb_build_object(
                       'priority_score', coalesce(s.priority_score, 0.0),
                       'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                   ) as metadata,
                   s.source_slug, s.source_type, s.title, s.url, s.parent_url,
                   e.embedding <=> query_embedding::extensions.halfvec(768) as distance
            from public.rag_sources s
            join public.rag_chunks c on c.source_id = s.id
            join public.rag_chunks_e5 e on e.id = c.id and e.content_hash = c.content_hash
            where s.status = 'active'
              and s.metadata @> dataset_filter
              and (s.metadata || c.metadata) @> metadata_filter
        )
        select k.id, k.source_id, k.source_slug, k.source_type, k.title, k.url,
               k.parent_url, k.chunk_id, k.chunk_index, k.content, k.metadata,
               1.0 - k.distance
        from candidate k
        where 1.0 - k.distance >= min_similarity
        order by k.distance
        limit greatest(match_count, 0);
        return;
    end if;

    return query
    select c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url,
           s.parent_url, c.chunk_id, c.chunk_index, c.content,
           s.metadata || c.metadata || jsonb_build_object(
               'priority_score', coalesce(s.priority_score, 0.0),
               'priority_details', coalesce(s.priority_details, '{}'::jsonb)
           ) as metadata,
           1.0 - hit.distance as similarity
    from (
        select e.id, e.content_hash,
               e.embedding <=> query_embedding::extensions.halfvec(768) as distance
        from public.rag_chunks_e5 e
        order by e.embedding <=> query_embedding::extensions.halfvec(768)
        limit 20000
    ) hit
    join public.rag_chunks c on c.id = hit.id and c.content_hash = hit.content_hash
    join public.rag_sources s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - hit.distance >= min_similarity
    order by hit.distance
    limit greatest(match_count, 0);
end
$body$;

commit;

notify pgrst, 'reload schema';
