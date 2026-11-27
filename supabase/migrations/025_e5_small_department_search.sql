-- Fast e5 search for small departments (022 functions; tables unchanged).
--
-- 022 reads the side table in distance order through the HNSW index and joins
-- each row to the chunk and source tables until match_count rows pass the
-- filter. A department filter rejects most of those rows. For a department that
-- holds a small share of rag, the scan reaches its 20000-row cap and each
-- rejected row costs a random read of rag_chunks and rag_sources. On Small
-- compute that took 43-65 s (cba, axcs, gic, welfare, msce) and 43-60 s for real
-- questions in math and history (2026-11-25, read-only).
--
-- Now a filter on ingestion_dataset first counts the department's chunks
-- through the source metadata GIN index and the chunk source_id index, stopping
-- at 10001. Up to 10000 chunks, the function measures the distance of every chunk
-- of the department and returns the nearest. Measured on the same data, 0.03 s
-- for the smallest departments and 0.1-1.5 s up to 4.4k chunks (warm 0.1-0.5 s).
-- Larger departments keep the 022 index path. They make up 0.4% or more of rag, so
-- the index stream reaches match_count early: 0.2-4 s for physics, fedu, mae and ce.
--
-- The exact path selects candidate sources by their own metadata, which matches
-- the chunk's ingestion_dataset (3000-row sample, 2026-11-25). The full filter on
-- source and chunk metadata still applies to every row.
begin;
set local search_path = pg_catalog, public, extensions;
set local lock_timeout = '10s';
select pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('campus-rag/e5-embeddings'));
-- Load pgvector first so hnsw.iterative_scan in the SET clause is a real setting (see 022).
select '[1]'::extensions.vector(1) <=> '[1]'::extensions.vector(1);

do $migration$
declare
    family text;
begin
    foreach family in array array['rag', 'pknu_notice', 'pknu_main', 'rule', 'pknu_syllabus']
    loop
        execute format($definition$
            create or replace function public.%1$I(
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
                dataset_filter jsonb;
                department_chunks bigint := 0;
            begin
                if metadata_filter ? 'ingestion_dataset' then
                    dataset_filter := jsonb_build_object('ingestion_dataset', metadata_filter -> 'ingestion_dataset');
                    select count(*) into department_chunks
                    from (
                        select 1
                        from public.%3$I s
                        join public.%4$I c on c.source_id = s.id
                        where s.status = 'active' and s.metadata @> dataset_filter
                        limit 10001
                    ) probe;
                end if;

                if dataset_filter is not null and department_chunks <= 10000 then
                    -- Exact: every chunk of the department. MATERIALIZED keeps the
                    -- planner from walking the HNSW index for the ORDER BY.
                    return query
                    with candidate as materialized (
                        select c.id, c.source_id, c.chunk_id, c.chunk_index, c.content,
                               s.metadata || c.metadata || jsonb_build_object(
                                   'priority_score', coalesce(s.priority_score, 0.0),
                                   'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                               ) as metadata,
                               s.source_slug, s.source_type, s.title, s.url, s.parent_url,
                               e.embedding <=> query_embedding::extensions.halfvec(768) as distance
                        from public.%3$I s
                        join public.%4$I c on c.source_id = s.id
                        join public.%2$I e on e.id = c.id and e.content_hash = c.content_hash
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
                    from public.%2$I e
                    order by e.embedding <=> query_embedding::extensions.halfvec(768)
                    limit 20000
                ) hit
                join public.%4$I c on c.id = hit.id and c.content_hash = hit.content_hash
                join public.%3$I s on s.id = c.source_id
                where s.status = 'active'
                  and (s.metadata || c.metadata) @> metadata_filter
                  and 1.0 - hit.distance >= min_similarity
                order by hit.distance
                limit greatest(match_count, 0);
            end
            $body$
        $definition$, 'match_' || family || '_documents_e5', family || '_chunks_e5',
                       family || '_sources', family || '_chunks');
    end loop;
end
$migration$;

commit;

notify pgrst, 'reload schema';
