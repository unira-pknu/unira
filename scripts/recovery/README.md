# 복구·보수 통합 CLI

복구 자료의 실제 저장 위치는 `var/recovery/`입니다. 전처리는 `preprocessed/`, 벡터는 `vectorized/`를 사용하며
`files/`에는 수집 원본만 둡니다. 이동한 기존 계획·벡터·영수증의 내용과 해시는 다시 쓰지 않습니다.
`scripts/artifact_paths.py`와 `var/storage_paths.json`이 이전 경로를 새 실제 위치에 연결하고, 기존 DB 작업 소유권 ID와
긴 파일명 백업 연결도 보존합니다. 이전 경로와 새 경로 양쪽에 같은 자료가 있으면 임의로 선택하지 않고 중단합니다.
복구 CLI의 `--run`에는 새 `var/recovery/<작업명>` 경로를 지정합니다.

프로젝트 루트에서 `python -m scripts.recovery`를 사용합니다. 작업 선택·옵션 검사·단계 실행·잠금·실패 기록은
`cli.py`, JSON 저장·해시·경로 검사·영수증 갱신은 `common.py`가 담당합니다. 형식별 추출 및 DB 대조 알고리즘은
기존 모듈의 함수를 재사용합니다. 별도 프로세스나 복사한 CLI 로직으로 실행하지 않습니다.

```powershell
# 가능한 작업과 단계 목록. 작업을 실행하지 않음
.\.venv\Scripts\python.exe -m scripts.recovery --list

# --phase를 생략하면 로컬 상태만 조회. 모델/DB 접근 및 파일 변경 없음
.\.venv\Scripts\python.exe -m scripts.recovery --task zip --run var\recovery\zip_collision_repair_20261108

# 지정한 단계와 경로 검토. 실제 추출·적재·검색은 수행하지 않음
.\.venv\Scripts\python.exe -m scripts.recovery --task formats --phase process --run var\recovery\attachment_formats_20261108 --retry-failed --plan
```

| `--task` | 용도 | 선택 가능한 단계 (`--phase`) | 작업 전용 옵션 |
|---|---|---|---|
| `zip` | ZIP 내부 경로·확장자 충돌 복구 | `prepare stage vectorize preflight apply verify publish`, 또는 `resume` | `--after-receipt` |
| `formats` | 파일 서명·실제 형식 대조와 추출 | `scan process` | `--file-ext`, `--recover`, `--retry-failed` |
| `rules` | 규정 주소·제목·버전 복원 | `prepare vectorize apply publish verify` | `--after-receipt`, `--backend-root` |
| `ce` | 로컬/DB 충돌을 원본과 비교하고 선별 동기화 | `compare probe stage apply publish verify` | `--expected-count`, `--after-receipt`, `--backend-root` |
| `excel` | 기존 Excel 재처리 결과의 적재 재개 | `inventory load verify finalize` | `--workers`, `--chunk-batch-size`, `--backend-root`, `--probe-question`, `--probe-preprocessed` |
| `publish` | 검토된 형식·본문 복구 결과만 공용 파일/DB에 반영 | `prepare vectorize apply publish verify` | `--source-run`, `--kind formats\|quality`, `--after-receipt`, `--backend-root` |
| `quality` | 깨진 문자/PDF 추출 품질을 원본과 비교 | `scan probe stage` | `--expected-chunks`, `--expected-pdfs`, `--pdf-source-id`, `--ocr`, `--retry-errors`, `--max-ocr-pages` |
| `sources` | 동일 원본·본문이 확인된 중복 출처 검토 | `scan baseline apply verify` | `--expected-db-only`, `--expected-groups`, `--after-receipt`, `--backend-root` |
| `missing` | 본문·벡터는 있지만 DB에 없는 출처만 선별 적재 | `prepare load priorities verify search`, 또는 `resume` | 아래 배치 옵션, `--backend-root`, `--cases-per-family` |

모든 작업은 `status`도 지원합니다. `--phase stage vectorize`처럼 여러 단계를 지정하면 표시된 순서로 실행하고
첫 실패에서 중단합니다. `resume`과 `status`는 다른 단계와 함께 지정하지 않습니다.
`--plan`은 실행 전 미리보기이며 완료 여부를 인증하는 검증이 아닙니다. `missing resume --plan`은
기존 선택 목록과 체크포인트를 대조해 남은 단계를 계산합니다. 다른 작업은 지정한 단계를 그대로 표시합니다.

`--run`은 `var/recovery/<작업 폴더>`여야 합니다. 작업별 `plan.json`의 정책과 `recovery_job.json`의 작업 종류를
검사하므로 같은 폴더를 다른 작업에 재사용할 수 없습니다. 기존 정책·체크포인트도 인식합니다. 이미 생성한 계획은
덮어쓰지 않고 다음 단계에서 재사용합니다. 새로운 스캔/비교가 필요하면 새 작업 폴더를 선택합니다.
`excel`은 이미 만들어진 `targets.json`·격리 벡터가 필요하며, 전체 Excel 재전처리를 시작하는 명령이 아닙니다.

잠금 파일은 기존 명령과 공유합니다. 기존 배치가 같은 작업을 실행 중이면 거부합니다. 서로 다른 작업의 DB·공용 파일
반영에는 기존 `--after-receipt` 가드와 스냅샷·해시 검증이 그대로 적용됩니다. 기존 적재 대기 상태에서는
후속 단계를 실행하지 않고 종료 코드 2를 반환합니다. 실패 시 `receipt.json`에 실패 단계·오류를 남기고
성공 재시도 시 이전 오류는 `recovered_events`에 보관합니다.

원본 대조가 필요한 `decisions.json`을 자동 승인하지 않습니다. `--ocr`은 비교 후보만 생성하며, 원본 확인·페이지
검토 없이 후보를 채택하지 않습니다. `--recover`도 다운로드 이력의 공개 주소만 사용하며 로그인·접근 제한을 우회하지
않습니다. `formats process`의 완료는 격리 추출 작업의 완료이고 DB/공용 파일 반영은 `publish` 작업으로 따로 수행합니다.

## 실행 예시

아래 명령은 사용자가 실행할 예시입니다. 새 스캔에는 존재하지 않는 폴더명을 지정합니다.

```powershell
# 형식 점검 후 추출. 원본/공용 인덱스/DB는 변경하지 않음
$run = "var\recovery\attachment_formats_new"
.\.venv\Scripts\python.exe -m scripts.recovery --task formats --phase scan process --run $run --file-ext pdf --file-ext hwp
```

기존 ZIP 복구를 재개할 때는 계획을 다시 만들지 않습니다. `resume`은 가드·벡터·DB 확인 후 필요한 반영 단계를 수행합니다.

```powershell
$gate = "var\recovery\missing_ingest_20261107\receipt.json"
.\.venv\Scripts\python.exe -m scripts.recovery --task zip --phase resume --run var\recovery\zip_collision_repair_20261108 --after-receipt $gate
```

형식 복구 결과 중 검증된 항목을 선택하여 적재하려면 별도 반영 작업을 만듭니다. 본문 품질 복구는
`--kind quality`와 검토가 완료된 품질 작업 폴더를 지정합니다. 기존 완결 작업을 재실행하라는 의미는 아닙니다.

```powershell
$publishRun = "var\recovery\format_publication_new"
.\.venv\Scripts\python.exe -m scripts.recovery --task publish --phase prepare --run $publishRun --kind formats --source-run var\recovery\attachment_formats_new --after-receipt $gate
# 선택 목록·보류 사유를 검토한 후 실행
.\.venv\Scripts\python.exe -m scripts.recovery --task publish --phase vectorize apply publish verify --run $publishRun --after-receipt $gate
```

누락 적재의 `resume`은 DB 검증 후 검색까지 실행합니다. 완료 영수증·체크포인트가 일치하면 다시 적재하지 않습니다.
`prepare`는 기존 Excel 제외 목록 `var/recovery/excel_rebuild_20261107_032204/targets.json`을 계속 사용합니다.
배치 옵션 기본값은 `--workers 1`, `--chunk-batch-size 32`, `--source-batch-size 16`,
`--transaction-chunk-limit 256`, `--statement-timeout-seconds 600`, `--max-batch-retries 2`입니다.
Excel 적재는 기존대로 작업자 2개·청크 배치 256개가 기본이며 작업자 최대 2개, 배치 최대 512개입니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.recovery --task missing --phase resume --run var\recovery\missing_ingest_20261107 --workers 1 --chunk-batch-size 32 --plan
# 검토 후 위 명령의 --plan을 제거하면 실제 재개
```

`--backend-root` 기본값은 `D:/unira-backend/backend`입니다. 로컬 백엔드로 검색을 확인하려면 `--backend-root backend`를
지정합니다. 검색 검증은 실제 DB·검색 코드·모델을 사용하며 인증 HTTP·답변 생성·브라우저 화면 검증을 대신하지 않습니다.
이 CLI는 백엔드를 배포하거나 서비스를 재시작하지 않습니다.

## 기존 명령 호환과 검증

기존 `scripts.preprocessing.repair_*`, `review_text_quality`, `scripts.vectorization.reconcile_*`,
`resume_excel`, `publish_recovery`, `verify_recovery_search`의 명령 진입점은 통합 CLI로 위임합니다.
기존 모듈의 함수 import 경로도 유지합니다. 누락 적재 코드는 Git 제외 작업 폴더에서
`scripts/recovery/missing.py`로 옮겼으며, 기존 작업 폴더의 명령 파일은 호환 진입점으로 남겨 두었습니다.

변경 후 실행·테스트·DB 작업은 자동으로 수행하지 않습니다. 사용자 검증 명령은 다음과 같습니다.

```powershell
# 잠금, 잘못된 작업 선택, 무실행 미리보기, 실패 후 중단, 기존 복구 가드 회귀 검사
.\.venv\Scripts\python.exe -m pytest scripts\tests\test_recovery_control.py scripts\tests\core\test_attachment_schema.py -q -p no:cacheprovider
# 공유 전처리/수집기까지 전체 회귀 검사
.\.venv\Scripts\python.exe -m pytest scripts\tests backend\tests -q -p no:cacheprovider
```

`-p no:cacheprovider`는 기존 Windows `.pytest_cache` 접근 거부 문제를 피하도록 캐시 기록을 끕니다.
