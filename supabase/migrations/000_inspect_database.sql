-- READ ONLY. Run in the Supabase SQL editor before applying the other files.
begin read only;
select e.extname, e.extversion, n.nspname as extension_schema
from pg_catalog.pg_extension e join pg_catalog.pg_namespace n on n.oid=e.extnamespace
where e.extname = 'vector';

select table_name, column_name, data_type, udt_schema, udt_name, column_default, is_nullable
from information_schema.columns
where table_schema='public' and table_name in
 ('rag_sources','rag_chunks','rule_sources','rule_chunks',
  'pknu_notice_sources','pknu_notice_chunks','pknu_main_sources','pknu_main_chunks','pknu_syllabus_sources','pknu_syllabus_chunks')
order by table_name, ordinal_position;

select c.relname as table_name, a.attname as column_name,
       pg_catalog.format_type(a.atttypid,a.atttypmod) as exact_type
from pg_catalog.pg_attribute a join pg_catalog.pg_class c on c.oid=a.attrelid
join pg_catalog.pg_namespace n on n.oid=c.relnamespace
where n.nspname='public' and a.attname='embedding' and not a.attisdropped;

select c.relname as table_name, con.conname, pg_catalog.pg_get_constraintdef(con.oid) as definition
from pg_catalog.pg_constraint con join pg_catalog.pg_class c on c.oid=con.conrelid
join pg_catalog.pg_namespace n on n.oid=c.relnamespace
where n.nspname='public' and c.relname in
 ('rag_sources','rag_chunks','rule_sources','rule_chunks',
  'pknu_notice_sources','pknu_notice_chunks','pknu_main_sources','pknu_main_chunks','pknu_syllabus_sources','pknu_syllabus_chunks')
order by c.relname, con.conname;

select schemaname, tablename, indexname, indexdef from pg_catalog.pg_indexes
where schemaname='public' and tablename in
 ('rag_sources','rag_chunks','rule_sources','rule_chunks',
  'pknu_notice_sources','pknu_notice_chunks','pknu_main_sources','pknu_main_chunks','pknu_syllabus_sources','pknu_syllabus_chunks')
order by tablename,indexname;

select p.proname, pg_catalog.pg_get_function_identity_arguments(p.oid) as arguments,
       pg_catalog.pg_get_function_result(p.oid) as result_type, p.prosecdef, p.proconfig
from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid=p.pronamespace
where n.nspname='public' and p.proname like 'match_%documents%'
order by p.proname;

select c.relname, c.relrowsecurity, c.relforcerowsecurity
from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid=c.relnamespace
where n.nspname='public' and c.relname in
 ('rag_sources','rag_chunks','rule_sources','rule_chunks',
  'pknu_notice_sources','pknu_notice_chunks','pknu_main_sources','pknu_main_chunks','pknu_syllabus_sources','pknu_syllabus_chunks');
select schemaname,tablename,policyname,roles,cmd,qual,with_check from pg_catalog.pg_policies
where schemaname='public' and tablename in
 ('rag_sources','rag_chunks','rule_sources','rule_chunks',
  'pknu_notice_sources','pknu_notice_chunks','pknu_main_sources','pknu_main_chunks','pknu_syllabus_sources','pknu_syllabus_chunks');
commit;
