"""Operational recovery of absent sources only; excludes the Excel rebuild."""
from __future__ import annotations

import collections
import datetime as dt
from dataclasses import asdict
import hashlib
import itertools
import json
from pathlib import Path
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Event

ROOT = Path(__file__).resolve().parents[2]
# Bound by the CLI for one invocation, restored in finally; never run on import.
RUN = None

from psycopg import sql
from psycopg.errors import QueryCanceled, LockNotAvailable, DeadlockDetected
from psycopg.types.json import Jsonb
from scripts.db import connect_postgres
from scripts.preprocessing.text_cleaning import has_searchable_body
from scripts.vectorization.datasets import TABLE_FAMILIES, table_family
from scripts.vectorization.recovery_control import (
    LoadOptions, PeerWorkerStopped, checkpoint_ids, write_chunk_batches, replace_with_retry,
)
from scripts.vectorization.load_to_supabase import (
    SOURCE_COLUMNS, DEPARTMENT_COLUMNS, CHUNK_COLUMNS, DEFAULT_EMBEDDING_MODEL,
    ensure_schema_ready, iter_records, prepare_row, preflight_index, record_identity,
)

from scripts.recovery.common import atomic, read, event as emit, receipt as update_receipt
from scripts.artifact_paths import reference_path, resolve_artifact_path


def now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()


def signature(path):
    s = path.stat()
    return [s.st_size, s.st_mtime_ns]


def norm(path):
    return str(path or '').replace('\\', '/').casefold()


def receipt(**updates):
    if RUN is None:
        raise RuntimeError('Use scripts.recovery --task missing --run ...')
    if not (RUN / 'receipt.json').exists():
        updates = {'created_at': now(), 'mode': 'insert_missing_only', **updates}
    update_receipt(RUN, **updates)


def snapshot():
    ids = set()
    origins = {f: set() for f in TABLE_FAMILIES}
    with connect_postgres() as c:
        c.execute('set transaction isolation level repeatable read read only')
        stamp = c.execute('select now()').fetchone()[0].isoformat()
        for family in TABLE_FAMILIES:
            with c.cursor(name='missing_snapshot_' + family) as cur:
                cur.itersize = 3000
                cur.execute(sql.SQL('select id,file_path from {}').format(sql.Identifier('public', family + '_sources')))
                for sid, path in cur:
                    ids.add(sid)
                    if path:
                        origins[family].add(norm(path))
    return ids, origins, stamp


def prepare():
    if (RUN / 'missing_sources.json').exists():
        emit('prepare_already_complete', sources=len(read(RUN / 'missing_sources.json')))
        return
    RUN.mkdir(parents=True, exist_ok=True)
    ids, origins, stamp = snapshot()
    excel_run = ROOT / 'var/recovery/excel_rebuild_20261107_032204'
    excel_ids = {record_identity({'source_slug':t['slug'], 'chunk_index':0}, t['dataset'])[0]
                 for t in read(excel_run / 'targets.json')}
    receipt(status='preparing', database_snapshot=stamp, excel_exclusion_receipt=str(excel_run.relative_to(ROOT)),
            original_expected_missing=13323, existing_source_ids_at_snapshot=len(ids))

    def collect(folder):
        dataset = folder.name
        index = folder / 'index.jsonl'
        manifest = read(folder / 'manifest.json')
        assert manifest['num_files_failed'] == 0 and not manifest.get('failures')
        assert manifest['embedding_backend'] == DEFAULT_EMBEDDING_MODEL
        original_signature = signature(index)
        delta = RUN / 'delta' / dataset
        delta.mkdir(parents=True, exist_ok=True)
        temporary = delta / 'index.partial'
        selected = {}
        seen = set()
        counts = collections.Counter()
        with index.open(encoding='utf-8-sig') as src, temporary.open('w', encoding='utf-8', newline='\n') as out:
            for line in src:
                if not line.strip():
                    continue
                match = re.search(r'\x22source_slug\x22\s*:\s*\x22([^\x22]+)\x22', line[:400])
                assert match, dataset
                slug = match[1]
                if slug not in seen:
                    seen.add(slug)
                    record = json.loads(line)
                    metadata = record.get('metadata') or {}
                    family = table_family(dataset, metadata)
                    sid = record_identity(record, dataset)[0]
                    if sid in excel_ids or str(metadata.get('source_ext', '')).lower() in ('.xls', '.xlsx'):
                        counts['excluded_excel'] += 1
                        continue
                    if sid in ids:
                        counts['already_present'] += 1
                        continue
                    original = metadata.get('source_path') or ''
                    if norm(original) in origins[family]:
                        counts['same_original_already_present'] += 1
                        continue
                    path = resolve_artifact_path(ROOT, metadata['preprocessed_path'])
                    before = signature(path)
                    doc = read(path)
                    assert signature(path) == before
                    assert doc['slug'] == slug and doc['source_path'] == original
                    if not has_searchable_body(doc) or metadata.get('used_metadata_fallback') is True or metadata.get('metadata_only') is True:
                        counts['excluded_no_body'] += 1
                        continue
                    assert family in ('rag', 'pknu_notice', 'pknu_main'), family
                    assert original and (metadata.get('doc_url') or metadata.get('attachment_url'))
                    selected[slug] = {'id':sid, 'dataset':dataset, 'family':family, 'slug':slug,
                        'preprocessed':metadata['preprocessed_path'], 'preprocessed_signature':before,
                        'source_path':original, 'source_ext':metadata.get('source_ext'),
                        'title':metadata.get('doc_title') or metadata.get('source_file'),
                        'uri':metadata.get('doc_url') or metadata.get('attachment_url'), 'chunks':[]}
                if slug not in selected:
                    continue
                record = json.loads(line)
                item = selected[slug]
                assert str(record['metadata'].get('source_ext','')).lower() not in ('.xls','.xlsx')
                assert table_family(dataset, record['metadata']) == item['family']
                digest = hashlib.sha256(record['text'].replace('\x00','').encode('utf-8')).hexdigest()
                item['chunks'].append([record['chunk_index'], digest])
                out.write(line if line.endswith('\n') else line + '\n')
                counts['chunks'] += 1
        assert signature(index) == original_signature, 'Canonical index changed during selection'
        replace_with_retry(temporary, delta / 'index.jsonl')
        atomic(delta / 'manifest.json', {'dataset':dataset, 'vectorized_at':now(),
            'embedding_backend':DEFAULT_EMBEDDING_MODEL, 'embedding_dimensions':384,
            'num_files':len(selected), 'num_files_vectorized':len(selected),
            'num_files_failed':0, 'failures':[], 'num_chunks':counts['chunks'],
            'index_path':(delta / 'index.jsonl').relative_to(ROOT).as_posix(),
            'selection_only':True, 'canonical_signature':original_signature})
        emit('selected_dataset', dataset=dataset, sources=len(selected), chunks=counts['chunks'])
        return dataset, list(selected.values()), dict(counts)

    folders = [p for p in sorted((ROOT / 'vectorized').iterdir()) if p.is_dir() and (p/'index.jsonl').is_file()]
    sources = []
    total = collections.Counter()
    datasets = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for dataset, entries, counts in pool.map(collect, folders):
            sources.extend(entries)
            total.update(counts)
            datasets[dataset] = counts
    assert len({e['id'] for e in sources}) == len(sources)
    assert len({(e['family'],norm(e['source_path'])) for e in sources}) == len(sources)
    for e in sources:
        assert len({i for i, _ in e['chunks']}) == len(e['chunks'])
    atomic(RUN / 'missing_sources.json', sources)
    receipt(status='selected', selection=dict(total), selected_sources=len(sources),
            selected_chunks=sum(len(e['chunks']) for e in sources), datasets=datasets)
    emit('selection_complete', sources=len(sources), chunks=sum(len(e['chunks']) for e in sources), exclusions=dict(total))


def copy_stage(cur, table, stage, columns, rows):
    names = sql.SQL(',').join(map(sql.Identifier, columns))
    cur.execute(sql.SQL('create temporary table if not exists {} on commit delete rows as select {} from {} with no data').format(
        sql.Identifier(stage), names, sql.Identifier('public', table)))
    cur.execute(sql.SQL('truncate {}').format(sql.Identifier('pg_temp', stage)))
    with cur.copy(sql.SQL('copy {} ({}) from stdin').format(sql.Identifier('pg_temp', stage), names)) as copy:
        for row in rows:
            copy.write_row(tuple(row[k] for k in columns))
    return names


def load(options=None):
    options = options or LoadOptions()
    sources = read(RUN / 'missing_sources.json')
    by_id = {e['id']:e for e in sources}
    if len(by_id) != len(sources) or any(str(e.get('source_ext','')).lower() in ('.xls','.xlsx') for e in sources):
        raise ValueError('Selection contains duplicate IDs or excluded Excel sources')
    progress_path = RUN / 'load.json'
    progress = read(progress_path) if progress_path.exists() else {'status':'preflight', 'started_at':now(), 'completed':[], 'concurrently_present':[]}
    done = checkpoint_ids(progress, by_id)
    progress.update(status='preflight', options=asdict(options))
    atomic(progress_path, progress)
    receipt(status='preflighting', load_options=asdict(options))
    if done:
        _verify_sources([by_id[sid] for sid in sorted(done)], label='checkpoint')
        emit('checkpoint_verified', sources=len(done))
    selected_datasets = sorted({e['dataset'] for e in sources})
    # Validate every selected index before the first database write.
    for dataset in selected_datasets:
        assert preflight_index(RUN / 'delta' / dataset / 'index.jsonl', dataset) == DEFAULT_EMBEDDING_MODEL
        emit('preflight_passed', dataset=dataset)
    progress['status'] = 'loading'
    atomic(progress_path, progress)
    receipt(status='loading', write_policy='Insert sources/chunks only; existing IDs are never updated; Excel and unrelated data untouched')
    started = time.monotonic()
    guard = Lock()
    stop = Event()
    def worker(assigned):
        try:
            with connect_postgres(autocommit=True) as conn:
                for family in {e['family'] for e in sources}:
                    ensure_schema_ready(conn, family + '_sources', family + '_chunks')
                for dataset in assigned:
                    pending = []
                    pending_chunks = 0

                    def commit():
                        nonlocal pending, pending_chunks
                        if not pending:
                            return
                        family = pending[0][0]['family']
                        assert all(e['family'] == family for e, _ in pending)
                        source_rows = []
                        chunk_rows = []
                        for entry, prepared in pending:
                            source = prepared[0]['source']
                            source['metadata'].obj['selective_ingestion_receipt'] = reference_path(RUN, ROOT)
                            source_rows.append(source)
                            chunk_rows.extend(r['chunk'] for r in prepared)
                        with conn.transaction(), conn.cursor() as cur:
                            cur.execute('select set_config(%s,%s,true)', ('statement_timeout', str(options.statement_timeout_seconds)+'s'))
                            cur.execute('set local lock_timeout = \'10s\'')
                            columns = SOURCE_COLUMNS + DEPARTMENT_COLUMNS if family == 'rag' else SOURCE_COLUMNS
                            names = copy_stage(cur, family+'_sources', 'missing_sources_stage_'+family, columns, source_rows)
                            cur.execute(sql.SQL('insert into {} ({}) select {} from {} on conflict do nothing returning id').format(
                                sql.Identifier('public', family+'_sources'), names, names, sql.Identifier('pg_temp','missing_sources_stage_'+family)))
                            inserted = {r[0] for r in cur.fetchall()}
                            rows = [r for r in chunk_rows if r['source_id'] in inserted]
                            # Bound each indexed INSERT while retaining one transaction
                            # for all chunks of every document in the source group.
                            def write_part(part):
                                if stop.is_set():
                                    raise PeerWorkerStopped('Another ingestion worker failed')
                                names = copy_stage(cur, family+'_chunks', 'missing_chunks_stage_'+family, CHUNK_COLUMNS, part)
                                cur.execute(sql.SQL('insert into {} ({}) select {} from {} returning id').format(
                                    sql.Identifier('public', family+'_chunks'), names, names, sql.Identifier('pg_temp','missing_chunks_stage_'+family)))
                                assert len(cur.fetchall()) == len(part)

                            def retry_part(error, attempt, size):
                                if stop.is_set():
                                    raise PeerWorkerStopped('Another ingestion worker failed')
                                with guard:
                                    event={'at':now(),'family':family,'type':type(error).__name__,
                                           'message':str(error),'attempt':attempt,'next_batch_size':size}
                                    progress.setdefault('retry_events',[]).append(event)
                                    atomic(progress_path,progress)
                                    emit('retry_chunk_statement',**event)
                                time.sleep(1)

                            write_chunk_batches(rows,batch_size=options.chunk_batch_size,
                                max_retries=options.max_batch_retries,savepoint=conn.transaction,
                                write=write_part,retryable=(QueryCanceled,LockNotAvailable,DeadlockDetected),
                                on_retry=retry_part)
                            if stop.is_set():
                                raise PeerWorkerStopped('Recovery interrupted before source commit')
                        # Update checkpoints only after the entire source group commits.
                        with guard:
                            for entry, _ in pending:
                                sid = entry['id']
                                progress['completed' if sid in inserted else 'concurrently_present'].append(sid)
                                done.add(sid)
                            atomic(progress_path, progress)
                            emit('loading', sources=len(progress['completed']), concurrent=len(progress['concurrently_present']), total=len(sources), seconds=round(time.monotonic()-started,1))
                        pending = []
                        pending_chunks = 0

                    for slug, records in itertools.groupby(iter_records(RUN/'delta'/dataset/'index.jsonl'), key=lambda r:r['source_slug']):
                        if stop.is_set():
                            raise PeerWorkerStopped('Another ingestion worker failed')
                        sid = record_identity({'source_slug':slug, 'chunk_index':0}, dataset)[0]
                        if sid in done:
                            continue
                        entry = by_id[sid]
                        assert signature(resolve_artifact_path(ROOT, entry['preprocessed'])) == entry['preprocessed_signature']
                        prepared = [prepare_row(r, DEFAULT_EMBEDDING_MODEL, dataset) for r in records]
                        assert len(prepared) == len(entry['chunks'])
                        if pending and (pending[0][0]['family'] != entry['family'] or len(pending)>=options.source_batch_size or pending_chunks+len(prepared)>options.transaction_chunk_limit):
                            commit()
                        pending.append((entry,prepared))
                        pending_chunks += len(prepared)
                    commit()
        except Exception as exc:
            with guard:
                progress.setdefault('errors',[]).append({'at':now(),'type':type(exc).__name__,'message':str(exc),'datasets':assigned})
                atomic(progress_path,progress)
                emit('worker_failed',error_type=type(exc).__name__,message=str(exc))
            stop.set()
            raise
    weights = collections.Counter()
    for entry in sources:
        if entry['id'] not in done:
            weights[entry['dataset']] += len(entry['chunks'])
    assignments = [[] for _ in range(options.workers)]
    totals = [0 for _ in range(options.workers)]
    for dataset in sorted(weights,key=lambda name:(-weights[name],name)):
        index = min(range(options.workers),key=lambda i:totals[i])
        assignments[index].append(dataset)
        totals[index] += weights[dataset]
    receipt(workers=options.workers)
    emit('parallel_loading',workers=options.workers,remaining_chunks=totals)
    errors=[]
    with ThreadPoolExecutor(max_workers=options.workers) as pool:
        futures=[pool.submit(worker,assigned) for assigned in assignments]
        try:
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    errors.append(error)
        except KeyboardInterrupt:
            stop.set()
            for future in futures:
                future.cancel()
            raise
    if errors:
        raise next((error for error in errors if not isinstance(error,PeerWorkerStopped)),errors[0])
    progress.update(status='loaded', finished_at=now())
    atomic(progress_path, progress)
    receipt(status='loaded_pending_verification', inserted_sources=len(progress['completed']), concurrently_present_sources=len(progress['concurrently_present']))
    emit('load_complete', inserted=len(progress['completed']), concurrent=len(progress['concurrently_present']))


def _verify_sources(sources, label='full'):
    checks = {}
    with connect_postgres() as conn:
        conn.execute('set transaction isolation level repeatable read read only')
        conn.execute('set local statement_timeout = \'180s\'')
        for family in sorted({e['family'] for e in sources}):
            selected = [e for e in sources if e['family']==family]
            observed = {}
            source_rows = {}
            for offset in range(0,len(selected),400):
                ids = [e['id'] for e in selected[offset:offset+400]]
                q=sql.SQL('select id,status,file_path,metadata->>%s from {} where id=any(%s)').format(sql.Identifier('public',family+'_sources'))
                for sid,status,path,dataset in conn.execute(q,('ingestion_dataset',ids)):
                    source_rows[sid]=(status,path,dataset)
                q=sql.SQL('''select source_id,chunk_index,content_hash,encode(sha256(convert_to(content,'UTF8')),'hex'),
                    embedding is not null and embedding_dim=384 and embedding_model=%s from {} where source_id=any(%s)''').format(sql.Identifier('public',family+'_chunks'))
                for sid,index,stored,actual,vector_ok in conn.execute(q,(DEFAULT_EMBEDDING_MODEL,ids)):
                    assert stored==actual and vector_ok, (sid,index,'invalid hash or embedding')
                    assert index not in observed.setdefault(sid,{}), (sid,'duplicate index')
                    observed[sid][index]=actual
            for e in selected:
                assert source_rows.get(e['id']) == ('active',e['source_path'],e['dataset']), (e['id'],'source identity/status/provenance mismatch')
                assert observed.get(e['id']) == dict(e['chunks']), (e['id'],'chunk count or content mismatch')
            checks[family]={'sources':len(selected),'chunks':sum(len(e['chunks']) for e in selected),'source_ids':'passed','chunk_counts':'passed','actual_sha256':'passed','embedding_metadata':'passed'}
            emit('verified_family',verification_scope=label,family=family,**checks[family])
    return checks


def verify():
    checks = _verify_sources(read(RUN/'missing_sources.json'))
    receipt(status='database_verified_pending_search', verification=checks, verified_at=now())


def priorities():
    from scripts.priority.department.priority import build_rule_feature_set, build_reference_feature_set, calculate_department_priority
    from scripts.priority.rag.priority import calculate_main_priority

    def contents(families):
        with connect_postgres() as c:
            c.execute('set transaction read only')
            with c.cursor(name='missing_priority_reference') as cur:
                queries = [sql.SQL('select c.content from {} c join {} s on c.source_id=s.id where s.status=\'active\'').format(
                    sql.Identifier('public', f+'_chunks'), sql.Identifier('public', f+'_sources')) for f in families]
                cur.execute(sql.SQL(' union all ').join(queries))
                for row in cur:
                    yield str(row[0] or '')

    emit('priority_references')
    rules = build_rule_feature_set(contents(('rule',)))
    main = build_reference_feature_set(contents(('pknu_notice','pknu_main')))
    # Only rows created by this recovery receive priority updates.
    owned = set(read(RUN/'load.json')['completed'])
    sources = read(RUN/'missing_sources.json')
    updated = 0
    with connect_postgres(autocommit=True) as conn:
        for dataset in sorted({e['dataset'] for e in sources}):
            batches = collections.defaultdict(list)
            for slug, group in itertools.groupby(iter_records(RUN/'delta'/dataset/'index.jsonl'), key=lambda r:r['source_slug']):
                first = next(group)
                sid = record_identity(first,dataset)[0]
                if sid not in owned:
                    continue
                source = prepare_row(first,DEFAULT_EMBEDDING_MODEL,dataset)['source']
                family = table_family(dataset,first['metadata'])
                content = '\n\n'.join(itertools.chain((first['text'],),(r['text'] for r in group)))
                if family=='rag':
                    score,details = calculate_department_priority(content,rules,main_features=main,metadata=source['metadata'].obj,title=source['title'] or '')
                else:
                    score,details = calculate_main_priority(dataset=family,content=content,rule_features=rules,metadata=source['metadata'].obj,title=source['title'] or '')
                batches[family].append((score,Jsonb(details),sid,reference_path(RUN, ROOT)))
            for family, rows in batches.items():
                with conn.transaction(),conn.cursor() as cur:
                    cur.executemany(sql.SQL('update {} set priority_score=%s,priority_details=%s,priority_updated_at=now() where id=%s and metadata->>\'selective_ingestion_receipt\'=%s').format(
                        sql.Identifier('public',family+'_sources')),rows)
                    assert cur.rowcount==len(rows)
                updated += len(rows)
            emit('priorities_updated',dataset=dataset,total=updated)
    assert updated==len(owned)
    receipt(priorities_updated=updated)


def main(argv=None):
    from scripts.recovery.cli import legacy_main
    return legacy_main('missing', argv)


if __name__ == '__main__':
    raise SystemExit(main())
