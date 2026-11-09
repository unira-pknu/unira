-- Keep the existing two-argument RPCs for older backend clients.
-- Three required arguments avoid ambiguous PostgREST overload resolution.
do $migration$
declare
    family text;
begin
    foreach family in array array['rag', 'pknu_notice', 'pknu_main', 'rule', 'pknu_syllabus']
    loop
        execute format($definition$
            create or replace function public.%I(
                query_text text, match_count integer, metadata_filter jsonb
            ) returns table (
                id text, source_id text, source_slug text, source_type text,
                title text, url text, parent_url text, chunk_id integer,
                chunk_index integer, content text, metadata jsonb,
                lexical_rank double precision
            ) language sql stable security invoker
            set search_path = pg_catalog, public, extensions
            as $body$
                with eligible as materialized (
                    select s.id, s.metadata from public.%I s
                    where s.status = 'active'
                      and s.metadata @> case
                          when coalesce(metadata_filter, '{}'::jsonb) ? 'ingestion_dataset'
                          then jsonb_build_object('ingestion_dataset', metadata_filter->'ingestion_dataset')
                          else '{}'::jsonb end
                ), scored as materialized (
                    select c.id, c.source_id, c.chunk_index,
                           ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', query_text))::double precision as lexical_rank
                    from eligible s join public.%I c on c.source_id = s.id
                    where c.content_tsv @@ websearch_to_tsquery('simple', query_text)
                      and (s.metadata || c.metadata) @> coalesce(metadata_filter, '{}'::jsonb)
                ), ranked as (
                    select scored.*, row_number() over (partition by source_id order by lexical_rank desc, chunk_index) as source_rank
                    from scored
                ), selected as (
                    select id, lexical_rank from ranked where source_rank <= 3
                    order by lexical_rank desc, source_id, chunk_index
                    limit greatest(match_count, 0)
                )
                select c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url,
                       s.parent_url, c.chunk_id, c.chunk_index, c.content,
                       s.metadata || c.metadata || jsonb_build_object(
                           'priority_score', coalesce(s.priority_score, 0.0),
                           'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                       ) as metadata, selected.lexical_rank
                from selected join public.%I c on c.id=selected.id
                join public.%I s on s.id=c.source_id
                order by selected.lexical_rank desc, c.source_id, c.chunk_index
            $body$;
        $definition$, 'match_' || family || '_documents_lexical', family || '_sources', family || '_chunks',
                       family || '_chunks', family || '_sources');
    end loop;
end
$migration$;

notify pgrst, 'reload schema';
