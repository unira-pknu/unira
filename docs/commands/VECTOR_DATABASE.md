# Vector Database Commands

전체 데이터셋의 스키마 준비·검색 함수 SQL과 Python 적재·audit 실행 안내는
[`supabase/migrations/`](../../supabase/migrations/README.md)에 있습니다.
적용 순서와 실제 DB 확인 범위, 벡터화 완료 확인 절차를 먼저 참고하세요.

## Priority Updates

### `scripts/ce/update_priorities.py`

Supabase의 CE source priority score를 갱신합니다.

```powershell
.\.venv\Scripts\python.exe scripts\ce\update_priorities.py
.\.venv\Scripts\python.exe scripts\ce\update_priorities.py --dry-run
```

### `scripts/rule/update_priorities.py`

Supabase의 rule source priority score를 갱신합니다.

```powershell
.\.venv\Scripts\python.exe scripts\rule\update_priorities.py
.\.venv\Scripts\python.exe scripts\rule\update_priorities.py --dry-run
```

옵션:

- `--dry-run`: DB 업데이트 없이 계산 결과만 확인합니다.
- `--from-index`: 로컬 `files/vectorized/rule/index.jsonl`에서 preview합니다. `--dry-run`과 함께 사용해야 합니다.
- `--index-path PATH`: preview에 사용할 index 경로입니다.
- `--preview-limit N`: preview 출력 개수입니다.

## RAG

### `scripts/vectorization/vectorization.py`

전처리된 chunk JSON을 embedding 벡터로 변환하고 `index.jsonl`, `manifest.json`, 개별 vector 파일을 생성합니다.

```powershell
.\.venv\Scripts\python.exe scripts\vectorization\vectorization.py --dataset ce --backend sentence-transformers
.\.venv\Scripts\python.exe scripts\vectorization\vectorization.py --dataset rule --backend sentence-transformers
```

기본값:

- `--dataset`: `ce`
- `--backend`: `sentence-transformers`
- `--model-name`: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- `--dimensions`: `384` (해시 임베딩 전용)
- `--batch-size`: `32`

주요 옵션:

- `--dataset ce|rule`: 기본 입출력 경로 preset입니다.
- `--input-root PATH`: 전처리 JSON 루트입니다. 지정하면 `--dataset` 입력 경로보다 우선합니다.
- `--output-root PATH`: 벡터화 출력 루트입니다. 지정하면 `--dataset` 출력 경로보다 우선합니다.
- `--backend hash|sentence-transformers`: embedding backend입니다.
- `--model-name NAME`: sentence-transformers 모델명입니다.
- `--dimensions N`: hash backend 차원 수입니다.
- `--batch-size N`: embedding batch size입니다.
- `--dry-run`: 저장 없이 대상만 확인합니다.

Supabase 적재 전에는 DB schema와 맞는 384차원 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 사용을 권장합니다.

### `scripts/vectorization/load_to_supabase.py`

벡터화된 `index.jsonl`을 Supabase PostgreSQL에 적재합니다.

```powershell
.\.venv\Scripts\python.exe scripts\vectorization\load_to_supabase.py --dataset ce
.\.venv\Scripts\python.exe scripts\vectorization\load_to_supabase.py --dataset rule
```

기본 매핑:

- `--dataset ce`: `files/vectorized/ce/index.jsonl` -> `rag_sources`, `rag_chunks`
- 다른 등록 학과도 `rag_*`를 사용하며 `department_id`로 구분합니다.
- `--dataset pknu_main`: 원본 `metadata.source_dataset=pknu_notice` 행은 `pknu_notice_*`, 나머지는 `pknu_main_*`에 분배합니다.
- `--dataset pknu_notice`: `pknu_notice_sources`, `pknu_notice_chunks`
- `--dataset pknu_student_life`: `pknu_main_sources`, `pknu_main_chunks`
- `--dataset pknu_syllabus`: `pknu_syllabus_sources`, `pknu_syllabus_chunks`
- `--dataset rule`: `files/vectorized/rule/index.jsonl` -> `rule_sources`, `rule_chunks`
- `--batch-size`: `200`

옵션:

- `--index PATH`: 직접 지정할 vectorized JSONL 경로입니다.
- `--dataset NAME`: 등록 학과 또는 본교·공지·규정·강의계획서 프리셋입니다. `all`은 수집된 전체 데이터셋입니다.
- `--sources-table NAME`: source table명을 직접 지정합니다.
- `--chunks-table NAME`: chunk table명을 직접 지정합니다.
- 혼합 `pknu_main` 입력에서는 테이블 재지정을 사용할 수 없습니다.
- `--batch-size N`: COPY 후 일괄 upsert하는 배치 크기입니다. 기본값은 200입니다.
- `--prepare-bulk`: 적재 전에 검색 인덱스를 명시적으로 제거합니다. 데이터·PK·UNIQUE·외래키는 유지합니다.
- `--finalize-indexes`: 모든 적재가 끝난 뒤 검색 인덱스를 생성하고 ANALYZE를 수행합니다. HNSW는 `halfvec(384)` 표현식과 기존 파라미터 `m=16`, `ef_construction=64`를 사용합니다.
- `--index-timeout-seconds N`: 인덱스 작업에만 적용하는 시간 제한입니다. 기본값은 1800초이며, `0`은 해당 작업의 시간 제한을 해제합니다. 전역 설정은 변경하지 않습니다.

원본 벡터 컬럼과 COPY 적재 형식은 `vector(384)`로 유지합니다. 기존 데이터는 재임베딩하거나
재적재할 필요 없이 인덱스를 재생성하고 수정된 `017_update_search_functions.sql`을 다시 적용합니다.
기존 인덱스 작업을 종료한 뒤 수행하는 전환 절차는
[`DB 적재 README`](../../supabase/migrations/README.md#이미-적재한-db의-전환)를 참고하세요.

필요 환경 변수는 `backend/.env`에서 로드합니다.

- `DATABASE_URL` 또는 `SUPABASE_DB_URL`
- 또는 `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`
- 선택: `PGPORT`, `PGSSLMODE`

### `scripts/vectorization/query_supabase.py`

Supabase에 적재된 RAG chunk를 대상으로 semantic search를 실행합니다.

```powershell
.\.venv\Scripts\python.exe scripts\vectorization\query_supabase.py "졸업 요건 알려줘" --dataset ce
.\.venv\Scripts\python.exe scripts\vectorization\query_supabase.py "학칙의 휴학 규정 알려줘" --dataset rule
```

기본값:

- `--dataset`: `ce`
- `--model-name`: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- `--top-k`: `5`
- `--min-similarity`: `0.0`
- `--rank-by`: `similarity`
- `--priority-weight`: `0.15`

옵션:

- positional `question`: 검색 질문입니다. `--rank-by priority`에서는 생략할 수 있습니다.
- `--dataset ce|rule`: 검색할 dataset입니다.
- `--match-function NAME`: 기본 RPC 함수명을 override합니다. 기본 similarity 검색에서만 사용합니다.
- `--top-k N`: 반환 결과 수입니다.
- `--min-similarity FLOAT`: 최소 similarity입니다.
- `--rank-by similarity|priority|hybrid`: 정렬 기준입니다.
- `--include-priority`: similarity 검색에서도 priority 정보를 함께 출력합니다.
- `--priority-weight FLOAT`: hybrid 정렬에서 priority 반영 비율입니다.

### `eval/tools/search_smoke.py`

`query_supabase.py`를 여러 고정 질문으로 호출하는 smoke test wrapper입니다.
검색 로직은 없고, 검색 파이프라인이 대략 정상 동작하는지 빠르게 확인하는 용도입니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.search_smoke
```

### `scripts/vectorization/pipelining.py`

CE 파이프라인을 자동으로 실행합니다.

실행 순서:

1. `scripts/crawlers/departments/engine.py --dataset ce --once`
2. `scripts/preprocessing/preprocess_files.py`로 JSON 전처리
3. `scripts/preprocessing/preprocess_files.py`로 첨부파일 전처리
4. `scripts/vectorization/vectorization.py`
5. `scripts/vectorization/load_to_supabase.py`
6. `scripts/ce/update_priorities.py`

1회 실행:

```powershell
.\.venv\Scripts\python.exe scripts\vectorization\pipelining.py --once
```

스케줄러 실행:

```powershell
.\.venv\Scripts\python.exe scripts\vectorization\pipelining.py --run-at 09:00
```

옵션:

- `--once`: 스케줄러 없이 전체 CE 파이프라인을 1회 실행합니다.
- `--run-on-start`: 시작 즉시 1회 실행한 뒤 스케줄러를 유지합니다. 기본값입니다.
- `--no-run-on-start`: 즉시 실행 없이 스케줄러만 시작합니다.
- `--run-at HH:MM`: 매일 실행 시각입니다. 기본값은 `09:00`입니다.
- `--poll-seconds N`: 스케줄러 polling 간격입니다.
- `--vector-backend sentence-transformers|hash`: vectorization backend입니다.
- `--vector-batch-size N`: vectorization batch size입니다.
- `--load-batch-size N`: Supabase 적재 batch size입니다.

## Supabase

### Migrations

마이그레이션 SQL은 `supabase/migrations/`에 있습니다.

```text
supabase/migrations/
  001_create_rag_documents.sql
  002_split_rag_documents.sql
  003_chat_history.sql
  004_create_rule_rag_tables.sql
  005_add_document_priority_columns.sql
```

### Audit

DB 상태 점검용 읽기 전용 스크립트는 `supabase/audit/`에 있습니다.

```powershell
.\.venv\Scripts\python.exe supabase\audit\audit_db.py
```

## Common End-To-End Examples

### CE

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\engine.py --dataset ce --once
.\.venv\Scripts\python.exe scripts\preprocessing\preprocess_files.py --input-root files\ce\output\json --output-root files\preprocessed\ce\json --output-json-root files\ce\output\json --layout flat
.\.venv\Scripts\python.exe scripts\preprocessing\preprocess_files.py --input-root files\ce\output\files --output-root files\preprocessed\ce\files --output-json-root files\ce\output\json
.\.venv\Scripts\python.exe scripts\vectorization\vectorization.py --dataset ce --backend sentence-transformers
.\.venv\Scripts\python.exe scripts\vectorization\load_to_supabase.py --dataset ce
.\.venv\Scripts\python.exe scripts\ce\update_priorities.py
```

### Rule

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\pknu_rule.py --laws --bylaws
.\.venv\Scripts\python.exe scripts\preprocessing\rule.py
.\.venv\Scripts\python.exe scripts\vectorization\vectorization.py --dataset rule --backend sentence-transformers
.\.venv\Scripts\python.exe scripts\vectorization\load_to_supabase.py --dataset rule
.\.venv\Scripts\python.exe scripts\rule\update_priorities.py
```

### Main Website

공지사항·대학생활·본교 전체·강의계획서도 벡터화 기본 경로를 지원합니다.
전체 전처리 자료는 `--dataset all`로 선택합니다. 상세 경로와 증분 실행 동작은
[벡터화 패키지 안내](../../scripts/vectorization/README.md)를 참고하세요.

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\pknu_notice.py --recent-only 5
.\.venv\Scripts\python.exe scripts\crawlers\pknu_student_life.py --mode all
```
