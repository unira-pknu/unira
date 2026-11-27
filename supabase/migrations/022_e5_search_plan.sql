-- Make match_<family>_documents_e5 use its HNSW index (021 functions; tables unchanged).
--
-- 021 joined the side table to the chunk and source tables in one flat query. The
-- planner estimated that join (id + content_hash) at about one row and chose to
-- join every chunk first and sort afterwards, never touching the HNSW index:
-- pknu_syllabus took 67 s and a department-filtered rag query 85-109 s
-- (2026-11-18, EXPLAIN ANALYZE, read-only).
--
-- Now a subquery reads the side table in distance order through the index
-- (LIMIT max_scan rows), and the joins and filters run on that stream. The outer
-- query keeps the same order, so it stops as soon as match_count rows pass; with
-- hnsw.iterative_scan the index keeps producing rows past ef_search (40) when the
-- filters reject many, e.g. one department is ~0.4% of rag. Measured on the same
-- queries: 0.1-0.5 s, a small department 1.1 s.
--
-- The join estimate is still about one row, so on small tables (pknu_main, 33k
-- rows) the planner hash-joined all 20000 nearest rows and sorted them: 17 s cold,
-- 5 s warm. Inside these functions only, hash/merge joins and sorts are switched off
-- so the plan is always "nested loop over the index stream, stop at match_count";
-- measured 0.3-1.5 s on every family with generic plans.
--
-- The scan stops after max_scan nearest rows (pgvector's hnsw.max_scan_tuples is
-- also 20000), so a very small department can return fewer than match_count
-- vector rows; its keyword search still runs.
begin;
set local search_path = pg_catalog, public, extensions;
set local lock_timeout = '10s';
select pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('campus-rag/e5-embeddings'));
-- Load pgvector in this session first. Until the library is loaded,
-- hnsw.iterative_scan is only a placeholder, and a placeholder in a function's SET
-- clause needs superuser ("permission denied to set parameter"). 021 passed because
-- creating its HNSW indexes had loaded the library.
select '[1]'::extensions.vector(1) <=> '[1]'::extensions.vector(1);

do $migration$
declare
    family text;
begin
    foreach family in array array['rag', 'pknu_notice', 'pknu_main', 'rule', 'pknu_syllabus']
    loop
        execute format($definition$
            create or replace function public.%I(
                query_embedding extensions.vector(768),
                match_count integer default 5,
                min_similarity double precision default 0.0,
                metadata_filter jsonb default '{}'::jsonb
            ) returns table (
                id text, source_id text, source_slug text, source_type text,
                title text, url text, parent_url text, chunk_id integer,
                chunk_index integer, content text, metadata jsonb,
                similarity double precision
            ) language sql stable security invoker
            set search_path = pg_catalog, public, extensions
            set hnsw.iterative_scan = strict_order
            set enable_hashjoin = off
            set enable_mergejoin = off
            set enable_sort = off
            as $body$
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
                    from public.%I e
                    order by e.embedding <=> query_embedding::extensions.halfvec(768)
                    limit 20000
                ) hit
                join public.%I c on c.id = hit.id and c.content_hash = hit.content_hash
                join public.%I s on s.id = c.source_id
                where s.status = 'active'
                  and (s.metadata || c.metadata) @> metadata_filter
                  and 1.0 - hit.distance >= min_similarity
                order by hit.distance
                limit greatest(match_count, 0)
            $body$
        $definition$, 'match_' || family || '_documents_e5', family || '_chunks_e5',
                       family || '_chunks', family || '_sources');
    end loop;
end
$migration$;

commit;

notify pgrst, 'reload schema';
