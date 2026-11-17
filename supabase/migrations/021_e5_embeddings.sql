-- Second embedding: intfloat/multilingual-e5-base (768 dims) beside the current
-- 384-dim `embedding` column. The existing tables, indexes and match_* functions
-- are not changed; the backend switches by calling match_<family>_documents_e5.
--
-- The vectors live in a side table per family (<family>_chunks_e5) instead of a
-- new column. Filling a new column would rewrite every row of the chunk tables
-- (rag_chunks: 2.2M rows) and add an entry to each of their indexes, including
-- the existing HNSW; here the fill only inserts rows.
--
-- A side row stores the content_hash of the text it was computed from, and search
-- joins on it: an edited chunk drops out of e5 search until
-- scripts/vectorization/backfill_e5.py embeds it again. Deleting a chunk deletes
-- its side row (on delete cascade), so the loader needs no change.
--
-- The HNSW index is created while the tables are empty and is maintained as rows
-- are inserted. A single CREATE INDEX over millions of rows has failed before on
-- this project without a clear error (timeout), and the inserted rows are
-- searchable as soon as they are committed.
--
-- hnsw.iterative_scan: HNSW returns about ef_search (40) nearest rows and the
-- filters run afterwards, so a department filter on rag (one department is ~1% of
-- rows) used to leave almost no vector results. strict_order keeps scanning the
-- index, in distance order, until match_count rows pass the filters.
begin;
set local search_path = pg_catalog, public, extensions;
set local lock_timeout = '10s';
select pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('campus-rag/e5-embeddings'));

do $migration$
declare
    family text;
begin
    foreach family in array array['rag', 'pknu_notice', 'pknu_main', 'rule', 'pknu_syllabus']
    loop
        execute format($table$
            create table if not exists public.%I (
                id text primary key references public.%I(id) on delete cascade,
                content_hash text not null,
                embedding extensions.halfvec(768) not null,
                embedded_at timestamptz not null default now()
            )
        $table$, family || '_chunks_e5', family || '_chunks');
        execute format('alter table public.%I enable row level security', family || '_chunks_e5');
        execute format('grant select, insert, update, delete on public.%I to service_role', family || '_chunks_e5');
        execute format(
            'create index if not exists %I on public.%I using hnsw (embedding extensions.halfvec_cosine_ops) with (m = 16, ef_construction = 64)',
            family || '_chunks_e5_embedding_hnsw_idx', family || '_chunks_e5');

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
            as $body$
                select c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url,
                       s.parent_url, c.chunk_id, c.chunk_index, c.content,
                       s.metadata || c.metadata || jsonb_build_object(
                           'priority_score', coalesce(s.priority_score, 0.0),
                           'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                       ) as metadata,
                       1.0 - (e.embedding <=> query_embedding::extensions.halfvec(768)) as similarity
                from public.%I e
                join public.%I c on c.id = e.id and c.content_hash = e.content_hash
                join public.%I s on s.id = c.source_id
                where s.status = 'active'
                  and (s.metadata || c.metadata) @> metadata_filter
                  and 1.0 - (e.embedding <=> query_embedding::extensions.halfvec(768)) >= min_similarity
                order by e.embedding <=> query_embedding::extensions.halfvec(768)
                limit greatest(match_count, 0)
            $body$
        $definition$, 'match_' || family || '_documents_e5', family || '_chunks_e5',
                       family || '_chunks', family || '_sources');
    end loop;
end
$migration$;

commit;

notify pgrst, 'reload schema';
