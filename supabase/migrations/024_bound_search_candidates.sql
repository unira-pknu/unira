-- Bound ranking work for frequent Korean terms. Preserve both RPC signatures,
-- the three-chunk source cap and the source/chunk metadata merge contract.
-- Broad searches rank a bounded sample of index matches. Exact title and
-- offering lookups provide an independent path to the relevant document.
create extension if not exists pg_trgm with schema extensions;

do $migration$
declare
    family text;
    source_table text;
    chunk_table text;
    function_name text;
    trigram_schema text;
begin
    select n.nspname into strict trigram_schema
    from pg_extension e join pg_namespace n on n.oid = e.extnamespace
    where e.extname = 'pg_trgm';

    foreach family in array array['rag', 'pknu_notice', 'pknu_main', 'rule', 'pknu_syllabus']
    loop
        source_table := family || '_sources';
        chunk_table := family || '_chunks';
        function_name := 'match_' || family || '_documents_lexical';

        execute format(
            'create index if not exists %I on public.%I using gin (title %I.gin_trgm_ops) where status = ''active''',
            family || '_sources_active_title_trgm_idx', source_table, trigram_schema
        );

        execute format($definition$
            create or replace function public.%1$I(query_text text, match_count integer, metadata_filter jsonb)
            returns table (
                id text, source_id text, source_slug text, source_type text,
                title text, url text, parent_url text, chunk_id integer,
                chunk_index integer, content text, metadata jsonb, lexical_rank double precision
            ) language plpgsql stable security invoker
            set search_path = pg_catalog, public, extensions
            as $body$
            declare
                parsed_query tsquery := websearch_to_tsquery('simple', query_text);
                requested integer := greatest(coalesce(match_count, 0), 0);
                filters jsonb := coalesce(metadata_filter, '{}'::jsonb);
                source_filters jsonb;
                scan_limit bigint := greatest(2048, requested::bigint * 32);
            begin
                if requested = 0 or parsed_query is null or numnode(parsed_query) = 0 then
                    return;
                end if;
                source_filters := (case when filters ? 'ingestion_dataset'
                    then jsonb_build_object('ingestion_dataset', filters->'ingestion_dataset') else '{}'::jsonb end)
                    || (case when filters ? 'original_source_slug'
                    then jsonb_build_object('original_source_slug', filters->'original_source_slug') else '{}'::jsonb end);

                return query execute $query$
                    with matches as materialized (
                        select c.id, c.source_id, c.chunk_index
                        from public.%2$I c join public.%3$I s on s.id = c.source_id
                        where c.content_tsv @@ $1 and s.status = 'active'
                          and ($2 = '{}'::jsonb or s.metadata @> $2)
                          and ($3 = '{}'::jsonb or (s.metadata || c.metadata) @> $3)
                        limit $4
                    ), ranked as (
                        select m.*, ts_rank_cd(c.content_tsv, $1)::double precision as score
                        from matches m join public.%2$I c on c.id = m.id
                    ), per_source as (
                        select r.*, row_number() over (
                            partition by r.source_id order by r.score desc, r.chunk_index, r.id
                        ) as source_rank
                        from ranked r
                    ), chosen as (
                        select r.id, r.score from per_source r
                        where r.source_rank <= 3
                        order by r.score desc, r.source_id, r.chunk_index, r.id
                        limit $5
                    )
                    select c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url,
                           s.parent_url, c.chunk_id, c.chunk_index, c.content,
                           s.metadata || c.metadata || jsonb_build_object(
                               'priority_score', coalesce(s.priority_score, 0.0),
                               'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                           ), chosen.score
                    from chosen join public.%2$I c on c.id = chosen.id
                    join public.%3$I s on s.id = c.source_id
                    order by chosen.score desc, c.source_id, c.chunk_index, c.id
                $query$ using parsed_query, source_filters, filters, scan_limit, requested;
            end
            $body$;
        $definition$, function_name, chunk_table, source_table);

        execute format($definition$
            create or replace function public.%1$I(query_text text, match_count integer default 30)
            returns table (
                id text, source_id text, source_slug text, source_type text,
                title text, url text, parent_url text, chunk_id integer,
                chunk_index integer, content text, metadata jsonb, lexical_rank double precision
            ) language sql stable security invoker
            set search_path = pg_catalog, public, extensions
            as $body$
                select * from public.%1$I(query_text, match_count, '{}'::jsonb)
            $body$;
        $definition$, function_name);
    end loop;
end
$migration$;

create index if not exists pknu_syllabus_sources_active_course_idx
    on public.pknu_syllabus_sources ((metadata->>'course_no')) where status = 'active';
create index if not exists rag_sources_active_menu_idx
    on public.rag_sources (title) where status = 'active';
create index if not exists pknu_main_sources_active_menu_idx
    on public.pknu_main_sources (title) where status = 'active';
create index if not exists rag_sources_active_faculty_idx
    on public.rag_sources (subcategory, processed_at desc) where status = 'active' and category = '교수진';

notify pgrst, 'reload schema';
