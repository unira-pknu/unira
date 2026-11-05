# 벡터화와 검색 도구

전처리 결과의 임베딩 생성, 저장·적재, 검색 및 연결 파이프라인을 모아 둔 패키지입니다.
이전 경로의 호환 파일은 제공하지 않습니다.

| 파일 | 역할 |
|---|---|
| `vectorization.py` | 전처리 청크를 임베딩하고 벡터 JSON·JSONL 인덱스 생성 |
| `common.py` | 공통 모델 이름·차원, 모델 로딩·정규화 임베딩·벡터 검증 |
| `datasets.py` | 데이터셋별 전처리 입력·벡터 출력·DB 인덱스 경로와 테이블 정의 |
| `load_to_supabase.py` | 벡터·출처 메타데이터를 DB에 적재 |
| `query_supabase.py` | 질문 임베딩과 DB 유사도 검색 |
| `check_hnsw_indexes.py` | DB 벡터 검색 인덱스 존재 여부 확인 |
| `layout_rag.py` | 검토한 이미지 레이아웃의 실험용 청크·임베딩·로컬 저장소 |
| `ingest_layout_data.py` | 이미지 검토 결과를 로컬 실험용 저장소에 적재 |
| `split_oversized_chunks.py` | DB의 긴 청크를 분할하고 다시 임베딩·교체 |
| `pipelining.py` | 크롤링·전처리·벡터화·적재를 연결하는 일일 파이프라인 |
| `preprocessing_pipeline_recent.py` | 최근 크롤링 자료를 전처리·벡터화·적재 |

프로젝트 루트의 Python 3.12 가상환경에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.vectorization.vectorization --help
.\.venv\Scripts\python.exe -m scripts.vectorization.vectorization --dataset ce --dry-run
# 전처리 폴더 전체를 확인하고 벡터화
.\.venv\Scripts\python.exe -m scripts.vectorization.vectorization --dataset all --dry-run
.\.venv\Scripts\python.exe -m scripts.vectorization.vectorization --dataset all
# 같은 모델·차원·입력으로 만들어진 유효한 결과는 재사용
.\.venv\Scripts\python.exe -m scripts.vectorization.vectorization --dataset all --changed-only
# 병렬 터미널 예시: 각 터미널의 worker-index를 0, 1, 2, 3으로 다르게 지정
.\.venv\Scripts\python.exe -m scripts.vectorization.vectorization --dataset all --workers 4 --worker-index 0 --log-file logs/vector_worker_0.log
```

`--dry-run`은 대상 확인용입니다. 실제 벡터화는 모델을 로드할 수 있으며,
DB 적재·검색 도구는 DB 연결이 필요합니다. 파이프라인 도구는 크롤링과 DB 쓰기도
수행하므로 단순 코드 점검 시 실행하지 마세요.
병렬 실행은 데이터셋 단위로 분할하며 모든 터미널에 같은 `--workers` 값과
서로 다른 `--worker-index`를 사용합니다. 실행 중 데이터셋 폴더를 추가하거나
같은 인덱스의 작업을 중복 실행하지 마세요. 터미널과 지정한 로그 파일에
작업별 데이터셋 순서 및 파일별 처리 건수를 표시합니다.
해시 임베딩은 로컬 검증 전용이며 DB 적재기는 거부합니다. 인덱스의 모델 정보와
개별 레코드의 모델 정보가 다른 경우에도 적재를 중단합니다.

전처리 기본 입력은 `files/preprocessed/<dataset>/`입니다. `all`은 실행 시작 시
존재하는 데이터셋 폴더를 자동 선택합니다. 기본 모델은 백엔드와 같은
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`이며 384차원입니다.
`--dimensions`는 해시 테스트 임베딩에만 적용됩니다. 실제 모델은 보고된 차원을
사용하며, DB 적재·질문 검색은 현재 384차원을 요구합니다.

| 대상 | 기본 벡터 출력 |
|---|---|
| 등록된 학과 | `files/vectorized/<dataset>/` |
| 공지사항·대학생활 | `files/vectorized/<dataset>/` |
| 규정 | `files/vectorized/rule/` |
| 본교 전체·강의계획서·추가 데이터셋 | `files/vectorized/<dataset>/` |

`--dataset all --input-root <경로>`는 해당 루트 아래의 데이터셋 폴더를 사용합니다.
`--output-root <경로>`를 함께 주면 결과를 `<경로>/<dataset>/`으로 나눕니다.
출력은 입력 폴더 밖에 있어야 합니다. 알 수 없는 데이터셋도 파일 벡터화는 가능하지만
DB 테이블 연결은 `datasets.py`에 등록된 데이터셋만 지원합니다.
DB는 다음 다섯 전용 출처·청크 테이블 계열을 사용합니다. 기존 DB에는 먼저
[`018_split_dataset_tables.sql`](../../supabase/migrations/018_split_dataset_tables.sql)을 적용합니다.

| 분류 | 출처 / 청크 | 검색 RPC |
|---|---|---|
| 학과·단과대 홈페이지 | `rag_sources` / `rag_chunks` | `match_rag_documents` |
| 메인 홈페이지 공지·공지 첨부 | `pknu_notice_sources` / `pknu_notice_chunks` | `match_pknu_notice_documents` |
| 메인 홈페이지 공지 외 전체 | `pknu_main_sources` / `pknu_main_chunks` | `match_pknu_main_documents` |
| 규정집 | `rule_sources` / `rule_chunks` | `match_rule_documents` |
| 강의계획서 | `pknu_syllabus_sources` / `pknu_syllabus_chunks` | `match_pknu_syllabus_documents` |

학과 출처에는 registry 기준 `department_id`, `department_name`, `college_name`,
`department_homepage`, `department_majors` 컬럼을 저장합니다. 같은 정보는 검색 응답의
metadata에도 제공됩니다. `metadata_filter={"department_id":"ce"}`로 학과를 제한할 수 있습니다.
`category`, `subcategory`와 원본 metadata의 상세 분류는 유지합니다.

`pknu_main` 벡터 파일에는 공지·대학생활·기타 본교 자료가 함께 있으므로 적재기가
`metadata.source_dataset`으로 행별 분배합니다. 원본이 `pknu_notice`이면 공지 전용,
나머지는 공지 외 본교 테이블입니다. 첨부파일의 `is_notice=false`로 분류하지 않습니다.
이관 전후 `ingestion_dataset` 및 안정적인 출처·청크 ID는 유지하므로 기존 벡터를
다시 생성할 필요가 없습니다. 기존 `pknu_student_life` 입력은 본교 테이블로 적재합니다.
혼합 본교 입력에는 `--sources-table`/`--chunks-table` 재지정을 허용하지 않습니다.

증분 실행이나 일부 파일 처리도 현재 유효한 전체 결과로 `index.jsonl`을 재구성합니다.
삭제된 입력, 실패·품질 검사에서 제외된 입력, 다른 모델·차원의 결과는 인덱스에서
제외합니다. 인덱스와 파일별 결과는 임시 파일을 완성한 후 교체합니다. 파일별 실패는
manifest에 기록하고 CLI는 실패가 있으면 종료 코드 1을 반환합니다.
기존 결과 중 입력 정보와 정책 버전이 없는 결과는 한 번 다시 생성합니다.

전처리 작업이 완료된 뒤 실제 벡터화를 실행하세요. 실행 중 입력이 바뀌면 해당 파일을
실패로 기록하며, 시작 이후 추가된 데이터셋·파일은 다음 실행에서 반영됩니다.
현재 진행 중인 전체 전처리를 자동 중지하거나 벡터화를 자동 시작하지 않습니다.

백엔드 질문 임베딩은 [`backend/app/embeddings.py`](../../backend/app/embeddings.py)에서
별도로 관리합니다. 이 패키지는 백엔드 런타임의 필수 의존성이 아닙니다.
# DB 적재 전 검사와 출처 키

전체 적재: `.\.venv\Scripts\python.exe -m scripts.vectorization.load_to_supabase --dataset all --batch-size 200`
모든 수집 데이터셋을 사전 검사한 다음 순차 적재하며, 우선순위는 테이블 계열마다 마지막에 한 번 갱신합니다.
중간 실패 시 이후 적재는 중단되고 이미 커밋한 배치는 유지됩니다. 실행 로그는 `logs/load_to_supabase.log`에 남습니다.

적재는 `COPY → 연결별 임시 테이블 → 일괄 upsert`로 수행합니다. 내용이 같은 행은 갱신하지 않습니다.
대량 적재 전 `--dataset all --prepare-bulk`로 검색 인덱스를 명시적으로 제거하고,
모든 적재가 끝난 뒤 `--dataset all --finalize-indexes`로 검색 인덱스와 통계를 준비합니다.
PK·UNIQUE·외래키와 원본 데이터는 유지합니다. 인덱스 작업과 적재 터미널을 동시에 실행하지 마세요.

검색 인덱스는 원본 `vector(384)` 컬럼을 `halfvec(384)`로 변환한 HNSW 표현식 인덱스입니다.
구축 파라미터는 기존 기본값인 `m=16`, `ef_construction=64`를 유지하며, 검색 파라미터와 메모리 설정은
변경하지 않습니다. COPY 적재와 원본 벡터는 그대로 사용하므로 재임베딩은 필요하지 않습니다.
검색 RPC도 같은 `halfvec` 표현식으로 정렬하도록 `017_update_search_functions.sql`을 함께 적용해야 합니다.
기존 `vector` 인덱스를 같은 이름으로 재사용하지 않도록 적재기가 형식과 파라미터를 검사합니다.
데이터 적재가 완료된 DB의 전환 순서와 인덱스 재생성 명령은
[`supabase/migrations/README.md`](../../supabase/migrations/README.md#halfvec-hnsw-인덱스)에 정리했습니다.
실행 중인 기존 인덱스 작업에는 코드 변경이 소급 적용되지 않습니다.

빈 선택 메타데이터는 전송 전에 제거하며 `False`·`0`·필수 날짜/수집 필드는 유지합니다.
본문이 없거나 `used_metadata_fallback`으로 표시된 문서는 검색 적재에서 제외합니다.
기존 벡터도 전처리 파일의 상태를 확인하므로 재임베딩 없이 제외할 수 있습니다.
재적재 시 같은 안정 키로 이미 저장된 제외 대상 청크만 삭제하고 출처 행·원본은 유지합니다.
본문이 남아 있는 부분 OCR 실패 문서는 보존합니다. 기존 벡터 파일은 이력으로 유지합니다.

`load_to_supabase.py`는 DB 연결 전에 완료된 `manifest.json`을 요구합니다.
선택한 데이터셋·인덱스 경로·검색 모델·384차원·실패 0건·레코드 수를 검사하고,
전체 인덱스의 레코드와 중복 청크 위치를 검증합니다. 실패가 남은 manifest는 적재하지 않습니다.
검사 중에는 벡터화 작업을 동시에 실행하지 마세요.

DB 출처 키와 청크 키는 적재 데이터셋을 포함한 해시로 생성합니다.
원래 slug는 `metadata.original_source_slug`, 적재 단위는 `metadata.ingestion_dataset`에 보존하며,
기존 `metadata.source_dataset`은 유지합니다. 기존 방식으로 적재된 행은 자동 변환하거나 삭제하지 않습니다.
기존 DB에 새 키 방식으로 재적재하면 이전 행이 남을 수 있으므로 재구축 또는 별도 이관이 필요합니다.

중복 삭제는 해당 `ingestion_dataset`에만 적용하고 URL·제목·첨부명·원본 경로·ZIP 멤버 경로를 구분합니다.
원본 경로가 없는 행과 이전 방식의 행은 중복 삭제에서 제외합니다.
일반 RAG 테이블을 사용하는 학과·본교·강의계획서는 공통 우선순위 계산기를 사용합니다.
