# Supabase schema and ingestion

File numbers are unique: 000 is read-only inspection, 001-015 are historical changes, 016-017 prepare the previous ingestion schema, and 018 establishes the five current table families. Preserve historical numbering. Do not blindly rerun the folder: historical scripts include table removal and permanent deletion of inactive sources. Do not replay 016/017 after 018: they describe the retired student-life tables and old RPC definitions.

## Cleanup and replay policy

This cleanup is a repository-only refactor. An existing database needs **no SQL to apply** for it.
`017` and `018` use local templates for repeated RPC definitions, retaining their signatures, defaults,
query bodies, function settings and table routing. The repeated transaction-local `search_path` in `017`
is removed. Table/column DDL, constraints, indexes, triggers, policies, grants and data migration statements
are unchanged. In `018`, both legacy student-life RPC names still read `pknu_main_*`.

Keep each numbered file self-contained and retain its version. Definitions superseded by a later file
are necessary for historical replay and are not instructions to rerun them on the current database.
Do not delete versions, renumber files, or repair the migration ledger for this cleanup.

| Files | Purpose / current use |
|---|---|
| `000` | Read-only inspection; does not apply schema changes |
| `001`–`002`, `004`–`009`, `011`–`012` | Earlier RAG tables, indexes and RPC revisions; retain for historical replay |
| `003`, `010`, `013`, `015` | Chat history, signup restriction and feedback; still needed independently of RAG RPC updates |
| `014` | Permanent inactive-source deletion and VACUUM; maintenance history, never a normal setup prerequisite |
| `016` | Previous four-family ingestion schema; prerequisite for the one-time `018` transition |
| `017` | RPCs for the previous four families; `018` replaces all of them |
| `018` | Five-family transition, semantic RPCs and legacy compatibility RPCs |
| `019`–`021` | Intermediate lexical RPC revisions; `022` replaces both canonical overloads |
| `022` | Latest canonical lexical RPC definitions and additional lookup indexes in this repository |

For a **manual** transition from the four-family schema directly to the latest repository definitions,
`016` → `018` → `022` avoids installing RPC bodies that are immediately replaced. This is not a fresh
database bootstrap: earlier auth/chat setup and any necessary legacy data conversion must already be
complete. If `018` is already applied, do not rerun `016` or `018`; only an independently reviewed,
unapplied update such as `022` may be needed. If `022` is already applied, no replay is needed.
When using a migration runner instead of manual SQL, preserve its normal numbered history and ledger;
do not skip or mark versions as applied based on the manual shortcut.

## Current table families

| Content | Sources | Chunks | Semantic RPC |
|---|---|---|---|
| Department/college websites | `rag_sources` | `rag_chunks` | `match_rag_documents` |
| Main-site notices and their attachments | `pknu_notice_sources` | `pknu_notice_chunks` | `match_pknu_notice_documents` |
| All other main-site content, including student life | `pknu_main_sources` | `pknu_main_chunks` | `match_pknu_main_documents` |
| Regulations | `rule_sources` | `rule_chunks` | `match_rule_documents` |
| Syllabi | `pknu_syllabus_sources` | `pknu_syllabus_chunks` | `match_pknu_syllabus_documents` |

Each RPC also has a `_lexical` variant. Department sources have `department_id` and `department_name` (required), plus `college_name`, `department_homepage` and `department_majors`. These attributes are exposed in search metadata; `metadata_filter={"department_id":"ce"}` limits department search. Existing category/subcategory fields remain.

023 and 024 were written as 021 and 022 on a parallel branch and applied to production on 2026-11-24, after the e5 files 021 and 022. They were renumbered when the branches merged; they touch only the lexical RPCs and source indexes, so their order relative to the e5 files does not matter.

`023_lexical_late_hydration.sql` updates only the five lexical RPC families (two- and three-argument signatures).
It ranks IDs/scores with the existing per-source cap before loading full text/metadata, and avoids copying full
source metadata into the scoped source-ID bitmap. Source-only filters retain chunk-override semantics;
other metadata filters retain the merged-JSON check. It preserves rows, vectors, indexes, grants and timeouts.
Queries are parsed once, and parameterized dynamic SQL uses actual query/filter values instead of the
cached SQL-function plan. `ts_rank_cd`, vector fusion and cross-encoder reranking are preserved.
In unfiltered searches, active-source checks occur after the
per-source window (but before the final limit), avoiding repeated source lookups for one long document.
Apply it as one transaction after 020; do not replay older schema migrations. Pair it with the backend's
specific graduation-credit/deferral query variants so broad words do not match tens of thousands of chunks.

`024_bound_search_candidates.sql` preserves the lexical RPC signatures and caps the pool scored for common
terms. The per-source limit of three still applies to ranked candidates. A broad query can omit matches
beyond the pool, so compare recall as well as latency on the same questions. It also adds active-source title
indexes for substring and exact menu lookup, plus indexes for syllabus codes and faculty profile categories.
Apply after 023 in a maintenance window. Building the indexes can block ingestion writes. Verify both overloads
and the selective/very common-term cases on a representative database before rollout. The migration leaves
rows, vectors, compute size and global timeout settings untouched. To restore the previous ranking functions,
reapply 023. The added indexes can continue serving the exact lookups.

`018_split_dataset_tables.sql` is a one-time maintenance migration for a DB with 016 applied; the RPC-only 017 is optional on the manual direct path. It locks ingestion tables, copies sources/chunks with unchanged IDs and embeddings, checks exact copied values and total row counts, deletes the moved originals, backfills department attributes from the registry snapshot, retires empty `pknu_student_life_*` tables and builds destination search indexes. The large department/rule HNSW indexes are reused. Everything commits atomically; errors roll back the migration. It never changes auth/chat tables or local `files/` data.

Routing uses original `metadata.source_dataset`, including attachments where `is_notice=false`. Original `ingestion_dataset` stays unchanged to preserve stable upsert keys. Future loads use the live department registry and the same routing. Keep an operational backup and run this migration with ingestion stopped. New tables retain RLS and grant backend `service_role` access; browser policies are not added.

The two old student-life public RPC names temporarily query `pknu_main_*` for existing deployed clients. Physical student-life tables are removed. Update a deployed backend's explicit `RPC_NAMES` override to the five canonical names and redeploy to include syllabus search.

## e5 embeddings (021)

`021_e5_embeddings.sql` adds a 768-dim `intfloat/multilingual-e5-base` vector per chunk in side tables `<family>_chunks_e5` (id → `<family>_chunks.id`, on delete cascade; `content_hash`; `halfvec(768)`), their empty HNSW indexes, and `match_<family>_documents_e5`. Nothing existing is altered, so it can be applied while the service runs on the 384-dim functions.

1. Apply `021_e5_embeddings.sql`.
2. On the GPU server: `python -m scripts.vectorization.backfill_e5 --count-only`, a trial with `--families rule --limit 500`, then the full run. It can be stopped and restarted; rerun it after crawls until the loader writes e5 vectors itself.
3. Check `pg_indexes` for the five `*_chunks_e5_embedding_hnsw_idx` indexes and `EXPLAIN` one `match_*_documents_e5` call (expect an index scan on the side table).
4. Switch the backend with the five settings in `backend/.env.example` (`VECTOR_RPC_SUFFIX=_e5` …) and compare the regression eval. Setting `VECTOR_RPC_SUFFIX` back to empty returns to the 384-dim search.

`022_e5_search_plan.sql` makes the functions read the side table through its HNSW index. `025_e5_small_department_search.sql` adds an exact path for an `ingestion_dataset` filter: a department with up to 10000 active chunks is searched chunk by chunk instead of through the index, which rejected most rows for small departments (43-65 s on Small compute).

A side row counts only while its `content_hash` equals the chunk's; an edited chunk is left out of e5 search until the backfill embeds it again.

## Execution order

1. Run `000_inspect_database.sql` for read-only schema, index, RPC and RLS inspection.
2. On a four-family DB that has not yet applied 016, apply `016_prepare_dataset_ingestion.sql`. For a manual transition directly to 018, the superseded RPC-only `017_update_search_functions.sql` is unnecessary.
3. Apply `018_split_dataset_tables.sql` once to establish the five families, move existing data and build destination indexes. An already-migrated DB needs only the normal loader commands below.
4. In a maintenance window, run `--prepare-bulk` once to remove existing named search indexes, if present.
5. Load completed datasets with the COPY-based Python loader.
6. After every ingestion terminal has finished, run `--finalize-indexes` once, then audit the result.

These steps describe the historical schema/ingestion transition, not actions required by this cleanup.
For the latest canonical lexical RPCs and lookup indexes, see `022` above; intermediate `019`–`021`
are unnecessary on the manual direct path. Do not rerun migrations as part of normal ingestion.

The schema script preserves existing rows and RLS policies. RLS is enabled for newly created tables only; browser access policies are not created automatically. Embeddings must have 384 dimensions.
`016` does not remove indexes created by historical migrations. `--prepare-bulk` explicitly removes only the named HNSW, full-text GIN and metadata GIN indexes; rows, PK/UNIQUE and foreign keys remain. Search may be slow until finalization. `--finalize-indexes` uses standard CREATE INDEX and may block writes. Run index maintenance separately from ingestion terminals. Index statements use a connection-local 30-minute timeout (change with `--index-timeout-seconds`); lock acquisition is limited to 10 seconds. No global timeout is changed.

For a long maintenance operation, `--dataset all --finalize-indexes --index-timeout-seconds 0` disables the statement timeout only inside each maintenance transaction. Already-created indexes are retained. This prevents cancellation by this timer but does not resolve memory limits, disk I/O throttling, disk exhaustion, or disconnected clients. Confirm resources before restarting a large HNSW build; a canceled build cannot resume its partial graph.
Search function definitions are replaced; compare any custom production changes first.

## halfvec HNSW 인덱스

`--finalize-indexes`는 다섯 테이블 계열 모두 아래 형태의 검색 인덱스를 생성합니다.
`halfvec`를 지원하는 pgvector 0.7.0 이상이 필요합니다.

```sql
create index rag_chunks_embedding_hnsw_idx
on public.rag_chunks using hnsw
((embedding::extensions.halfvec(384)) extensions.halfvec_cosine_ops)
with (m = 16, ef_construction = 64);
```

HNSW 구축 파라미터는 기존 기본값 `m=16`, `ef_construction=64`를 유지합니다.
검색 시 `hnsw.ef_search`는 변경하지 않습니다. 인덱스 구축 트랜잭션에는 `SET LOCAL maintenance_work_mem='512MB'`를 적용하며 전역 설정은 바꾸지 않습니다. 원본 컬럼은 `vector(384)`로
보관하고 인덱스에서만 `halfvec(384)`로 변환하므로, 기존 로컬 벡터 파일·COPY 적재·질문 임베딩을
다시 만들 필요가 없습니다. 인덱스에는 그래프도 포함되므로 전체 크기가 정확히 절반이 되는 것은 아닙니다.

`018_split_dataset_tables.sql`의 다섯 의미 검색 RPC는 양쪽 벡터를 `halfvec(384)`로 변환해
정렬합니다. 입력 인자와 반환 형식은 유지하고, 반환 유사도와 최소 유사도 필터는 원본 `vector` 거리로
계산합니다. 기존 RPC가 원본 `vector`로 정렬하면 이 표현식 인덱스를 사용하지 못하므로 함께 적용해야 합니다.
어휘 검색 RPC와 GIN 인덱스는 그대로 사용합니다.

### 이미 적재한 DB의 전환

이번 다섯 분류 전환은 `018_split_dataset_tables.sql`만 적용하면 됩니다. 아래 수동 인덱스 교체 절차는
구식 `vector` HNSW가 남아 있을 때만 사용하며, 이미 올바른 `halfvec` 인덱스를 삭제할 필요가 없습니다.

코드를 수정해도 실행 중인 기존 `vector` 인덱스 작업이나 DB에 적용된 RPC가 자동 전환되지는 않습니다.
전환할 때는 유지보수 시간에 기존 인덱스 생성 작업을 먼저 종료하세요. 취소한 구축 작업은 이어서 실행할 수 없습니다.
이미 데이터 적재가 완료됐다면 다음 순서로 인덱스와 RPC만 전환하며 데이터를 다시 적재하지 않습니다.

1. 기존 HNSW 인덱스만 제거합니다. 원본 행과 GIN 인덱스는 유지합니다.

   ```sql
   drop index if exists public.rag_chunks_embedding_hnsw_idx restrict;
   drop index if exists public.rule_chunks_embedding_hnsw_idx restrict;
   drop index if exists public.pknu_notice_chunks_embedding_hnsw_idx restrict;
   drop index if exists public.pknu_main_chunks_embedding_hnsw_idx restrict;
   drop index if exists public.pknu_syllabus_chunks_embedding_hnsw_idx restrict;
   ```

2. 프로젝트 루트에서 인덱스를 생성합니다. `0`은 이 유지보수 작업의 statement timeout만 해제합니다.

   ```powershell
   .\.venv\Scripts\python.exe -m scripts.vectorization.load_to_supabase --dataset all --finalize-indexes --index-timeout-seconds 0
   ```

3. 다섯 분류를 처음 도입할 때는 `018_split_dataset_tables.sql`에서 RPC까지 적용합니다. 이후 `016`/`017`을 재실행하지 마세요.
4. 아래 명령으로 인덱스의 유효성·표현식·파라미터를 확인합니다.

   ```powershell
   .\.venv\Scripts\python.exe -m scripts.vectorization.check_hnsw_indexes
   ```

같은 이름의 기존 인덱스가 `vector` 방식이거나 구축 파라미터가 다르면 적재기는 자동 삭제하거나
완료로 처리하지 않고 전환 안내와 함께 중단합니다. 올바른 기존 `halfvec` 인덱스는 재사용합니다.
반정밀도 인덱스도 전체 그래프가 메모리에 들어가지 않으면 디스크 I/O가 발생할 수 있습니다.

## Python ingestion

Run from the repository root. Separate index maintenance from loading:

```powershell
.\.venv\Scripts\python.exe -m scripts.vectorization.load_to_supabase --dataset all --prepare-bulk
.\.venv\Scripts\python.exe -m scripts.vectorization.load_to_supabase --dataset all --batch-size 200
.\.venv\Scripts\python.exe -m scripts.vectorization.load_to_supabase --dataset all --finalize-indexes
```

Check the completed manifest and its failures before loading index.jsonl. Never load an index still being generated.
Dataset paths and table routing are maintained in scripts/vectorization/datasets.py.
Each bounded batch is copied to connection-local temporary staging tables, then merged with one source upsert and one chunk upsert. Unchanged rows are not updated merely to change timestamps. Failed COPY/merge operations roll back the current batch; earlier committed batches remain. Source identity and backend RPC contracts are unchanged.

Optional empty metadata is removed recursively; false/zero, list positions, and the schema's `published_at`/`crawl` fields are preserved. Records with empty body text or metadata-only extraction fallback are excluded. For old vectors, the loader checks `metadata.preprocessed_path` against the existing preprocessing file, so re-embedding is unnecessary. Missing provenance files stop validation instead of silently ignoring the check.
On loading, already-stored chunks with the same stable IDs as excluded input records are deleted; source rows and originals remain for provenance. This is scoped to the selected dataset's input, not a global purge. A failed OCR attempt does not exclude a document that still has real extracted text. Manifests retain the original vector count; compare database counts with the loader's accepted/excluded counts.
The loader performs source deduplication and priority updates; --replace-sources deletes existing chunks for the affected sources. Review scope and backups before use.
No separate JSONB ingestion RPC is required. `--dataset all` validates every collected dataset before writing, loads regulations first, and refreshes priorities once per table family after loading. A failure stops the run; batches already committed remain in the database. Rerunning upserts the same stable keys.

## Verification

```powershell
.\.venv\Scripts\python.exe supabase\audit\audit_db.py
```

The audit retains existing diagnostics and checks all five families for source counts, chunks by dataset/model, empty content, dimension mismatches, orphan chunks and duplicate chunk positions. It runs in a read-only transaction.
Orphans and dimension mismatches should be zero; duplicate positions should return no rows. Review empty content and missing dataset metadata against the source data.
`--finalize-indexes` also runs ANALYZE. Auditing itself remains read-only.

SQL parsing does not replace execution testing against PostgreSQL. COPY/upsert, unchanged-row detection, replacement rollback and index maintenance were tested in isolated tables and fully rolled back. Migration 018 separately moves production data when explicitly applied; the loader refactor tests themselves do not change production data.

- [Vectorization](../../scripts/vectorization/README.md)
- [Database commands](../../docs/commands/VECTOR_DATABASE.md)
