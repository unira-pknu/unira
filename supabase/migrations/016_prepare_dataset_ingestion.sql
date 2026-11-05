-- Non-destructive bootstrap/upgrade for the existing four RAG table families.
-- Does not migrate rag_documents, delete old rows, or alter auth/chat tables.
-- Search indexes are built AFTER loading with load_to_supabase.py --finalize-indexes.
-- Existing search indexes are preserved; use --prepare-bulk explicitly before a bulk load.
begin;
set local lock_timeout = '10s';
create schema if not exists extensions;
create extension if not exists vector with schema extensions;

do $migration$
declare
    family text;
    source_table text;
    chunk_table text;
    existed boolean;
    vector_type text;
begin
    if not exists (select 1 from pg_catalog.pg_extension e
                   join pg_catalog.pg_namespace n on n.oid=e.extnamespace
                   where e.extname='vector' and n.nspname='extensions') then
        raise exception 'pgvector must be in extensions; inspect 000 before changing its schema';
    end if;
    foreach family in array array['rag','rule','pknu_notice','pknu_student_life'] loop
        source_table := family || '_sources';
        chunk_table := family || '_chunks';
        existed := pg_catalog.to_regclass('public.' || source_table) is not null;
        execute format($ddl$
            create table if not exists public.%I (
                id text primary key, source_slug text unique not null,
                source_type text not null default 'unknown', title text, url text,
                parent_url text, file_path text, file_ext text, category text, subcategory text,
                status text not null default 'active', metadata jsonb not null default '{}'::jsonb,
                crawled_at timestamptz, processed_at timestamptz,
                created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
                priority_score double precision not null default 0,
                priority_details jsonb not null default '{}'::jsonb, priority_updated_at timestamptz
            )$ddl$, source_table);
        if not existed then
            execute format('alter table public.%I enable row level security',source_table);
        end if;
        execute format($ddl$alter table public.%I
            add column if not exists priority_score double precision not null default 0,
            add column if not exists priority_details jsonb not null default '{}'::jsonb,
            add column if not exists priority_updated_at timestamptz$ddl$, source_table);

        existed := pg_catalog.to_regclass('public.' || chunk_table) is not null;
        execute format($ddl$
            create table if not exists public.%I (
                id text primary key,
                source_id text not null references public.%I(id) on delete cascade,
                chunk_id integer not null, chunk_index integer not null,
                content text not null, content_hash text, token_count integer,
                metadata jsonb not null default '{}'::jsonb,
                embedding extensions.vector(384) not null,
                embedding_model text not null default 'sentence-transformers:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
                embedding_dim integer not null default 384, embedded_at timestamptz,
                created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
                unique (source_id, chunk_index)
            )$ddl$, chunk_table, source_table);
        if not existed then
            execute format('alter table public.%I enable row level security',chunk_table);
        end if;
        select pg_catalog.format_type(a.atttypid,a.atttypmod) into vector_type
        from pg_catalog.pg_attribute a
        where a.attrelid=pg_catalog.to_regclass('public.'||chunk_table)
          and a.attname='embedding' and not a.attisdropped;
        if vector_type is null or vector_type !~ '(^|\.)vector\(384\)$' then
            raise exception 'Unexpected embedding type on %: %. No automatic conversion performed',chunk_table,vector_type;
        end if;
        execute format($ddl$alter table public.%I add column if not exists content_tsv tsvector
            generated always as (pg_catalog.to_tsvector('pg_catalog.simple'::regconfig,coalesce(content,''))) stored$ddl$,chunk_table);

        -- Conflict keys used by scripts/vectorization/load_to_supabase.py.
        -- Existing equivalent constraints make these indexes unnecessary.
        if not exists (select 1 from pg_catalog.pg_constraint
                       where conrelid=pg_catalog.to_regclass('public.'||chunk_table)
                         and contype='u' and pg_catalog.pg_get_constraintdef(oid)='UNIQUE (source_id, chunk_index)') then
            execute format('create unique index if not exists %I on public.%I(source_id,chunk_index)',chunk_table||'_source_chunk_ingest_uq',chunk_table);
        end if;
        execute format('create index if not exists %I on public.%I(status)',source_table||'_status_idx',source_table);
        execute format('create index if not exists %I on public.%I((metadata->>''source_dataset''))',source_table||'_dataset_idx',source_table);
        execute format('create index if not exists %I on public.%I(priority_score desc)',source_table||'_priority_score_idx',source_table);
        execute format('create index if not exists %I on public.%I(source_id)',chunk_table||'_source_id_idx',chunk_table);
        if exists(select 1 from pg_catalog.pg_roles where rolname='service_role') then
            execute format('grant select,insert,update,delete on public.%I,public.%I to service_role',source_table,chunk_table);
        end if;
    end loop;
end
$migration$;

-- Own trigger name: does not replace the application-wide set_updated_at function.
create or replace function public.rag_ingestion_updated_at()
returns trigger language plpgsql security invoker set search_path=pg_catalog
as $body$ begin new.updated_at := now(); return new; end $body$;
do $triggers$
declare tab text;
begin
    foreach tab in array array['rag_sources','rag_chunks','rule_sources','rule_chunks',
         'pknu_notice_sources','pknu_notice_chunks','pknu_student_life_sources','pknu_student_life_chunks'] loop
        if not exists(select 1 from pg_catalog.pg_trigger where tgrelid=pg_catalog.to_regclass('public.'||tab)
                      and not tgisinternal and tgname=tab||'_set_updated_at') then
            execute format('drop trigger if exists rag_ingestion_updated_at on public.%I',tab);
            execute format('create trigger rag_ingestion_updated_at before update on public.%I for each row execute function public.rag_ingestion_updated_at()',tab);
        end if;
    end loop;
end $triggers$;
commit;
