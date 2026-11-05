-- Five independent datasets. Run once after 016/017; do not replay historical 001-015.
-- Reuses every existing source/chunk ID and embedding. Auth/chat tables are untouched.
-- The department HNSW index is retained. Only moved data gets destination indexes.
begin;
set local search_path = pg_catalog, public, extensions;
set local lock_timeout = '10s';
set local statement_timeout = 0;
set local maintenance_work_mem = '512MB';
select pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('campus-rag/five-dataset-tables'));

do $lock$
declare f text;
begin
    foreach f in array array['rag','rule','pknu_notice','pknu_student_life','pknu_main','pknu_syllabus'] loop
        if to_regclass('public.'||f||'_sources') is not null then
            execute format('lock table public.%I,public.%I in share row exclusive mode',f||'_sources',f||'_chunks');
        end if;
    end loop;
end $lock$;

-- Explicitly omit indexes when cloning: destination search indexes are built after moving rows.
do $create$
declare f text; tab text;
begin
    foreach f in array array['pknu_main','pknu_syllabus'] loop
        if to_regclass('public.'||f||'_sources') is null then
            execute format('create table public.%I (like public.pknu_notice_sources including defaults including constraints including storage)',f||'_sources');
            execute format('alter table public.%I add primary key(id), add unique(source_slug)',f||'_sources');
        end if;
        if to_regclass('public.'||f||'_chunks') is null then
            execute format('create table public.%I (like public.pknu_notice_chunks including defaults including constraints including generated including storage)',f||'_chunks');
            execute format('alter table public.%I add primary key(id), add unique(source_id,chunk_index), add foreign key(source_id) references public.%I(id) on delete cascade',f||'_chunks',f||'_sources');
        end if;
        foreach tab in array array[f||'_sources',f||'_chunks'] loop
            execute format('alter table public.%I enable row level security',tab);
            execute format('grant select,insert,update,delete on public.%I to service_role',tab);
            execute format('drop trigger if exists rag_ingestion_updated_at on public.%I',tab);
            execute format('create trigger rag_ingestion_updated_at before update on public.%I for each row execute function public.rag_ingestion_updated_at()',tab);
        end loop;
    end loop;
end $create$;

create temporary table migration_department_registry (
    department_id text primary key, department_name text not null, college_name text,
    department_homepage text not null, department_majors jsonb not null
) on commit drop;
-- Snapshot of scripts/crawlers/departments/registry.json; future ingestion uses the live registry.
insert into migration_department_registry values
    ('aics', 'AI융합시스템공학과', 'AX혁신대학', 'https://icms.pknu.ac.kr/aics', '[]'::jsonb),
    ('aqua-culture', '수산생명과학부', '수산과학대학', 'http://aqua-culture.pknu.ac.kr', '["양식응용생명과학전공"]'::jsonb),
    ('aquamed', '수산생명의학과', '수산과학대학', 'http://aquamed.pknu.ac.kr', '[]'::jsonb),
    ('archieng', '건축공학과', '공과대학', 'http://archieng.pknu.ac.kr', '[]'::jsonb),
    ('axcs', 'AX지속가능융합학과', 'AX혁신대학', 'https://icms.pknu.ac.kr/axcs', '[]'::jsonb),
    ('axic', 'AX혁신대학', 'AX혁신대학', 'https://axic.pknu.ac.kr/axic', '[]'::jsonb),
    ('axliberal', 'AXIC자유전공학부', 'AX혁신대학', 'https://icms.pknu.ac.kr/axliberal', '[]'::jsonb),
    ('bigdata', '데이터정보과학부', '정보융합대학', 'https://icms.pknu.ac.kr/bigdata', '["빅데이터융합전공"]'::jsonb),
    ('biotech', '생물공학과', '수산과학대학', 'http://biotech.pknu.ac.kr', '[]'::jsonb),
    ('bme', '스마트헬스케어학부', '정보융합대학', 'http://bme.pknu.ac.kr', '["의공학전공"]'::jsonb),
    ('cba', '경영대학', '경영대학', 'http://cba.pknu.ac.kr', '[]'::jsonb),
    ('cbaliberal', '경영대학 자유전공학부', '경영대학', 'https://cbaliberal.pknu.ac.kr', '[]'::jsonb),
    ('ce', '컴퓨터·인공지능공학부', '정보융합대학', 'https://ce.pknu.ac.kr', '["컴퓨터공학전공, 인공지능전공"]'::jsonb),
    ('chem', '화학과', '자연과학대학', 'https://chem.pknu.ac.kr', '[]'::jsonb),
    ('chemeng', '화학공학과', '공과대학', 'http://chemeng.pknu.ac.kr', '[]'::jsonb),
    ('chinese', '중국학과', '인문사회과학대학', 'http://chinese.pknu.ac.kr', '[]'::jsonb),
    ('chss', '인문사회과학대학', '인문사회과학대학', 'http://chss.pknu.ac.kr', '[]'::jsonb),
    ('civil', '지속가능공학부', '공과대학', 'http://civil.pknu.ac.kr', '["토목공학전공"]'::jsonb),
    ('cns', '자연과학대학', '자연과학대학', 'http://cns.pknu.ac.kr', '[]'::jsonb),
    ('coe', '공과대학', '공과대학', 'https://coe.pknu.ac.kr', '[]'::jsonb),
    ('college', '학부대학', '학부대학', 'https://college.pknu.ac.kr', '[]'::jsonb),
    ('control', '전기공학부', '공과대학', 'http://control.pknu.ac.kr', '["제어계측공학전공"]'::jsonb),
    ('dba', '경영학부', '경영대학', 'http://dba.pknu.ac.kr', '[]'::jsonb),
    ('dias', '국제지역학부', '인문사회과학대학', 'http://dias.pknu.ac.kr', '[]'::jsonb),
    ('digitalfinance', '디지털금융학과', '정보융합대학', 'https://digitalfinance.pknu.ac.kr', '[]'::jsonb),
    ('dmfbe', '해양수산경영경제학부', '수산과학대학', 'https://dmfbe.pknu.ac.kr', '[]'::jsonb),
    ('duem', '글로벌자율전공학부', '글로벌자율전공학부', 'https://duem.pknu.ac.kr', '[]'::jsonb),
    ('earth', '지구환경시스템과학부', '환경·해양대학', 'https://earth.pknu.ac.kr', '["환경지질과학전공"]'::jsonb),
    ('ecoeng', '지속가능공학부', '공과대학', 'http://ecoeng.pknu.ac.kr', '["생태공학전공"]'::jsonb),
    ('econ', '경제학과', '인문사회과학대학', 'https://econ.pknu.ac.kr', '[]'::jsonb),
    ('education', '유아교육과', '인문사회과학대학', 'http://education.pknu.ac.kr', '[]'::jsonb),
    ('ee', '전자정보통신공학부', '정보융합대학', 'https://ee.pknu.ac.kr', '["전자공학전공"]'::jsonb),
    ('eese', '전기전자SW공학전공', '미래융합학부', 'https://eese.pknu.ac.kr', '["전기전자SW공학전공"]'::jsonb),
    ('electric-eng', '전기공학부', '공과대학', 'http://electric-eng.pknu.ac.kr', '["전기공학전공"]'::jsonb),
    ('english', '영어영문학부', '인문사회과학대학', 'http://english.pknu.ac.kr', '[]'::jsonb),
    ('engopen', '공과대학 자유전공학부', '공과대학', 'https://engopen.pknu.ac.kr/engopen', '[]'::jsonb),
    ('enma', '환경·해양대학', '환경·해양대학', 'http://enma.pknu.ac.kr', '[]'::jsonb),
    ('env-eng', '지구환경시스템과학부', '환경·해양대학', 'https://env-eng.pknu.ac.kr', '["환경공학전공"]'::jsonb),
    ('envatm', '지구환경시스템과학부', '환경·해양대학', 'https://envatm.pknu.ac.kr', '["환경대기과학전공"]'::jsonb),
    ('ere', '에너지자원공학과', '환경·해양대학', 'https://ere.pknu.ac.kr', '[]'::jsonb),
    ('fashion', '패션디자인학과', '인문사회과학대학', 'http://fashion.pknu.ac.kr', '[]'::jsonb),
    ('fedu', '수해양산업교육과', '수산과학대학', 'http://fedu.pknu.ac.kr', '[]'::jsonb),
    ('fire', '소방공학과', '공과대학', 'http://fire.pknu.ac.kr', '[]'::jsonb),
    ('fisheries', '수산과학대학 자유전공학부', '수산과학대학', 'http://fisheries.pknu.ac.kr', '[]'::jsonb),
    ('fishsci', '수산과학대학', '수산과학대학', 'https://fishsci.pknu.ac.kr', '[]'::jsonb),
    ('food', '식품과학부', '수산과학대학', 'https://food.pknu.ac.kr', '["식품공학전공"]'::jsonb),
    ('future', '미래융합학부', '미래융합학부', 'https://future.pknu.ac.kr/future/1', '[]'::jsonb),
    ('geoinfo', '지구환경시스템과학부', '환경·해양대학', 'http://geoinfo.pknu.ac.kr', '["공간정보시스템공학전공"]'::jsonb),
    ('gic', '글로벌혁신대학', '글로벌혁신대학', 'https://gic.pknu.ac.kr/gic', '[]'::jsonb),
    ('history', '사학과', '인문사회과학대학', 'http://history.pknu.ac.kr', '[]'::jsonb),
    ('hssliberal', '인문사회과학대학 자유전공학부', '인문사회과학대학', 'https://hssliberal.pknu.ac.kr', '[]'::jsonb),
    ('humanbio', '스마트헬스케어학부', '정보융합대학', 'https://humanbio.pknu.ac.kr', '["휴먼바이오융합전공"]'::jsonb),
    ('humanict', '미디어커뮤니케이션학부', '정보융합대학', 'https://humanict.pknu.ac.kr', '["미디어ICT공학전공"]'::jsonb),
    ('ice', '전자정보통신공학부', '정보융합대학', 'http://ice.pknu.ac.kr', '["정보통신공학전공"]'::jsonb),
    ('induseng', '고분자화학소재공학부', '공과대학', 'http://induseng.pknu.ac.kr', '["에너지화학소재공학전공"]'::jsonb),
    ('industrial', '조형학부', '정보융합대학', 'https://industrial.pknu.ac.kr', '["공업디자인전공"]'::jsonb),
    ('itc', '정보융합대학', '정보융합대학', 'https://itc.pknu.ac.kr', '[]'::jsonb),
    ('japanese', '일어일문학부', '인문사회과학대학', 'http://japanese.pknu.ac.kr', '[]'::jsonb),
    ('kukmun', '국어국문학과', '인문사회과학대학', 'http://kukmun.pknu.ac.kr', '[]'::jsonb),
    ('lem', '환경·해양대학 자유전공학부', '환경·해양대학', 'https://lem.pknu.ac.kr', '[]'::jsonb),
    ('liberalitc', '정보융합대학 자유전공학부', '정보융합대학', 'https://liberalitc.pknu.ac.kr', '[]'::jsonb),
    ('lifelong', '평생교육·상담학전공', '미래융합학부', 'https://lifelong.pknu.ac.kr', '["평생교육·상담학전공"]'::jsonb),
    ('mae', '기계공학부', '공과대학', 'http://mae.pknu.ac.kr', '["기계설계공학전공"]'::jsonb),
    ('marinebio', '수산생명과학부', '수산과학대학', 'http://marinebio.pknu.ac.kr', '["자원생물학전공"]'::jsonb),
    ('marinesports', '스마트헬스케어학부', '정보융합대학', 'http://marinesports.pknu.ac.kr', '["해양스포츠전공"]'::jsonb),
    ('masscom', '미디어커뮤니케이션학부', '정보융합대학', 'https://masscom.pknu.ac.kr', '["언론정보전공"]'::jsonb),
    ('materials', '융합소재공학부', '공과대학', 'http://materials.pknu.ac.kr', '["신소재시스템공학전공"]'::jsonb),
    ('materials-engineering', '융합소재공학부', '공과대학', 'https://materials-engineering.pknu.ac.kr', '["재료공학전공"]'::jsonb),
    ('math', '응용수학과', '자연과학대학', 'https://math.pknu.ac.kr', '[]'::jsonb),
    ('mbe', '해양수산경영경제학부', '수산과학대학', 'http://mbe.pknu.ac.kr', '["해양수산경영학전공"]'::jsonb),
    ('me', '기계공학부', '공과대학', 'http://me.pknu.ac.kr', '["기계학전공"]'::jsonb),
    ('metal', '융합소재공학부', '공과대학', 'http://metal.pknu.ac.kr', '["금속공학전공"]'::jsonb),
    ('microbiology', '미생물학과', '자연과학대학', 'http://microbiology.pknu.ac.kr', '[]'::jsonb),
    ('mpsm', '해양생산시스템관리학부', '수산과학대학', 'https://mpsm.pknu.ac.kr', '[]'::jsonb),
    ('msce', '기계조선에너지시스템공학전공', '미래융합학부', 'https://msce.pknu.ac.kr', '["기계조선에너지시스템공학전공"]'::jsonb),
    ('mse', '에너지수송시스템공학부', '공과대학', 'http://mse.pknu.ac.kr', '["기계시스템공학전공"]'::jsonb),
    ('name', '에너지수송시스템공학부', '공과대학', 'https://name.pknu.ac.kr', '["조선해양시스템공학전공"]'::jsonb),
    ('nano', '나노융합반도체공학부', '공과대학', 'http://nano.pknu.ac.kr', '["나노융합공학전공"]'::jsonb),
    ('nsum', '자연과학대학 자유전공학부', '자연과학대학', 'https://nsum.pknu.ac.kr', '[]'::jsonb),
    ('nulife', '식품과학부', '수산과학대학', 'https://nulife.pknu.ac.kr', '["식품영양학전공"]'::jsonb),
    ('nursing', '간호학과', '자연과학대학', 'http://nursing.pknu.ac.kr', '[]'::jsonb),
    ('oceaneng', '해양공학과', '환경·해양대학', 'https://oceaneng.pknu.ac.kr', '[]'::jsonb),
    ('physics', '물리학과', '자연과학대학', 'https://physics.pknu.ac.kr', '[]'::jsonb),
    ('pknuarchi', '조형학부', '정보융합대학', 'https://pknuarchi.pknu.ac.kr', '["건축학전공"]'::jsonb),
    ('pknudic', '국제통상학부', '경영대학', 'http://pknudic.pknu.ac.kr', '[]'::jsonb),
    ('pknulaw', '법학과', '인문사회과학대학', 'http://pknulaw.pknu.ac.kr', '[]'::jsonb),
    ('pknupa', '행정복지학부', '인문사회과학대학', 'http://pknupa.pknu.ac.kr', '[]'::jsonb),
    ('pkuocean', '지구환경시스템과학부', '환경·해양대학', 'https://pkuocean.pknu.ac.kr', '["해양학전공"]'::jsonb),
    ('politics', '정치외교학과', '인문사회과학대학', 'http://politics.pknu.ac.kr', '[]'::jsonb),
    ('polymer', '고분자화학소재공학부', '공과대학', 'http://polymer.pknu.ac.kr', '["고분자공학전공"]'::jsonb),
    ('ps1', '행정복지학부', '인문사회과학대학', 'https://icms.pknu.ac.kr/ps1', '["사회복지학전공"]'::jsonb),
    ('ree', '해양수산경영경제학부', '수산과학대학', 'https://ree.pknu.ac.kr', '["자원환경경제학전공"]'::jsonb),
    ('ref', '에너지수송시스템공학부', '공과대학', 'http://ref.pknu.ac.kr', '["냉동공조공학전공"]'::jsonb),
    ('safe', '경찰범죄심리학전공', '미래융합학부', 'http://safe.pknu.ac.kr', '["경찰범죄심리학전공"]'::jsonb),
    ('safety', '시스템경영안전공학부', '공과대학', 'http://safety.pknu.ac.kr', '["안전공학전공"]'::jsonb),
    ('scicomp', '과학컴퓨팅학과', '자연과학대학', 'https://scicomp.pknu.ac.kr', '[]'::jsonb),
    ('semicon', '전기공학부', '공과대학', 'https://semicon.pknu.ac.kr', '["디스플레이반도체공학전공"]'::jsonb),
    ('semiconductor', '나노융합반도체공학부', '공과대학', 'https://semiconductor.pknu.ac.kr', '["차세대반도체공학전공"]'::jsonb),
    ('smartmobility', '스마트모빌리티공학과', '정보융합대학', 'https://smartmobility.pknu.ac.kr', '[]'::jsonb),
    ('sme', '시스템경영안전공학부', '공과대학', 'https://sme.pknu.ac.kr', '["산업경영공학전공", "기술·데이터공학전공"]'::jsonb),
    ('stat', '데이터정보과학부', '정보융합대학', 'http://stat.pknu.ac.kr', '["통계·데이터사이언스전공"]'::jsonb),
    ('visual', '조형학부', '정보융합대학', 'https://visual.pknu.ac.kr', '["시각디자인전공"]'::jsonb),
    ('welfare', '사회복지서비스학전공', '미래융합학부', 'https://icms.pknu.ac.kr/Welfare', '["사회복지서비스학전공"]'::jsonb);

create temporary table migration_totals(kind text primary key, n bigint not null) on commit drop;
insert into migration_totals values('sources',0),('chunks',0);
do $before$
declare f text; row_count_value bigint;
begin
    foreach f in array array['rag','rule','pknu_notice','pknu_student_life','pknu_main','pknu_syllabus'] loop
        if to_regclass('public.'||f||'_sources') is not null then
            execute format('select count(*) from public.%I',f||'_sources') into row_count_value;
            update migration_totals set n=migration_totals.n+row_count_value where kind='sources';
            execute format('select count(*) from public.%I',f||'_chunks') into row_count_value;
            update migration_totals set n=migration_totals.n+row_count_value where kind='chunks';
        end if;
    end loop;
    raise notice 'Baseline counts captured; source/chunk IDs and vectors will be preserved';
end $before$;

create temporary table migration_source_moves (
    old_family text not null, source_id text not null, target_family text not null,
    primary key(old_family,source_id)
) on commit drop;
insert into migration_source_moves
select 'rag',id,
    case when coalesce(nullif(metadata->>'ingestion_dataset',''),metadata->>'source_dataset')='pknu_syllabus'
              then 'pknu_syllabus'
         when metadata->>'source_dataset'='pknu_notice' or metadata->>'ingestion_dataset'='pknu_notice'
              then 'pknu_notice'
         else 'pknu_main' end
from public.rag_sources
where coalesce(nullif(metadata->>'ingestion_dataset',''),metadata->>'source_dataset')
    in ('pknu_main','pknu_notice','pknu_student_life','pknu_syllabus');
-- is_notice is deliberately NOT used: notice attachments currently carry is_notice=false.
do $legacy$
begin
    if to_regclass('public.pknu_student_life_sources') is not null then
        execute 'insert into migration_source_moves select ''pknu_student_life'',id,''pknu_main'' from public.pknu_student_life_sources';
    end if;
    if exists (
        select 1 from public.rag_sources s
        where not exists(select 1 from migration_source_moves m where m.old_family='rag' and m.source_id=s.id)
          and not exists(select 1 from migration_department_registry d
              where d.department_id=coalesce(nullif(s.metadata->>'ingestion_dataset',''),s.metadata->>'source_dataset'))
    ) then
        raise exception 'Unclassified rag_sources records: inspect before migrating';
    end if;
end $legacy$;

-- All inserts, equality checks and deletes are one transaction. Any conflict aborts the move.
do $move$
declare r record; source_cols text:='id,source_slug,source_type,title,url,parent_url,file_path,file_ext,category,subcategory,status,metadata,crawled_at,processed_at,created_at,updated_at,priority_score,priority_details,priority_updated_at'; chunk_cols text:='id,source_id,chunk_id,chunk_index,content,content_hash,token_count,metadata,embedding,embedding_model,embedding_dim,embedded_at,created_at,updated_at';
        source_select text; chunk_select text; source_old text; source_new text; chunk_old text; chunk_new text;
        old_count bigint; new_count bigint; mismatch boolean;
begin
    select string_agg('s.'||x,','),string_agg('o.'||x,','),string_agg('n.'||x,',')
      into source_select,source_old,source_new from unnest(string_to_array(source_cols,',')) x;
    select string_agg('c.'||x,','),string_agg('o.'||x,','),string_agg('n.'||x,',')
      into chunk_select,chunk_old,chunk_new from unnest(string_to_array(chunk_cols,',')) x;
    for r in select old_family,target_family,count(*) as source_count
             from migration_source_moves group by 1,2 order by 1,2 loop
        raise notice 'Moving % sources: % -> %',r.source_count,r.old_family,r.target_family;
        execute format('drop index if exists public.%I restrict',r.target_family||'_chunks_embedding_hnsw_idx');
        execute format('insert into public.%I (%s) select %s from public.%I s join migration_source_moves m on m.source_id=s.id where m.old_family=$1 and m.target_family=$2',
            r.target_family||'_sources',source_cols,source_select,r.old_family||'_sources') using r.old_family,r.target_family;
        execute format('select count(*) from public.%I c join migration_source_moves m on m.source_id=c.source_id where m.old_family=$1 and m.target_family=$2',r.old_family||'_chunks')
            into old_count using r.old_family,r.target_family;
        execute format('insert into public.%I (%s) select %s from public.%I c join migration_source_moves m on m.source_id=c.source_id where m.old_family=$1 and m.target_family=$2',
            r.target_family||'_chunks',chunk_cols,chunk_select,r.old_family||'_chunks') using r.old_family,r.target_family;
        get diagnostics new_count = row_count;
        if old_count<>new_count then raise exception 'Chunk move count mismatch: % -> %',old_count,new_count; end if;
        execute format('select exists(select 1 from public.%I o join migration_source_moves m on m.source_id=o.id join public.%I n on n.id=o.id where m.old_family=$1 and m.target_family=$2 and row(%s) is distinct from row(%s))',
            r.old_family||'_sources',r.target_family||'_sources',source_old,source_new)
            into mismatch using r.old_family,r.target_family;
        if mismatch then raise exception 'Source content mismatch; original rows retained'; end if;
        execute format('select exists(select 1 from public.%I o join migration_source_moves m on m.source_id=o.source_id join public.%I n on n.id=o.id where m.old_family=$1 and m.target_family=$2 and row(%s) is distinct from row(%s))',
            r.old_family||'_chunks',r.target_family||'_chunks',chunk_old,chunk_new)
            into mismatch using r.old_family,r.target_family;
        if mismatch then raise exception 'Chunk/vector mismatch; original rows retained'; end if;
        execute format('delete from public.%I c using migration_source_moves m where m.source_id=c.source_id and m.old_family=$1 and m.target_family=$2',r.old_family||'_chunks') using r.old_family,r.target_family;
        execute format('delete from public.%I s using migration_source_moves m where m.source_id=s.id and m.old_family=$1 and m.target_family=$2',r.old_family||'_sources') using r.old_family,r.target_family;
        raise notice 'Moved and verified % chunks for %',new_count,r.target_family;
    end loop;
end $move$;

alter table public.rag_sources
    add column if not exists department_id text,
    add column if not exists department_name text,
    add column if not exists college_name text,
    add column if not exists department_homepage text,
    add column if not exists department_majors text[] not null default '{}';
update public.rag_sources s
set department_id=d.department_id,department_name=d.department_name,college_name=d.college_name,
    department_homepage=d.department_homepage,
    department_majors=array(select jsonb_array_elements_text(d.department_majors)),
    metadata=s.metadata || jsonb_strip_nulls(jsonb_build_object(
        'department_id',d.department_id,'department_name',d.department_name,
        'college_name',d.college_name,'department_homepage',d.department_homepage))
        || case when jsonb_array_length(d.department_majors)>0
                then jsonb_build_object('department_majors',d.department_majors) else '{}'::jsonb end
from migration_department_registry d
where d.department_id=coalesce(nullif(s.metadata->>'ingestion_dataset',''),s.metadata->>'source_dataset')
  and (s.department_id is distinct from d.department_id
       or s.department_name is distinct from d.department_name
       or s.college_name is distinct from d.college_name
       or s.department_homepage is distinct from d.department_homepage
       or s.department_majors is distinct from array(select jsonb_array_elements_text(d.department_majors))
       or s.metadata->>'department_id' is distinct from d.department_id);
alter table public.rag_sources alter column department_id set not null,alter column department_name set not null;
create index if not exists rag_sources_department_id_idx on public.rag_sources(department_id);
create index if not exists rag_sources_college_name_idx on public.rag_sources(college_name);
update public.pknu_syllabus_sources
set category='강의계획서',metadata=metadata||jsonb_build_object('category','강의계획서')
where nullif(btrim(category),'') is null;

-- Retire only the old, now-empty main-site tables. RESTRICT refuses external dependencies.
do $retire$
begin
    if to_regclass('public.pknu_student_life_chunks') is not null then
        if exists(select 1 from public.pknu_student_life_chunks) or exists(select 1 from public.pknu_student_life_sources) then
            raise exception 'Legacy student-life tables still contain rows';
        end if;
        drop table public.pknu_student_life_chunks restrict;
        drop table public.pknu_student_life_sources restrict;
    end if;
end $retire$;

-- Count conservation across the five independent families, after deleting original moved rows.
do $verify$
declare f text; ns bigint:=0; nc bigint:=0; n bigint; has_orphans boolean;
begin
    foreach f in array array['rag','rule','pknu_notice','pknu_main','pknu_syllabus'] loop
        execute format('select count(*) from public.%I',f||'_sources') into n; ns:=ns+n;
        execute format('select count(*) from public.%I',f||'_chunks') into n; nc:=nc+n;
        execute format('select exists(select 1 from public.%I c left join public.%I s on s.id=c.source_id where s.id is null)',f||'_chunks',f||'_sources') into has_orphans;
        if has_orphans then raise exception 'Orphan chunks detected in %',f; end if;
    end loop;
    if ns<>(select migration_totals.n from migration_totals where kind='sources')
       or nc<>(select migration_totals.n from migration_totals where kind='chunks') then
        raise exception 'Source/chunk count conservation failed; rolling back';
    end if;
    raise notice 'Conservation verified: % sources, % chunks',ns,nc;
end $verify$;

-- Search indexes: existing large department/rule indexes are reused.
do $indexes$
declare f text; tab text;
begin
    foreach f in array array['pknu_notice','pknu_main','pknu_syllabus','rule','rag'] loop
        raise notice 'Ensuring search indexes for %',f;
        execute format('create index if not exists %I on public.%I(status)',f||'_sources_status_idx',f||'_sources');
        execute format('create index if not exists %I on public.%I((metadata->>''source_dataset''))',f||'_sources_dataset_idx',f||'_sources');
        execute format('create index if not exists %I on public.%I(priority_score desc)',f||'_sources_priority_score_idx',f||'_sources');
        execute format('create index if not exists %I on public.%I(source_id)',f||'_chunks_source_id_idx',f||'_chunks');
        execute format('create index if not exists %I on public.%I using gin(metadata)',f||'_sources_metadata_gin_idx',f||'_sources');
        execute format('create index if not exists %I on public.%I using gin(metadata)',f||'_chunks_metadata_gin_idx',f||'_chunks');
        execute format('create index if not exists %I on public.%I using gin(content_tsv)',f||'_chunks_tsv_idx',f||'_chunks');
        execute format('create index if not exists %I on public.%I using hnsw ((embedding::extensions.halfvec(384)) extensions.halfvec_cosine_ops) with (m=16,ef_construction=64)',f||'_chunks_embedding_hnsw_idx',f||'_chunks');
        execute format('analyze public.%I',f||'_sources');
        execute format('analyze public.%I',f||'_chunks');
    end loop;
end $indexes$;

create or replace function public.match_rag_documents(
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
    from public.rag_chunks as c
    join public.rag_sources as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;

create or replace function public.match_rag_documents_lexical(
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
        from public.rag_chunks as c
        join public.rag_sources as s on s.id = c.source_id
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

create or replace function public.match_pknu_notice_documents(
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
    from public.pknu_notice_chunks as c
    join public.pknu_notice_sources as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;

create or replace function public.match_pknu_notice_documents_lexical(
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
        from public.pknu_notice_chunks as c
        join public.pknu_notice_sources as s on s.id = c.source_id
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

create or replace function public.match_pknu_main_documents(
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
    from public.pknu_main_chunks as c
    join public.pknu_main_sources as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;

create or replace function public.match_pknu_main_documents_lexical(
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
        from public.pknu_main_chunks as c
        join public.pknu_main_sources as s on s.id = c.source_id
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

create or replace function public.match_rule_documents(
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
    from public.rule_chunks as c
    join public.rule_sources as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;

create or replace function public.match_rule_documents_lexical(
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
        from public.rule_chunks as c
        join public.rule_sources as s on s.id = c.source_id
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

create or replace function public.match_pknu_syllabus_documents(
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
    from public.pknu_syllabus_chunks as c
    join public.pknu_syllabus_sources as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;

create or replace function public.match_pknu_syllabus_documents_lexical(
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
        from public.pknu_syllabus_chunks as c
        join public.pknu_syllabus_sources as s on s.id = c.source_id
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

-- Existing deployed clients can keep calling these two public RPC names during rollout.
create or replace function public.match_pknu_student_life_documents(
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
    from public.pknu_main_chunks as c
    join public.pknu_main_sources as s on s.id = c.source_id
    where s.status = 'active'
      and (s.metadata || c.metadata) @> metadata_filter
      and 1.0 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding::extensions.halfvec(384) <=> query_embedding::extensions.halfvec(384)
    limit greatest(match_count, 0);
$$;

create or replace function public.match_pknu_student_life_documents_lexical(
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
        from public.pknu_main_chunks as c
        join public.pknu_main_sources as s on s.id = c.source_id
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

notify pgrst, 'reload schema';
commit;
