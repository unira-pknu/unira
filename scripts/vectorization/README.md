# 벡터화와 검색 도구

선별 복구·재적재·공용 데이터 반영은 [통합 복구 CLI](../recovery/README.md)로 실행합니다.
`python -m scripts.recovery --list`로 작업을 확인하고 `--task`·`--phase`·`--run`으로 범위를 지정합니다.
기존 복구 명령의 진입점과 작업 기록도 유지합니다.

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
| `resume_excel.py` | 기존 Excel delta와 DB 체크포인트를 대조해 남은 출처만 적재하고 강좌 조회 검증 |
| `verify_recovery_search.py` | 선별 적재한 출처의 실제 검색·본문 해시·인용 연결 확인 |
| `publish_recovery.py` | 검증한 첨부 형식·문자 품질 복구를 공용 파일과 DB에 선별 반영 |

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

전처리 기본 입력은 `preprocessed/<dataset>/`입니다. `all`은 실행 시작 시
존재하는 데이터셋 폴더를 자동 선택합니다. 기본 모델은 백엔드와 같은
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`이며 384차원입니다.
`--dimensions`는 해시 테스트 임베딩에만 적용됩니다. 실제 모델은 보고된 차원을
사용하며, DB 적재·질문 검색은 현재 384차원을 요구합니다.

| 대상 | 기본 벡터 출력 |
|---|---|
| 등록된 학과 | `vectorized/<dataset>/` |
| 공지사항·대학생활 | `vectorized/<dataset>/` |
| 규정 | `vectorized/rule/` |
| 본교 전체·강의계획서·추가 데이터셋 | `vectorized/<dataset>/` |

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

## 중단된 복구 작업의 실행 순서

2026-11-09에 읽은 로컬 기록은 Excel 8,551개 중 7,488개 출처/1,459,018개 청크 적재,
누락 13,323개 출처/47,554개 청크 삽입·DB 대조 후 검색 실패 상태입니다. 현재 DB에 대한
새 조회 결과는 아닙니다. ZIP·규정·CE 작업은 기존 작업 폴더와 검토 결과를 유지합니다.
아래 명령은 사용자가 프로젝트 루트 PowerShell에서 실행하며 단계 실패 시 중단합니다.
이미 같은 적재가 실행 중이면 먼저 그 작업을 마치세요. 전체 데이터셋 재적재는 필요하지 않습니다.

먼저 변경한 코드의 회귀 테스트를 실행합니다. 루트 `pytest.ini`는 `backend` import 경로를
추가하고 pytest 캐시 기록을 끕니다. `.pytest_cache` 접근 거부를 피하기 위해 기존 캐시의
권한을 바꾸거나 삭제할 필요는 없습니다.

```powershell
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe -m pytest scripts/tests backend/tests -q
if ($LASTEXITCODE -ne 0) { throw "테스트 실패: 실행을 중단합니다." }
```

검색 검증은 기본 `D:/unira-backend/backend`의 설치된 코드를 사용합니다. 테스트를 통과한
작업 폴더의 코드가 운영 폴더에 반영되어야 합니다. 커밋되지 않은 변경은 `git pull`만으로
옮겨지지 않습니다. [학교 서버 반영 절차](../../docs/backend/SCHOOL_SERVER.md#작업-폴더의-미커밋-수정-반영)를
따르세요. metadata 필터를 lexical RPC로 넘기는 새 백엔드와
`019_scoped_lexical_search.sql`의 3인자 RPC가 필요합니다. 이 작업의 기존 DDL 적용 기록은
있으므로 무조건 다시 적용하지 마세요. DDL 완료는 RPC 시간 초과 해결을 뜻하지 않습니다.

Excel은 기존 벡터를 재사용합니다. `load`는 인덱스 해시·완료 ID·출처 경로·데이터셋·청크 수를
확인하고 완료 출처의 실제 DB 본문 해시/모델/표 구조/활성 상태부터 대조합니다. 불일치하면
건너뛰거나 전체 재적재하지 않고 중단합니다. 작업별 잠금은 같은 재개 도구의 중복 실행을 막지만,
구버전 적재기나 다른 도구까지 차단하지 않으므로 같은 데이터에 대한 다른 적재를 병행하지 마세요.

```powershell
$excelRun = "var\recovery\excel_rebuild_20261107_032204"
.\.venv\Scripts\python.exe -m scripts.vectorization.resume_excel load --run $excelRun --workers 2 --chunk-batch-size 256
if ($LASTEXITCODE -ne 0) { throw "Excel 재개 실패" }
.\.venv\Scripts\python.exe -m scripts.vectorization.resume_excel finalize --run $excelRun
if ($LASTEXITCODE -ne 0) { throw "Excel 최종 대조/강좌 조회 실패" }
.\.venv\Scripts\python.exe -m scripts.vectorization.resume_excel status --run $excelRun
```

`load`만 끝나면 `database_verified_pending_search`입니다. `finalize`는 전체 선택의 DB 대조를
다시 수행하고, 원본 개설 현황 Excel에서 같은 행의 교수·학년도·학기로 얻은 강좌명 집합과
실제 설치된 백엔드의 교수별 강좌 조회 결과 및 학부 과정·공식 출처 URL을 비교합니다.
기본 질문은 “2026학년도 2학기 학부 유승호 교수님이 진행하는 강의명을 알려줘”입니다.
검증 원본을 자동으로 하나로 고를 수 없으면 `--probe-preprocessed <확인한 JSON 경로>`를 지정합니다.
성공해야 `receipt.json`이 `complete`, `database_verified=true`, `search_verified=true`가 됩니다.
교수 조회는 `pknu_syllabus_sources`를 직접 조회하므로 Excel 임베딩의 의미 검색 검증과는 다릅니다.
Excel 적재 자체는 전체 청크·행 구조·본문 해시·모델 대조로 검증합니다. OpenAI 답변 생성,
인증된 HTTP 요청, 브라우저 렌더링은 이 검증 범위에 포함하지 않습니다.

Excel 적재가 끝나 DB 부하가 줄어든 뒤 누락 적재의 검색 검증을 재시도합니다. 기존
체크포인트가 완전하면 아래 파이프라인은 `verify → search`만 실행하고 13,323건을 재삽입하지 않습니다.

```powershell
.\.venv\Scripts\python.exe var/recovery/missing_ingest_20261107/pipeline.py --plan
if ($LASTEXITCODE -ne 0) { throw "누락 적재 체크포인트 검토 실패" }
.\.venv\Scripts\python.exe var/recovery/missing_ingest_20261107/pipeline.py
if ($LASTEXITCODE -ne 0) { throw "누락 문서 DB/검색 검증 실패" }
```

기존 `verify_search.py`는 유지보수되는 `scripts.vectorization.verify_recovery_search`로 연결합니다.
단독 실행도 가능하지만 먼저 파이프라인의 실제 DB 대조를 통과해야 합니다.
각 테이블 계열에서 최대 6개의 서로 다른 문서를 선정하고, 기대한 출처 ID·데이터셋·원본 경로·
청크 위치/본문 SHA-256·인용 제목/URL을 대조합니다. 최소 한 건의 정상 검색을 계열마다 요구하며
개별 미검색 사례도 결과에 남깁니다. 전체 코퍼스의 검색 정확도를 보증하지 않습니다.
일부 결과가 나와도 RPC 오류/시간 초과가 있으면 실패입니다. 기존 실패 결과는 백업합니다.
계속 SQLSTATE 57014가 발생하면 완료 상태를 수정하지 말고 RPC 실행계획·후보량·인덱스를 추가 확인하세요.

누락·Excel 두 완료 기록이 모두 통과한 뒤 다음 순서로 진행합니다. 기존 격리 산출물과
CE 원본 검토 결과를 재사용하므로 `prepare`, `probe`, `stage`, `vectorize`를 일괄 재실행하지 않습니다.

```powershell
$gate = "var\recovery\missing_ingest_20261107\receipt.json"
$zipRun = "var\recovery\zip_collision_repair_20261108"
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_archive_collisions resume --run $zipRun --after-receipt $gate
if ($LASTEXITCODE -ne 0) { throw "ZIP 복구 실패" }
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_archive_collisions status --run $zipRun

$ruleRun = "var\recovery\rule_sources_20261108"
foreach ($phase in @("apply", "publish", "verify", "status")) {
    .\.venv\Scripts\python.exe -m scripts.preprocessing.repair_rule_sources $phase --run $ruleRun --after-receipt $gate
    if ($LASTEXITCODE -ne 0) { throw "규정 $phase 실패" }
}

$ceRun = "var\recovery\ce_reconcile_20261109"
foreach ($phase in @("apply", "publish", "verify", "status")) {
    .\.venv\Scripts\python.exe -m scripts.vectorization.reconcile_ce $phase --run $ceRun --after-receipt $gate
    if ($LASTEXITCODE -ne 0) { throw "CE $phase 실패" }
}
```

ZIP `resume`는 선행 작업이 미완료면 종료 코드 0이어도 `waiting_for_ingestion`을 표시할 수 있습니다.
이 상태에서 다음 반영 작업으로 진행하지 마세요. ZIP 완료는 DB 적재 대조까지이며, 규정·CE의
최종 `verify`는 실제 검색도 확인합니다. CE 비교 이후 DB/원본이 바뀌었다면 기존 계획을 강제로
수정하지 말고 새 작업 폴더에서 다시 대조해야 합니다.
첨부 실제 형식 복구는 공용 DB를 변경하지 않는 별도 작업으로
[실패 항목 재시도 명령](../preprocessing/README.md#기존-첨부-복구의-중단-후-재시도)을 사용합니다.

## 격리 복구 결과의 공용 반영

Excel 종료 검사에서는 실제 검색 행과 제목 행만 있는 빈 서식을 구분한다.
`resume_excel`은 벡터화와 같은 `extract_chunk_records` 기준을 사용하되,
실제 데이터 행이 생겼거나 출처 식별 정보가 바뀐 제외 문서는 중단한다.
제목 행만 있는 서식과 메타데이터 전용 문서는 기존 본문을 삭제하지 않고
검색 대상에서 제외한다. 모든 적재 체크포인트가 커밋된 뒤 이 검사에서 실패한 경우에도
`load`를 재실행하면 완료 문서를 다시 쓰지 않고 대조·종료 검사를 이어간다.
`finalize`의 개설교과목 자동 선택이 여러 버전과 일치하면 임의로 최신 파일을 고르지 않는다.
이때 `--probe-preprocessed "<대조할 개설교과목 전처리 JSON 경로>"`로 원본을 지정한다.
해당 경로는 검증된 Excel inventory에 정확히 한 건이어야 하며, 실제 조회의 교수·학기·과정과
원본 행의 강좌 집합을 대조한다.

Windows에서 원래 폴더 구조를 백업 아래에 한 번 더 붙이면 경로 길이 제한에 걸릴 수 있다.
공용 반영 도구는 긴 백업 경로만 `backup/_long_paths/` 아래의 경로 해시 이름으로 저장하고,
`backup_locations.json`에 원래 경로와 백업 위치를 남긴다. 공용 문서의 내부 경로·확장자는
유지하며, 교체용 임시 파일은 같은 폴더의 짧은 이름을 사용한다.

`publish_recovery`는 형식 복구의 성공 항목 또는 원본 검토 후 staged된 문자 품질 결과를
선별합니다. `prepare`는 격리 파일과 읽기 전용 DB 스냅샷을 준비하므로 기존 적재 중에도
실행할 수 있습니다. DB 변경·공용 게시에는 누락·Excel 적재의 완료 기록을 요구합니다. 추출 실패,
출처 충돌, OCR 시각 검토 필요, 원본 해시 변경, 로컬과 다른 DB 버전은 보류 목록에 남깁니다.
DB가 다른 내용을 가지고 있으면 처리 시각만으로 덮지 않습니다. 기존 비활성 출처도
재활성화하지 않습니다. 원본 경로를 사용하는 다른 출처 ID가 있으면 먼저 식별자를 검토합니다.

다음은 사용자 실행 예시입니다. 같은 작업이 이미 실행 중이면 중복 실행하지 않습니다.

```powershell
$publicRun = "var\recovery\format_publication_20261109"
$gate = "var\recovery\missing_ingest_20261107\receipt.json"
.\.venv\Scripts\python.exe -m scripts.vectorization.publish_recovery prepare --run $publicRun --source-run var/recovery/attachment_formats_20261108 --kind formats --after-receipt $gate
if ($LASTEXITCODE -ne 0) { throw "복구 반영 후보 선정 실패" }
foreach ($phase in @("vectorize", "apply", "publish", "verify", "status")) {
    .\.venv\Scripts\python.exe -m scripts.vectorization.publish_recovery $phase --run $publicRun --after-receipt $gate
    if ($LASTEXITCODE -ne 0) { throw "복구 $phase 실패" }
}
```

문자 품질 복구에는 별도 `--run`을 지정하고 `--kind quality --source-run <검토 작업 폴더>`를
사용합니다. 먼저 `review_text_quality stage`가 성공해야 합니다. `retain`은 기존 자료를
보존하는 결정이며 수정·복원 성공으로 집계하지 않습니다. 모든 후보가 보류되면 벡터화·적재를
진행하지 않습니다. 작업 폴더의 `plan.json.deferred`에 보류 이유를 보존합니다.

선정 시 DB 출처·청크·벡터를 작업 폴더에 백업하고 본문/벡터가 현재 로컬 기준과 같은지
대조합니다. `apply`는 이 스냅샷과 현재 DB가 같은 경우에만 해당 출처 청크를 교체합니다.
이미 같은 선택이 커밋됐다면 본문 해시·모델·벡터를 대조해 재사용합니다. 모든 청크를
검토 제외한 출처는 기존 DB 청크를 남기고 출처를 비활성화합니다.

`publish`는 기존 JSON·벡터·전체 인덱스를 백업하고 해당 출처만 병합합니다.
`verify`는 실제 DB의 본문 SHA-256·청크 위치·float32 벡터와 공용 JSON을 대조하고,
운영 검색 RPC에 데이터셋·원래 slug 조건을 보내 해당 출처의 검색 및 인용을 검증합니다.
이 검사는 식별자 조건을 준 검색이며 일반 자연어 검색 정확도나 생성 답변을 보증하지 않습니다.
선정하지 않은 원본·검토 보류 자료는 변경하지 않으며, `complete`, `search_verified=true`는
선정된 문서에 대한 완료입니다. 보류 개수와 이유를 함께 확인해야 합니다.

## 로컬 인덱스 밖의 활성 출처와 동일 경로 중복

`reconcile_active_sources`는 5개 테이블 계열의 활성 출처를 로컬 전체 인덱스와 대조합니다.
이전 관측값 34건·16그룹은 실행 시 다시 확인하며 값이 다르면 계획을 보존하고 중단합니다.
현재 요청에서는 코드만 준비했으며 DB 조회·변경·검색·테스트는 실행하지 않았습니다.

로컬 인덱스에 없다는 이유로 출처를 비활성화하지 않습니다. 동일 원본 경로도 데이터셋,
문서/첨부 식별자, 제목·출처 URL, 학기·분반·개정/시행 정보가 다르면 별도 자료로 보존합니다.
원본 파일이 있고 기록된 첨부 SHA-256과 일치하며, 문서 식별 정보와 순서별 청크 본문
해시가 모두 같고 임베딩이 유효한 그룹만 `exact_duplicate`로 분류합니다.
청크 구성이 다르거나 ZIP 멤버·원본 미확인·식별자 누락이면 자동 비활성화하지 않습니다.
대표 출처는 이 동등성 검사를 통과한 그룹에서 로컬 인덱스에 있는 ID를 우선하고,
나머지는 ID 순서로 결정합니다. 적재 시각만으로 다른 개정본을 중복 처리하지 않습니다.

사용자 실행 순서입니다. 매 단계가 성공하면 다음 단계로 진행합니다.

```powershell
$env:PYTHONIOENCODING = "utf-8"
$sourceRun = "var\recovery\active_sources_20261108"
.\.venv\Scripts\python.exe -m pytest scripts\tests\test_recovery_control.py -q
.\.venv\Scripts\python.exe -m scripts.vectorization.reconcile_active_sources scan --run $sourceRun
```

`plan.json`은 DB에만 있는 출처의 원본 정보·실제 본문 해시와 각 중복 그룹을 기록합니다.
`snapshots/`는 해당 출처·청크·벡터의 변경 전 사본입니다. `decisions.json`에서
대표 ID 또는 `preserve`를 검토합니다. `preserve_pending_original_review` 자료는
이 작업으로 비활성화할 수 없습니다. 다른 본문/개정본의 폐기는 별도 판단이 필요합니다.
예상 건수가 다르면 새 작업 폴더에서 검토한 `--expected-db-only`, `--expected-groups`를
명시해 다시 검사합니다. 기존 계획이나 스냅샷을 수정하여 검사를 우회하지 않습니다.

기존 적재가 모두 끝나고 선택·원본이 안정된 뒤 실행합니다.

```powershell
$gate = "var\recovery\missing_ingest_20261107\receipt.json"
foreach ($phase in @("baseline", "apply", "verify", "status")) {
    .\.venv\Scripts\python.exe -m scripts.vectorization.reconcile_active_sources $phase --run $sourceRun --after-receipt $gate
    if ($LASTEXITCODE -ne 0) { throw "$phase 실패: 바로 위 오류를 확인하세요." }
}
```

`baseline`은 보존 자료와 중복 그룹이 실제 검색되는지 확인합니다. 검색이 이미 누락됐거나
RPC가 실패하면 비활성화 전 중단하여 기존 검색 공백을 숨기지 않습니다. `apply`는 선택한
모든 스냅샷이 현재 DB와 같은지 잠근 뒤 하나의 트랜잭션에서 중복 출처의 `status`만
`inactive`로 바꿉니다. DB 트리거의 `updated_at` 갱신은 별도로 기록합니다.
청크·벡터는 삭제하지 않고 대표·고유 출처의 본문·상태·메타데이터는 변경하지 않습니다.

`verify`는 상태 변경 외의 출처/청크/벡터 변경이 없는지 대조하고, 실제 운영 검색 코드와
Supabase RPC로 대표 출처·보존 자료가 검색되고 비활성 출처가 반환되지 않는지 확인합니다.
본문과 URL도 스냅샷과 대조합니다. 중간 실패 후 이미 적용된 상태라면 `verify`부터
재시도합니다. 비교 이후 외부 변경이 있으면 새로 대조해야 합니다.
`complete`, `search_verified=true`가 최종 기준이며 답변 생성·브라우저 검증은 포함하지 않습니다.
