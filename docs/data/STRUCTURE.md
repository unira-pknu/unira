# Data Structure

`files/`에는 수집 원본과 그 수집 이력만 보관합니다. 전처리·벡터·복구 자료는 프로젝트 루트의 별도 폴더를 사용합니다.
각 데이터 폴더는 `.gitignore` 대상입니다.

## Top-Level Layout

```text
files/
  department/      # 학과 수집 원본·첨부·수집 이력
  pknu_main/       # 본교 수집 원본·첨부·수집 이력
  pknu_syllabus/   # 강의계획서 원본·다운로드 이력
  rule/            # 규정 원본·첨부·수집 이력
preprocessed/      # <dataset>/ 전처리 문서
vectorized/        # <dataset>/ 벡터·인덱스
var/
  recovery/        # 복구 계획·변경 전 백업·재개 체크포인트
  reviewed/        # 별도 검토 산출물
  storage_paths.json # 이동한 기존 자료의 경로 호환 정보
```

All crawler datasets now use the same generated storage contract:

```text
files/<dataset>/
  state.json
  state.json.bak
  output/
    json/<category>/<slug>.json
    html/<category>/<slug>.html
    files/<category>/<slug>/<attachment>
    deleted/<category>/<slug>.json
    deleted/<category>/<slug>/page.html
    deleted/<category>/<slug>/files/<attachment>
    runs/<run_id>.json
```

`state.json` has `schema_version`, `dataset`, `updated_at`, and `items`. Each
item contains at least `source_id`, `slug`, `content_hash`, `last_seen_at`,
`miss_count`, and `status`, while retaining dataset-specific keys. State is
written to a temporary file in the same directory, flushed, and atomically
replaced. Before replacement, a valid previous state is copied to
`state.json.bak`. If the current state is corrupt, the backup is loaded; if both
are invalid the crawler fails instead of silently resetting state.

Legacy root state files are detected automatically and copied into the new
location without deleting the source or overwriting an existing target:

- `state.json` → `files/ce/state.json`
- `state_pknu_notice.json` → `files/pknu_notice/state.json`
- `state_pknu_student_life.json` → `files/pknu_student_life/state.json`

Review the migration without changing files (the default), or apply it:

```powershell
python scripts/migrations/crawler_storage.py --dataset all
python scripts/migrations/crawler_storage.py --dataset all --apply
```

Existing output trees are not bulk-moved. Crawlers write the common paths and
the shared lookup helper can search explicitly supplied legacy JSON roots. This
keeps path migration opt-in and prevents user files from being overwritten.

Deleted documents are archived, not removed. JSON moves to
`deleted/<category>/<slug>.json`; related HTML and attachment directories move
under `deleted/<category>/<slug>/`. Archival performs a collision preflight and
stops if any destination already exists.

To roll back a state migration, stop the crawler, retain the generated
`files/<dataset>/state.json` for inspection, and restore either the untouched
legacy root state or `state.json.bak`. Output rollback consists only of pointing
readers at the prior output root because automatic output migration is not
performed.

## Crawler Document Schema 1.0

The authoritative JSON Schema is
`schemas/crawler-document-1.0.schema.json`. Required document fields are:

```text
schema_version, id, slug, source_site, source_dataset, source_id, url, title,
author, category, subcategory, type, tags, published_at, updated_at,
effective_at, content, content_hash, content_source, attachments, crawl,
metadata
```

Readers dispatch on `schema_version`. Version `1.0` is validated directly;
documents without a version are converted in memory. Unknown versions fail
explicitly. Legacy conversion uses the canonical field first and then these
fallbacks:

| Canonical field | Legacy fallback | New crawler output |
| --- | --- | --- |
| `published_at` | `date` | legacy field removed |
| `crawl.crawled_at` | `crawled_at` | legacy field removed |
| `content` | `page_content`, then `html_text` | duplicate rule fields removed when represented |
| `attachments[].text` | `file_preview_texts`, `attachment_texts` | legacy arrays removed when represented |
| absolute `source_site` | origin of document `url` | always absolute |
| standard `type` | rule `hak`, `gyu`, `bylaw_guideline` | raw value kept in `metadata.source_type` |
| project-relative `saved_path` | absolute path under project root | external absolute path retained only in attachment metadata |

Numeric source fields are converted from digit-only strings when their meaning
is numeric. Information-bearing legacy text is not removed unless it is present
in canonical `content` or attachment text.

Document migration is dry-run by default:

```powershell
python scripts/migrations/crawler_documents.py --dataset pknu_notice
python scripts/migrations/crawler_documents.py --dataset pknu_notice --apply
```

The report compares document counts, normalized content, old/new content hashes,
and attachment counts. Apply mode first writes `<name>.json.legacy.bak`, refuses
backup collisions, and atomically replaces the JSON only after all loss checks
pass.

## Crawler Run Results

Every crawler writes one run result to:

```text
files/<source_dataset>/output/runs/<run_id>.json
```

The shared `CrawlStats` counters are `discovered`, `requested`, `new`, `updated`,
`unchanged`, `deleted`, `skipped`, `failed`, `attachments_discovered`,
`attachments_downloaded`, and `attachments_failed`. `unchanged` means that a
document was requested and its content hash did not change. `skipped` means the
request or processing was omitted because of state, a limit, or a CLI option.

Run status is `success` when no document or attachment failed,
`partial_success` when processing continued after one or more item failures,
`failed` when the run could not be initialized, and `cancelled` on interruption.
CLI exit codes are 0 for `success`, `partial_success`, and `cancelled`, 1 for
`failed`, and argparse retains exit code 2 for invalid CLI usage. The final
console log emits the same JSON object with the `RUN_RESULT` prefix.

### Crawler Logs

Crawler logs are written as UTF-8 to both human-readable and machine-readable files:

```text
logs/crawlers/<dataset>.log
logs/crawlers/<dataset>.jsonl
```

Console and `.log` records use
`<timestamp> <level> <dataset> <event> key=value ...`. Each JSONL line is an
independent JSON object containing at least `timestamp`, `level`, `dataset`,
`run_id`, and `event`. Stable events include `run_started`, `run_finished`,
`section_started`, `section_finished`, `list_fetched`, `document_discovered`,
`document_saved`, `document_unchanged`, `document_skipped`, `document_deleted`,
`document_failed`, `attachment_downloaded`, `attachment_failed`, `state_loaded`,
`state_saved`, `retry_scheduled`, and `request_failed`.

Passwords, tokens, cookies, authorization values, API keys, and sensitive URL
query values are replaced with `***REDACTED***`. Stack traces are emitted for
ERROR/CRITICAL records and in DEBUG mode. Existing progress messages remain
visible and are assigned the fallback `log_message` event.

## CE Dataset

Source crawler:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\engine.py --dataset ce --once
```

학과 홈페이지와 section 정보는 `scripts/crawlers/departments/registry.json`에 있습니다. CMS별 메뉴·목록·상세·정적 페이지·첨부 링크 파싱은 `scripts/crawlers/departments/adapters/`가 담당하고, `engine.py`는 공통 저장·상태·통계·로그 실행 흐름을 담당합니다. registry의 `adapter`를 생략하면 `numeric_cms`가 사용됩니다.

Raw output:

```text
files/ce/output/json/<category>/<slug>.json
files/ce/output/html/<category>/<slug>.html
files/ce/output/files/<category>/<slug>/<attachment>
```

Preprocessing can be run separately for page JSON and attachments.

```powershell
.\.venv\Scripts\python.exe scripts\preprocessing\preprocess_files.py --input-root files\ce\output\json --output-root preprocessed\ce\json --output-json-root files\ce\output\json --layout flat
.\.venv\Scripts\python.exe scripts\preprocessing\preprocess_files.py --input-root files\ce\output\files --output-root preprocessed\ce\files --output-json-root files\ce\output\json
```

Typical preprocessed layout:

```text
preprocessed/ce/json/<category>/<slug>.json
preprocessed/ce/files/<ext>/<category>/<slug>/<attachment>.json
```

Vectorized output:

```text
vectorized/ce/
  index.jsonl
  manifest.json
  ...
```

Supabase target tables:

- `rag_sources`
- `rag_chunks`

## Rule Dataset

Source crawler:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\pknu_rule.py --laws --bylaws
```

Rule attachments are downloaded by default. Use `--no-download-files` for a metadata-only crawl.

Raw output:

```text
files/rule/output/json/pknu_rule_law/*.json
files/rule/output/json/pknu_rule_bylaw/*.json
files/rule/output/html/pknu_rule_law/*.html
files/rule/output/html/pknu_rule_bylaw/*.html
files/rule/output/files/...
```

Rule schema 1.0 JSON uses `content` as the single combined text field and
`attachments[].text` for extracted attachment text. New output no longer emits
the duplicate `html_text`, `page_content`, `attachment_texts`, or
`file_preview_texts` fields. The version-aware reader still consumes them from
legacy files, and preserves a field whenever its text is not represented by
`content` or canonical attachments.

Preprocessed output:

```text
preprocessed/rule/json/
preprocessed/rule/html/
preprocessed/rule/file/
```

Every chunk keeps `source_kind` metadata so JSON, HTML, and attachment-derived records can be distinguished even when text overlaps.

Vectorized output:

```text
vectorized/rule/
  index.jsonl
  manifest.json
  ...
```

Supabase target tables:

- `rule_sources`
- `rule_chunks`

## Main Notice Dataset

Source crawler:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\pknu_notice.py
```

Raw output:

```text
files/pknu_notice/output/json/<category>/<slug>.json
files/pknu_notice/output/html/<category>/<slug>.html
files/pknu_notice/output/files/<category>/<slug>/<attachment>
files/pknu_notice/output/deleted/<category>/<slug>.json
```

Notice JSON uses the shared schema fields documented below. Notice-specific
values such as `no`, `notice_no`, `is_notice`, `categories`, and `pknu_cd` are
retained as source metadata. `published_at` replaces `date`, and
`crawl.crawled_at` replaces the crawler-specific `crawled_at` field.

Attachment metadata:

- `attachments` uses the common attachment schema with `id`, `name`, `url`,
  `final_url`, project-relative `saved_path`, `downloaded`, `content_type`,
  integer `size_bytes`, `sha256`, `text`, and structured `error`.
- Existing downloaded attachments are reused on incremental crawls when their `saved_path` still exists.

## Student Life Dataset

Source crawler:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\pknu_student_life.py --mode all
```

Raw output:

```text
files/pknu_student_life/output/json/<subcategory>/<slug>.json
files/pknu_student_life/output/files/<subcategory>/<slug>/*.pdf
files/pknu_student_life/output/deleted/
```

Student life JSON includes guide/eBook metadata and, when a PDF is downloaded, an attachment entry with local file information such as `saved_path`.

Notes:

- `/main/434` guide PDFs are downloaded into `output/files`.
- Image-only or hard-to-extract PDFs may have short or skipped text content.
- `scripts/crawlers/main/student_life_stats.py` summarizes text extraction status for this dataset.
- This dataset has a dedicated vectorization preset in `scripts/vectorization/vectorization.py`.

## Vectorized Index Records

`scripts/vectorization/vectorization.py` writes `index.jsonl` under each vectorized dataset root.
Each line represents one chunk with content, metadata, embedding, and source identity used by `scripts/vectorization/load_to_supabase.py`.

Supported built-in dataset presets:

- `ce`: `preprocessed/ce` -> `vectorized/ce`
- `rule`: `preprocessed/rule` -> `vectorized/rule`

All registered departments, `rule`, `pknu_notice`, `pknu_student_life`, `pknu_main`, and `pknu_syllabus` have vectorization presets. `--dataset all` discovers existing dataset directories under `preprocessed/`. See [vectorization paths](../../scripts/vectorization/README.md).

## Supabase Repository Files

Tracked Supabase project files live under `supabase/`.

```text
supabase/
  migrations/  # SQL schema migrations
  audit/       # read-only DB audit scripts
```

Migration files:

```text
supabase/migrations/001_create_rag_documents.sql
supabase/migrations/002_split_rag_documents.sql
supabase/migrations/003_chat_history.sql
supabase/migrations/004_create_rule_rag_tables.sql
supabase/migrations/005_add_document_priority_columns.sql
```

Audit scripts:

```text
supabase/audit/audit_db.py
```

## State And Logs

Crawler state files live at the project root and are ignored by Git:

```text
state.json
state_pknu_notice.json
state_pknu_student_life.json
```

Logs are written under `logs/` and are ignored except `logs/.gitkeep`.

Common log files:

```text
logs/crawler.log
logs/rule_crawler.log
logs/rule_preprocessing.log
logs/main_notice_crawler.log
logs/main_student_life_crawler.log
logs/daily_pipeline.log
```
# DB의 다섯 데이터 분류

파일 저장 위치와 독립적으로 DB는 다음 출처·청크 테이블 계열로 나눕니다.

| 분류 | 출처 테이블 | 청크 테이블 |
|---|---|---|
| 학과·단과대 홈페이지 | `rag_sources` | `rag_chunks` |
| 메인 홈페이지 공지 및 첨부파일 | `pknu_notice_sources` | `pknu_notice_chunks` |
| 메인 홈페이지 공지 외 전체·대학생활 | `pknu_main_sources` | `pknu_main_chunks` |
| 규정집 | `rule_sources` | `rule_chunks` |
| 강의계획서 | `pknu_syllabus_sources` | `pknu_syllabus_chunks` |

학과 출처에는 `department_id`, `department_name`, `college_name`, `department_homepage`,
`department_majors` 컬럼이 있고, 기존 `category`, `subcategory`, 상세 metadata도 유지합니다.
학과 정보는 검색 응답 metadata에도 포함됩니다. 수집 단위에는 학과뿐 아니라 단과대 사이트도 포함됩니다.
공지 분배는 원본 `metadata.source_dataset` 기준입니다. 공지 첨부파일도 공지 계열에 속합니다.
이관은 원본 파일, 벡터, 출처 ID, `ingestion_dataset`을 보존합니다.
기존 DB에는 [`018_split_dataset_tables.sql`](../../supabase/migrations/018_split_dataset_tables.sql)을 적용합니다.
