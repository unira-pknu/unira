-- Search-function update only: keep rows, vectors, indexes, timeouts and grants.
-- Sort small IDs/scores before loading bodies or concatenating large metadata.
-- Keep the per-source cap of three and both existing RPC signatures.
-- Parse the tsquery once and plan with the actual query/filter values. A cached
-- SQL-function plan can be much slower than the same literal SQL on this corpus.
do $migration$
declare
    family text;
begin
    foreach family in array array['rag', 'pknu_notice', 'pknu_main', 'rule', 'pknu_syllabus']
    loop
        execute format($definition$
            create or replace function public.%I(query_text text, match_count integer default 30)
            returns table (
                id text, source_id text, source_slug text, source_type text,
                title text, url text, parent_url text, chunk_id integer,
                chunk_index integer, content text, metadata jsonb, lexical_rank double precision
            ) language plpgsql stable security invoker
            set search_path = pg_catalog, public, extensions
            as $body$
            declare
                parsed_query tsquery;
            begin
                parsed_query := websearch_to_tsquery('simple', query_text);
                return query execute $query$
                with scored as materialized (
                    select c.id, c.source_id, c.chunk_index,
                           ts_rank_cd(c.content_tsv, $1)::double precision as lexical_rank
                    from public.%I c
                    where c.content_tsv @@ $1
                ), ranked as (
                    select sc.*, row_number() over (
                        partition by sc.source_id order by sc.lexical_rank desc, sc.chunk_index, sc.id
                    ) as source_rank from scored sc
                ), selected as (
                    select r.id, r.lexical_rank from ranked r join public.%I eligible_source on eligible_source.id = r.source_id
                    where r.source_rank <= 3 and eligible_source.status = 'active'
                    order by r.lexical_rank desc, r.source_id, r.chunk_index, r.id
                    limit greatest($2, 0)
                )
                select c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url,
                       s.parent_url, c.chunk_id, c.chunk_index, c.content,
                       s.metadata || c.metadata || jsonb_build_object(
                           'priority_score', coalesce(s.priority_score, 0.0),
                           'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                       ) as metadata, chosen.lexical_rank
                from selected chosen join public.%I c on c.id = chosen.id
                join public.%I s on s.id = c.source_id
                order by chosen.lexical_rank desc, c.source_id, c.chunk_index, c.id
                $query$ using parsed_query, match_count;
            end
            $body$;
        $definition$, 'match_' || family || '_documents_lexical', family || '_chunks', family || '_sources',
                       family || '_chunks', family || '_sources');

        execute format($definition$
            create or replace function public.%I(query_text text, match_count integer, metadata_filter jsonb)
            returns table (
                id text, source_id text, source_slug text, source_type text,
                title text, url text, parent_url text, chunk_id integer,
                chunk_index integer, content text, metadata jsonb, lexical_rank double precision
            ) language plpgsql stable security invoker
            set search_path = pg_catalog, public, extensions
            as $body$
            declare
                parsed_query tsquery;
            begin
                if coalesce(metadata_filter, '{}'::jsonb) = '{}'::jsonb then
                    return query select * from public.%I(query_text, match_count);
                    return;
                end if;
                parsed_query := websearch_to_tsquery('simple', query_text);
                return query execute $query$
                with eligible as materialized (
                    select s.id,
                           s.metadata->'ingestion_dataset' as ingestion_dataset,
                           s.metadata->'original_source_slug' as original_source_slug
                    from public.%I s
                    where s.status = 'active'
                      and s.metadata @> (case
                          when $2 ? 'ingestion_dataset'
                          then jsonb_build_object('ingestion_dataset', $2->'ingestion_dataset')
                          else '{}'::jsonb end
                      || case
                          when $2 ? 'original_source_slug'
                          then jsonb_build_object('original_source_slug', $2->'original_source_slug')
                          else '{}'::jsonb end)
                ), scored as materialized (
                    select c.id, c.source_id, c.chunk_index,
                           ts_rank_cd(c.content_tsv, $1)::double precision as lexical_rank
                    from eligible e join public.%I c on c.source_id = e.id
                    where c.content_tsv @@ $1
                      %s
                      and case
                          -- The common source-only filters do not need a full
                          -- source/chunk JSON concatenation for every match.
                          -- Explicit chunk overrides, including JSON null, win.
                          when $2 - array['ingestion_dataset', 'original_source_slug'] = '{}'::jsonb then
                              (not ($2 ? 'ingestion_dataset') or
                               (case when c.metadata ? 'ingestion_dataset' then c.metadata->'ingestion_dataset'
                                     else e.ingestion_dataset end) = $2->'ingestion_dataset')
                              and (not ($2 ? 'original_source_slug') or
                               (case when c.metadata ? 'original_source_slug' then c.metadata->'original_source_slug'
                                     else e.original_source_slug end) = $2->'original_source_slug')
                          else ((select s.metadata from public.%I s where s.id = c.source_id) || c.metadata) @> $2
                      end
                ), ranked as (
                    select sc.*, row_number() over (
                        partition by sc.source_id order by sc.lexical_rank desc, sc.chunk_index, sc.id
                    ) as source_rank from scored sc
                ), selected as (
                    select r.id, r.lexical_rank from ranked r where r.source_rank <= 3
                    order by r.lexical_rank desc, r.source_id, r.chunk_index, r.id
                    limit greatest($3, 0)
                )
                select c.id, c.source_id, s.source_slug, s.source_type, s.title, s.url,
                       s.parent_url, c.chunk_id, c.chunk_index, c.content,
                       s.metadata || c.metadata || jsonb_build_object(
                           'priority_score', coalesce(s.priority_score, 0.0),
                           'priority_details', coalesce(s.priority_details, '{}'::jsonb)
                       ) as metadata, chosen.lexical_rank
                from selected chosen join public.%I c on c.id = chosen.id
                join public.%I s on s.id = c.source_id
                order by chosen.lexical_rank desc, c.source_id, c.chunk_index, c.id
                $query$ using parsed_query, metadata_filter, match_count;
            end
            $body$;
        $definition$, 'match_' || family || '_documents_lexical', 'match_' || family || '_documents_lexical',
                       family || '_sources', family || '_chunks',
                       case when family = 'rag' then 'and c.source_id = any(array(select e2.id from eligible e2))' else '' end,
                       family || '_sources', family || '_chunks', family || '_sources');
    end loop;
end
$migration$;

notify pgrst, 'reload schema';
