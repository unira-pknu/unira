# 수집 자료 전처리

복구·보수 작업은 [통합 복구 CLI](../recovery/README.md)의 `python -m scripts.recovery --task ... --phase ...`를
사용합니다. ZIP·첨부 형식·규정 출처·본문 품질 작업을 옵션으로 선택하며, 기존 명령은 같은 CLI로 위임합니다.

전체 실행 절차는 [전체 전처리 가이드](../../docs/preprocessing/PREPROCESSING.md)에 정리되어 있습니다.

텍스트 추출(`extractors/`), 정제(`text_cleaning.py`), 청크 생성
(`file_preprocessing.py`), 규정 처리(`rule.py`), 이미지 검토 및 레이아웃 구현을
이 패키지에서 관리합니다. 이전 전처리 경로의 호환 파일은 제거했습니다. 벡터화와 DB 적재는
`scripts/vectorization/`에서 관리합니다.

추출기의 정식 import 경로는 `scripts.preprocessing.extractors`입니다.
이전 `scripts/extractors/` 폴더는 제거했으며 호환 모듈을 제공하지 않습니다.

학과 전처리는 공통 파일 처리기를 사용합니다.
파일 단위 CLI는 `scripts.preprocessing.preprocess_files`를 사용하며
`--input-root`, `--output-root`를 명시합니다. `file_preprocessing`의 CLI도
동일한 공통 진입점으로 연결됩니다. 특정 학과를 기본 대상으로 선택하지 않습니다.

강의계획서 PDF는 `files/pknu_syllabus/_state/downloads.json`을 자동 연결하여
과목·교수·학과·학기·분반·유형과 공식 조회 화면 URL을 보존합니다. 작업 ID와
파일 경로를 대조하고 실제 PDF의 SHA-256을 검증하며, 이력이 없는 PDF는 거부합니다.
조회 화면 주소는 개별 PDF 직링크가 아닙니다. 세션별 보고서 다운로드 주소를
영구 첨부 링크로 사용하지 않습니다.

기존 전처리 문서의 본문·청크·식별자는 그대로 두고 출처만 복원하려면:

```powershell
.venv/Scripts/python.exe -m scripts.preprocessing.syllabus_provenance
.venv/Scripts/python.exe -m scripts.preprocessing.syllabus_provenance --apply
```

첫 명령은 검증만 수행합니다. `--apply`는 전체 검증 후 원본 JSON을
`var/recovery/syllabus_provenance/`에 ZIP으로 백업하고 provenance만 갱신합니다.
재실행 시 변경 없는 문서는 쓰지 않습니다. 기존 벡터 파일과 DB는 변경하지 않으므로
검색에 반영하려면 복원한 문서를 별도로 벡터화·적재해야 합니다.

강의계획서는 표 전용 추출기로 평가·교재·선수과목·주차별 계획의 셀과 좌표를
보존합니다. 기존 문서 반영과 검색·원문 링크 설계는
[강의계획서 검색](../../docs/backend/SYLLABUS_SEARCH.md)을 참고합니다.

XLS/XLSX는 원본 시트·행 번호·셀 좌표와 열 제목을 `structured_table`로 보존합니다.
다단 제목과 병합 셀의 원래 위치를 기록하고, 빈 셀을 삭제해 뒤의 열을 당기지 않습니다.
헤더를 확인하지 못한 표는 열 문자와 `header_status=unverified`를 사용하며 과목·교수
필드를 추측하지 않습니다. 검색용 청크는 한 행만 포함하고, 긴 행을 나눌 때는 같은
행의 과목·교수·열 제목을 반복합니다. 서로 다른 행 사이에는 overlap을 넣지 않습니다.
벡터화와 DB 적재에서도 구조 메타데이터와 본문의 줄바꿈을 유지합니다.
XLSX는 XML을 행 단위로 읽으며 서식만 있는 빈 셀·행을 메모리에 쌓지 않습니다.
실제 값과 수식이 있는 셀의 원본 좌표는 유지하고, 데이터 행 내부의 빈 열은 복원합니다.

새 전처리 결과는 `provenance.spreadsheet_policy_version=spreadsheet-rows-v1`을 기록합니다.
이 버전이 없는 기존 XLS/XLSX 결과는 원본 수정 시간이 같아도 `--changed-only`에서
재처리합니다. 원본 Excel은 변경하지 않습니다. 기존 검색 데이터에 적용하려면 해당
자료 범위의 전처리 후 벡터화·적재를 이어서 수행해야 합니다. 행 경계가 바뀌므로
이전 청크의 임베딩을 새 텍스트에 그대로 재사용해서는 안 됩니다.

프로젝트 루트의 Python 3.12 가상환경에서 실행합니다.

```powershell
# 읽기 전용: 등록된 학과 전체, 본교 전체, 강의계획서, 규정의 형식별 개수
.venv/Scripts/python.exe -m scripts.preprocessing.collected
# 처리 경로 확인: 청크 생성 없음, 실행 로그 기록
.venv/Scripts/python.exe -m scripts.preprocessing.collected --dry-run
# 전체 범위 청크 생성: 기존 최신 결과는 건너뜀
.venv/Scripts/python.exe -m scripts.preprocessing.collected --run
# 기존 개별 데이터셋 실행
.venv/Scripts/python.exe -m scripts.preprocessing.preprocessing_pipeline --dataset ce --dry-run
```

`collected`는 크롤링·벡터화·DB 적재를 실행하지 않습니다. 상태, 백업 및 파생
폴더를 제외하고 `type`과 `url`이 있는 수집 JSON을 본문으로 처리합니다.
HTML은 JSON과 중복되므로 공통 처리에서 제외합니다. 규정은 기존 전용 로직으로
JSON·HTML·첨부를 처리합니다. 독립 이미지 OCR은 별도 검토 대상으로 남겨두며,
본교 본문 이미지 OCR 금지 정책을 변경하지 않습니다.

점검 결과는 `document_json`, `other_json`, `html_snapshot`, `extractable`,
`image_separate_review`, `unsupported`로 구분합니다. `extractable`은 형식 지원을
뜻하며 모든 파일의 내용 추출 성공을 보증하지 않습니다.

전처리 결과는 `preprocessed/<dataset>/`에 모읍니다. 본교 전체는
`preprocessed/pknu_main/`, 강의계획서는 `preprocessed/pknu_syllabus/`입니다.
강의계획서는 다운로드 이력으로 복원한 과목·교수·학기·과정·분반과 공식 조회 화면 주소를
보존합니다. 백엔드는 교수별 강좌 목록을 이 메타데이터로 직접 조회합니다.
이 결과의 벡터화·DB 등록은 `scripts/vectorization/`의 별도 명령으로 수행합니다.

기존 `preprocessing_pipeline --dataset all`은 일일 수집 호환을 위해
CE·공지사항·대학생활·규정 4개 범위를 유지합니다. 전체 수집에는 `collected`를
사용하세요. `preprocessing_pipeline_recent`는 크롤링·벡터화·DB 적재도 포함합니다.

## ZIP 멤버의 저장 경로와 기존 충돌 복구

ZIP 내부 `자료/안내.pdf`와 `자료/안내.hwp`는 각각 `자료/안내.pdf.json`,
`자료/안내.hwp.json`으로 저장합니다. 내부 폴더와 원래 확장자를 유지하고,
`source_path=<ZIP 경로>!/<내부 경로>`를 출처 식별 기준으로 사용합니다.
동일 멤버 이름, Windows의 대소문자 별칭, 파일/디렉터리 충돌은 추출 전에 거부합니다.
다른 멤버의 결과가 이미 있으면 덮어쓰지 않습니다. 일부 멤버 추출 실패도 ZIP 전체
성공으로 표시하지 않습니다.

기존 JSON의 `source_path`가 같은 멤버 또는 확인된 원래 인코딩 이름과 일치할 때만
`slug`를 계승합니다. 일반 전처리의 기존 경로는 새 파일 저장 후 `.json.legacy`로
보존하여 JSON 입력 목록에서 제외합니다. `archive_member_path`, 원래 확장자,
멤버 SHA-256, 경로 정책 버전 및 이전 결과의 연결을 벡터와 DB 메타데이터에 전달합니다.

기존 충돌 복구에는 `repair_archive_collisions`를 사용합니다. 현재 전처리 결과가 가리키는
ZIP을 다시 대조하여 이전 `.json` 경로가 중복된 멤버 그룹만 선별합니다. 파일이 없거나
암호화·손상·경로 문제가 있는 ZIP은 `plan.json`의 `issues`에 남기며 자동 복구하지 않습니다.
본문/검색 청크가 없는 멤버는 적재 대상에서 제외합니다. 그런 멤버에 기존 DB 출처가
있으면 정상 데이터를 빈 결과로 대체하지 않도록 적용을 중단합니다.

아래 명령은 프로젝트 루트의 PowerShell에서 사용자가 직접 실행합니다. 매 단계가
성공한 뒤 다음 단계로 진행합니다. 2026-11-08에 실제 격리 산출물의 사전 검사와
관련 회귀 테스트를 실행했습니다. 기존 적재가 진행 중이어서 ZIP DB 반영·검증은 아직 수행하지 않았습니다.

```powershell
$env:PYTHONIOENCODING = "utf-8"
$zipRun = "var\recovery\zip_collision_repair_20261108"

# 테스트: 파일 충돌, 기존 출처 ID 유지, 실패 시 원본 보존, 동시 적재 차단
.\.venv\Scripts\python.exe -m pytest scripts\tests\core\test_attachment_schema.py scripts\tests\core\test_downstream_schema.py scripts\tests\test_recovery_control.py -q

# 기존 적재 중에도 가능: 공용 전처리/벡터/DB를 바꾸지 않는 격리 단계
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_archive_collisions prepare --run $zipRun
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_archive_collisions stage --run $zipRun
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_archive_collisions vectorize --run $zipRun
```

`prepare`가 선택 수·제외 사유를 출력하고, `stage`가 ZIP 원본을 재추출하며,
`vectorize`가 동일한 384차원 Sentence Transformers 모델로 해당 멤버만 임베딩합니다.
작업 디렉터리는 `var/recovery/` 아래를 지정합니다. `prepare`를 다시 실행하면 호환되는
기존 계획을 그대로 재사용하며 계획·진행 기록을 덮어쓰지 않습니다.
이후 단계 실패 시 같은 작업 디렉터리에서 실패 단계부터 재시도합니다.
재전처리하면 벡터화부터 다시 수행해야 합니다.

기존 누락 적재와 Excel 작업이 모두 완료된 뒤 아래를 실행합니다. 현재 두 작업의
`receipt.json`도 자동 확인하며 `complete` 또는 `completed`가 아니면 차단합니다.
다른 적재 작업이 있으면 해당 완료 기록도 `--after-receipt`로 추가합니다.
파일의 상태를 임의로 완료로 바꾸어 이 검사를 우회하지 마세요.

```powershell
$previousReceipt = "var\recovery\missing_ingest_20261107\receipt.json"
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_archive_collisions resume --run $zipRun --after-receipt $previousReceipt
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_archive_collisions status --run $zipRun
```

`resume`는 실제 격리 JSON·벡터·원본 ZIP을 먼저 검증하고, 이전 작업이 진행 중이면
오류 대신 `waiting_for_ingestion`을 표시합니다. 이 상태는 성공적인 DB 적재 완료가
아니며 공용 파일·DB를 바꾸지 않은 대기 상태입니다. 같은 명령으로 다시 재개하세요.
이전 작업 완료 후 적재·검증·공용 파일 반영을 이어갑니다. 단독 사전 검사는
`preflight --run $zipRun`으로 수행할 수 있습니다. 적재 전 본문 검사에는 출처 ID와
원본 경로가 일치하는 격리 JSON을 명시적으로 제공하여, 아직 없는 공용 JSON 경로에
접근하지 않습니다. 기존 일반 적재의 경로 검사 규칙은 유지합니다.

`apply`는 출처 ID·원본 경로·데이터셋을 대조한 멤버만 문서별 트랜잭션으로 반영합니다.
해당 문서의 청크만 교체하고 전역 중복 삭제나 검색 인덱스 제거는 하지 않습니다.
DB 출처 연결, 청크 수, 실제 본문 SHA-256 및 임베딩 모델·차원을 검증한 뒤에만
`publish`를 허용합니다. `verify --run $zipRun`으로 DB 대조를 다시 수행할 수 있습니다.
이 검증은 DB 적재 대조이며 자연어 검색이나 답변 생성 검사는 포함하지 않습니다.

`publish`는 기존 JSON·벡터와 대상 데이터셋 인덱스를 작업 폴더의 `backup/`에 보존하고,
수정 파일만 배치한 뒤 기존 전체 인덱스에서 해당 출처만 교체하여 병합합니다.
덮어써져 사라졌던 멤버가 복구되므로 청크가 늘 수 있으나 전체 데이터셋을 재임베딩하지 않습니다.
최종 `receipt.json.status=complete`와 `verified_sources`, `verified_chunks`, `issues`,
`excluded_members`를 함께 확인하세요. 성공 판정은 실제 사용자 실행 결과로 확인해야 합니다.

## 첨부파일의 실제 형식 판별과 공개 원본 복구

`repair_attachment_formats`는 저장된 첨부 이력의 경로·SHA-256·원본/최종 다운로드 URL과
로컬 파일 서명을 대조합니다. 저장 경로가 달라졌다면 일치하는 SHA-256으로 이력을 연결합니다.
일반 PDF·Office·HWP·ZIP 등 바이너리 원본과 첨부 이력에
연결된 파일을 검사합니다. 이미지 폴더의 독립 본문 이미지는 OCR 대상으로 추가하지 않습니다.
PDF 페이지 트리, OLE 문서 스트림, Office/HWPX ZIP 내부 이름·CRC, 이미지 디코더 등을
확인하며 확장자만으로 실제 형식을 확정하지 않습니다. 검사 한도나 의존성 문제로 검증하지
못한 파일은 손상 확정과 구분해 `unverified`로 기록합니다.

기존 적재 중에도 사용할 수 있는 격리 작업입니다. 원본·공용 전처리·벡터·DB·크롤러 상태를
변경하지 않고 `var/recovery/<작업명>/` 아래에 계획, 복구 사본, 추출 문서와 결과를 저장합니다.
이번 변경에서는 실제 검사·다운로드·추출·테스트를 실행하지 않았습니다.

```powershell
$env:PYTHONIOENCODING = "utf-8"
$formatRun = "var\recovery\attachment_formats_20261108"
.\.venv\Scripts\python.exe -m pytest scripts\tests\core\test_attachment_schema.py scripts\tests\core\test_downstream_schema.py -q

# 테스트 통과 후 각 단계가 성공하면 다음 단계 진행
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_attachment_formats scan --run $formatRun
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_attachment_formats process --run $formatRun --recover
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_attachment_formats status --run $formatRun
```

`scan --file-ext zip`처럼 확장자로 1차 범위를 좁힐 수도 있습니다. 전체 대조는 필터 없이 실행합니다.
`scan`은 같은 작업의 계획을 덮어쓰지 않습니다. 원본이 검사 후 바뀌면 `INPUT_CHANGED`로
처리를 보류하므로 새 작업 디렉터리에서 다시 검사해야 합니다. `process`는 같은 계획으로 재시도할 수 있습니다.

### 기존 첨부 복구의 중단 후 재시도

2026-11-09에 확인한 기존 `attachment_formats_20261108` 작업은 `results.json`을 교체하는
도중 Windows `WinError 5`로 중단된 기록이 있습니다. 새 `scan`으로 계획을 만들지 않고
같은 작업에서 아래를 실행하세요. 현재 코드의 실행·테스트는 사용자가 수행합니다.

```powershell
$formatRun = "var\recovery\attachment_formats_20261108"
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_attachment_formats process --run $formatRun --recover --retry-failed
if ($LASTEXITCODE -ne 0) { throw "첨부 복구 재시도 실패: 마지막 오류를 확인하세요." }
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_attachment_formats status --run $formatRun
```

재시도는 현재 부분 결과와 `results_before_retry_*.json`의 기존 결과를 연결합니다. 같은
원본 경로·SHA-256의 성공 기록만 후보로 사용하고 실제 원본·복구 사본의 해시, 추출 본문과
청크 수를 확인한 뒤 재사용합니다. 새 계획에 없는 기록과 변경된 원본은 성공으로 넘기지 않습니다.
작업 폴더의 결과 교체는 Windows 잠금/접근 거부(5/32/33)를 최대 8회 짧게 재시도합니다.
계속 실패하면 기존 대상 파일을 보존하고 오류를 반환합니다. 영구 ACL 문제를 해결한 것으로
간주하지 않으며, 파일 삭제나 권한 완화로 우회하지 않습니다.
이 작업은 격리 추출까지이며 공용 전처리·벡터·DB 반영은 수행하지 않습니다.
전체 후속 작업 순서는 [복구 작업 재개 절차](../vectorization/README.md#중단된-복구-작업의-실행-순서)를 따릅니다.

`process`는 원본 바이트를 작업 폴더에 복사한 뒤 해시가 같은지 확인하여 추출 중 파일 변경을 막습니다.
`process --recover`는 먼저 이력의 SHA-256과 일치하는 정상 로컬 사본을 확인합니다.
없으면 기록된 공개 다운로드 URL을 시도하고, 실패하면 원본 게시글에서 첨부 이름이 정확히
일치하는 링크가 하나일 때만 새 다운로드 주소를 사용합니다. 리디렉션마다 대상 범위를 확인하며
현재 수집 범위 밖의 외부 사이트·접근 거부·로그인 요구는 보류합니다. 쿠키/로그인을 우회하지 않습니다.
컨테이너 검사 후 실제 본문 파싱에서 손상이 드러난 경우에도 공개 원본을 한 번 다시 확인합니다.
추출기 미설치와 이미지 OCR 본문 없음은 재다운로드로 해결되는 손상으로 간주하지 않습니다.
강의계획서 조회 화면을 PDF 다운로드 주소로 가정하지 않으며, 세션이 필요한 경우 이력의 작업 ID와
`SESSION_DOWNLOAD_REQUIRED`를 남깁니다.

정상 파일은 실제 형식의 추출기로 처리합니다. 예를 들어 JPEG가 `.zip`으로 저장돼 있으면
첨부 이력이 확인된 경우 이미지 OCR을 사용하고, HWP/OLE가 `.zip`으로 저장돼 있으면
HWP 추출기에 올바른 확장자의 임시 사본을 전달합니다. 원본 파일명을 바꾸지 않습니다.
ZIP 원본을 복구하면 내부 멤버도 실제 형식을 판별하여 개별 결과를 기록합니다.

최종 `results.json.summary`와 `results`의 의미는 다음과 같습니다.

| 결과 코드 | 표시 | 판정 기준 |
|---|---|---|
| `processed` | 처리 성공 | 실제 형식으로 비어 있지 않은 본문 청크 추출. ZIP은 모든 멤버 처리 성공 필요 |
| `body_extraction_failed` | 본문 추출 실패 | 파일을 확보했지만 추출기/의존성 오류, 본문 없음, ZIP 일부 멤버 실패 등 |
| `unrecoverable` | 복구 불가 | 이번 실행에서 정상 사본을 확보하거나 출처를 확정하지 못함. 영구 유실 판정은 아님 |

제목·URL 등의 메타데이터만 생성한 결과는 처리 성공으로 세지 않습니다. HTTP 200이라도
HTML 로그인/오류 화면이면 복구 성공으로 보지 않습니다. `recovery_attempts`에는 URL별 실패,
게시글 확인 기록, 저장한 응답 스냅샷 경로를 남깁니다. 기존 SHA-256과 다른 정상 응답은
`requires_source_review=true`로 표시하므로 원본과 같은 버전이라고 단정하면 안 됩니다.
OCR은 `ocr_review_required=true`로 남기며, 본문 추출 성공이 표 구조·내용 정확성을 보장하지 않습니다.
`receipt.json.status=complete`는 작업 종료를 뜻하며 세 결과의 건수를 함께 확인해야 합니다.
벡터화·DB 반영은 이 작업에 포함하지 않습니다.

## 규정 출처 복원과 중복·연혁 정리

`repair_rule_sources`는 URL이 없는 규정 전처리 결과를 원본 JSON·HTML·첨부 SHA-256과
대조하여 문서명과 실제 수집 주소를 복원합니다. HTML은 수집 당시 파일명과 변환 후 slug를
모두 인덱싱하며, 첨부 경로의 연속 공백을 보존합니다. 제목/URL이 확인되지 않거나 첨부
해시가 수집 이력과 다르면 추측해 채우지 않고 적용을 중단합니다.

확인한 대상은 다음과 같습니다.

- `22e2ae9fff00.html`, `379dc11174e0.html`: 두 원본의 SHA-256이 같고,
  `schlPubRulId=2094548`, `schlPubRulSeq=2200000110025`, `efYd=20230101`이 일치합니다.
  학교의 두 wrapper 링크를 2026-11-08에 직접 확인했으며 모두 같은
  [부경대학교 학칙 연혁 문서](https://www.law.go.kr/LSW/schlPubRulInfoP.do?chrClsCd=010202&schlPubRulSeq=2200000110025&urlMode=schlPubRulLsInfoP)로 연결됩니다.
  서로 다른 개정본으로 합치지 않고 같은 버전의 중복 출처로 연결합니다.
- `국립부경대학교 한국어연수과정 운영지침(일부개정 2027.07.01  전문).pdf`:
  원본 게시글 `bbsId=2&no=719089`의 첨부 이력과 실제 PDF 해시가 일치합니다.
  게시글 제목은 `일부개정 2026.7.1.`, 추출 본문의 최종 부칙도 `2026.07.01.`이지만
  파일명에는 2027년이 있습니다. 부칙의 “공포한 날부터 시행”만으로 공포일을 확정하지 않고
  `effective_at=null`, `rule_effective_status=unconfirmed`와 충돌 근거를 보존합니다.
  게시글의 현재 온라인 본문은 웹 도구에서 확인하지 못했으므로 로컬 수집 원본과 이력을 근거로 합니다.

동일 규정의 별도 원본에는 `2200000153229`, `2026-02-27` 버전도 관측됩니다.
이것이 모든 연혁의 최신본이라는 주장이나 2023년판의 적용 종료일을 확정하는 근거는 아닙니다.
복구 작업은 해당 2023년 HTML과 연결된 JSON 표현을 과거 자료로 보존하고 기본 검색에서
제외하도록 DB 상태를 `inactive`로 지정합니다. 원본은 보존하고 과거 버전을 조회 제외 상태로 남깁니다.
단순 본문 일치만으로 다른 문서 ID·버전·시행일을 가진 규정을 중복으로 제거하지 않습니다.

현재 구성에서는 누락 URL 3건과 관련 JSON 표현을 함께 준비합니다. 정확한 선택 목록과
출처 ID는 실행 후 `documents.json`에서 확인합니다. 준비·벡터화는 격리 폴더에서 진행하므로
기존 적재 중에도 가능합니다. 이번 변경에서 작업 실행과 테스트는 하지 않았습니다.

```powershell
$env:PYTHONIOENCODING = "utf-8"
$ruleRun = "var\recovery\rule_sources_20261108"
.\.venv\Scripts\python.exe -m pytest scripts\tests\core\test_downstream_schema.py -q

# 테스트 통과 후 각 단계가 성공하면 다음 단계 실행
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_rule_sources prepare --run $ruleRun
.\.venv\Scripts\python.exe -m scripts.preprocessing.repair_rule_sources vectorize --run $ruleRun
```

기존 누락 적재와 Excel 적재의 완료 기록이 모두 `complete/completed`가 된 후 아래를
실행합니다. 확인된 규정 ID만 문서별 트랜잭션으로 갱신하며 전역 중복 삭제는 하지 않습니다.
출처 ID가 없거나 원본 경로/데이터셋이 다르면 덮어쓰지 않고 중단합니다.

```powershell
$gate = "var\recovery\missing_ingest_20261107\receipt.json"
foreach ($phase in @("apply", "publish", "verify", "status")) {
    .\.venv\Scripts\python.exe -m scripts.preprocessing.repair_rule_sources $phase --run $ruleRun --after-receipt $gate
    if ($LASTEXITCODE -ne 0) { throw "$phase 단계 실패" }
}
```

`apply`는 제목·URL·버전·확인된 시행일·청크 수·실제 본문 해시·임베딩을 대조합니다.
`publish`는 기존 결과를 백업하고 해당 출처만 공용 인덱스에 병합합니다.
`verify`는 기본값 `D:/unira-backend/backend`의 실제 검색 함수와 Supabase 규정 RPC를
호출하고 `/ask`와 같은 출처 카드 직렬화를 확인합니다. 다른 백엔드 코드 경로를 확인하려면
`--backend-root`를 지정하세요. 확인된 제목/공식 URL이 일치하는지, 수정한 과거/중복 출처가
기본 검색에서 제외되는지, 미확인 시행일이 새로 생기지 않았는지 검사합니다.
활성 복구 출처의 공식 페이지 제목과 실제 첨부 SHA-256도 다시 대조합니다. 접근 거부,
원문 변경, 제목/해시 불일치는 통과시키지 않으며 새 주소·시행일을 임의로 보충하지 않습니다.
내부 RPC 실패가 있으면 일부 결과가 반환되더라도 검증 실패입니다.
실제 생성 답변·브라우저 렌더링·인증된 운영 HTTP 요청까지 검증하는 명령은 아닙니다.
최종 완료 기준은 `receipt.json.status=complete`, `search_verified=true` 및
`search_verification.json.status=passed`입니다. 실제 검증 결과는 사용자 실행 후 확정됩니다.

## CE 로컬·DB 본문 불일치의 원본 대조와 선택 동기화

`scripts.vectorization.reconcile_ce`는 `ingestion_dataset=ce`의 기존 출처 중 실제 청크
본문 해시/위치가 다른 문서만 비교합니다. 누락 문서와 DB에만 있는 문서는 별도 작업입니다.
Excel 파일과 기존 Excel·누락 적재의 선택 목록은 제외합니다. 현재 불일치 건수가 기본값
4건과 다르면 선택 목록을 저장하고 중단합니다. 이전 점검의 4건이 지금도 같다고 가정하지
않으며, 건수 변경을 검토한 뒤 새 실행 폴더에서 `--expected-count`를 명시할 수 있습니다.

DB는 repeatable-read 읽기 전용 트랜잭션으로 비교하고, 선택한 원본·로컬 전처리·DB의
본문/메타데이터/벡터를 실행 폴더에 보존합니다. `probe`는 원본을 격리 폴더로 복사한 뒤
파일 서명에 맞는 추출기를 사용합니다. 기존 전처리 시각, 벡터화 시각, DB 적재/임베딩 시각,
청크 수와 길이, 본문 diff, 원본 텍스트 포함 범위, OCR 블록과 깨진 문자 지표를 기록합니다.
**DB의 `processed_at`은 현행 로더의 적재 시각이며, 전처리 시각이나 문서 개정일이 아닙니다.**
`comparisons.json.timeline`은 로컬 전처리·벡터화와 DB 임베딩·적재 시각을 별도로 기록합니다.
시간대가 없는 로컬 처리 시각은 KST로 해석하며, 잘못되거나 없는 시각은 순서 미확인으로 둡니다.
`db_embedding_later`는 DB 임베딩 작업이 더 늦었다는 뜻이며 문서 개정본이 더 최신이라는
뜻은 아닙니다. 처리 시각만으로 DB 본문을 로컬 버전으로 대체하지 않습니다.

```powershell
$env:PYTHONIOENCODING = "utf-8"
$ceRun = "var\recovery\ce_reconcile_20261108"
.\.venv\Scripts\python.exe -m pytest scripts\tests\test_recovery_control.py scripts\tests\core\test_downstream_schema.py -q
if ($LASTEXITCODE -ne 0) { throw "회귀 테스트 실패" }
foreach ($phase in @("compare", "probe")) {
    .\.venv\Scripts\python.exe -m scripts.vectorization.reconcile_ce $phase --run $ceRun
    if ($LASTEXITCODE -ne 0) { throw "$phase 단계 실패" }
}
```

위 단계는 기존 적재 중에도 수행할 수 있지만 원본/인덱스가 변경되면 중단합니다.
`comparisons.json`, 각 `snapshots/<출처ID>/body.diff`와 `original_blocks.json` 및 원본을
대조합니다. 공백을 제외한 원본 전체가 DB 청크와 정확히 일치하고 OCR·깨진 문자 문제가
없으면 DB 보존을 제안합니다. OCR 재추출 결과를 정답으로 간주하거나 처리 시각/글자 수만으로
우승 버전을 정하지 않습니다. 다운로드 이력의 첨부 해시가 달라도 자동 선택하지 않습니다.
ZIP 멤버는 이 작업에서 경로를 추정해 추출하지 않고 기존 ZIP 복구 절차로 넘깁니다.
`original_to_local.diff`, `original_to_database.diff`도 함께 생성해 재추출 원본과 양쪽 본문을
따로 대조합니다. 청크별 본문 해시·중복 본문 수·overlap을 포함한 글자 수를 기록합니다.
OCR 지표는 휴리스틱이므로 실제 표·숫자·문자 정확도를 보증하지 않습니다.
비교 증거 파일을 임의 수정하면 `stage`가 차단합니다. 판정은 `decisions.json`만 수정하고,
비교 자체를 갱신하려면 `probe`를 다시 실행하세요.

`decisions.json`의 `winner=review` 항목은 원본을 확인한 후 `database` 또는 `local`로
선택하고 `original_reviewed=true`, `reason`을 기록합니다. 미확인 항목은 `stage`가 차단합니다.
`raw_sha256`은 수정하지 않습니다. DB가 더 최근에 처리된 사례도 자동으로 로컬로 덮지 않습니다.

```powershell
# 기존 누락/Excel 적재 완료 후 실행. 다른 적재도 있으면 --after-receipt를 추가하세요.
$gate = "var\recovery\missing_ingest_20261107\receipt.json"
foreach ($phase in @("stage", "apply", "publish", "verify", "status")) {
    .\.venv\Scripts\python.exe -m scripts.vectorization.reconcile_ce $phase --run $ceRun --after-receipt $gate
    if ($LASTEXITCODE -ne 0) { throw "$phase 단계 실패" }
}
```

DB 선택 문서는 DB 행을 갱신하지 않고 해당 본문/벡터를 로컬에 복원합니다. 복원된 전처리는
`database_restored` 블록과 `restored_from_database`를 남기며, 원래 전처리 시각을 알 수 없어
`processed_at=null`로 기록합니다. DB 적재 시각을 전처리 시각으로 바꾸지 않습니다.
일반 벡터화 경로에서 동일 청크를 재생성할 수 없는 DB 버전은 공용 결과에 게시하지 않습니다.
로컬 선택 문서는 기존 벡터의 모델·차원·본문 연결을 검사하여 해당 출처의 청크만 교체하고
DB 출처 제목/URL/상태/우선순위는 보존합니다. 제목/URL 충돌은 별도 원본 확인이 필요합니다.
본문이 바뀌지 않은 기존 벡터를 재사용하므로 이 작업에는 새 임베딩 호출이 없습니다.

반영 직전에 선택한 로컬/DB 버전을 다시 대조하고, 변경되었으면 덮어쓰지 않습니다.
DB 선택 문서의 전체 스냅샷이 유지되는지 검사하며, 로컬 선택 문서는 전체 선택을 하나의
트랜잭션으로 반영합니다. 공용 결과는 원본 백업 후 선택한 출처만 병합합니다.
동기화가 일부 진행된 후에는 실패한 단계부터 실행하세요. 비교 이후 외부 변경이 확인된
경우에는 새 실행 폴더에서 다시 비교해야 합니다. 원본과 이전 비교/백업은 삭제하지 않습니다.

`verify`는 기본 `D:/unira-backend/backend`의 검색 코드로 실제 Supabase RPC와 재순위화를
호출합니다. 각 선택 출처가 검색되는지, 반환 본문·출처 URL이 선택 버전과 일치하는지 확인하고
내부 RPC 실패를 검증 성공으로 처리하지 않습니다. 본문 해시·청크 위치·float32 임베딩도
대조합니다. 최종 완료는 `receipt.json`의 `status=complete`, `search_verified=true`입니다.
공용 파일 게시 단계는 `published_pending_search`로 남으며, 검색 검사 전에는 완료로
표시하지 않습니다. 현재 작업 폴더에 비교 결과가 없으면 4건의 이름·권장 버전은 아직
확정되지 않은 상태입니다. 이번 요청에서는 파일 열람과 코드 수정만 수행하며
DB 조회·원본 추출·동기화·검색·테스트 실행은 모두 보류합니다.
실제 답변 생성·운영 HTTP 인증·브라우저 검증은 포함하지 않습니다. 코드를 준비했으며
이번 변경의 실행·테스트 결과는 사용자 실행 후 확정됩니다.

## 문자 깨짐과 LMS·ZOOM PDF의 원본 대조

### 현재 잔존 여부·적용 조건의 읽기 전용 점검

과거 `garbled_chunks`와 `review_required`는 해당 실행의 기록이며 현재 DB 상태가 아닙니다.
기존 실행에 `audit`를 사용하면 frozen plan의 원문 ID·청크 위치·텍스트 해시를 현재 자료와 대조합니다.
역사적 경로는 `scripts/artifact_paths.py`로 해석하며, 이전 receipt·계획·결정을 수정하지 않습니다.
원본 추출·OCR·임베딩·검색·동기화는 실행하지 않고 점검 결과를 표준 출력에만 표시합니다.
`audit`는 쓰기 단계와 함께 실행할 수 없으며 잠금/작업 표식/새 receipt도 생성하지 않습니다.

```powershell
$qualityRun = "var/recovery/text_quality_20261109_review2"
# 현재 로컬 인덱스 전체: 큰 데이터셋에서는 오래 걸릴 수 있음. 진행 상황은 표준 오류에 표시
.\.venv\Scripts\python.exe -m scripts.recovery --task quality --run $qualityRun --phase audit
# 빠른 조건 점검: 규정 인덱스만. 다른 데이터셋의 과거 청크는 not_scanned로 남음
.\.venv\Scripts\python.exe -m scripts.recovery --task quality --run $qualityRun --phase audit --audit-dataset rule
# 실제 DB: repeatable-read/read-only 트랜잭션. 활성 원문의 누락률과 과거 선택 청크의 활성/비활성 상태를 별도 확인
.\.venv\Scripts\python.exe -m scripts.recovery --task quality --run $qualityRun --phase audit --database
```

로컬 점검은 `vectorized/*/index.jsonl`을 읽습니다. 개별 벡터 JSON이나 전처리 원문이 남아 있는 것과
현재 인덱스에 포함되는 것은 다르며, 로컬 인덱스도 DB의 활성 상태를 보장하지 않습니다.
`--database`는 기존 PostgreSQL 연결 설정을 사용하고 임베딩을 조회하지 않습니다. 현재 트랜잭션의
관측 시각과 snapshot 식별자를 출력하며, 영구 DB 백업을 만들지는 않습니다.
`--audit-dataset` 필터는 DB의 현재 품질 집계 범위를 제한하며 과거 선택 청크 확인은 모든 선택 ID에 수행합니다.

적용 조건 집계는 원문 ID별로 묶고 다음을 구분합니다.

- `all_documents_missing_rate`: 전체 관측 문서 중 사용할 수 있는 조건 metadata가 없는 비율. 모든 문서가 그 조건을 필요로 한다는 뜻이 아님
- `condition_indicated` / `indicated_missing_rate`: 본문·제목에 조건 단서가 있거나 명시적인 조건 metadata가 있는 문서 중 구조화된 조건이 없는 비율. 규정 계열은 시행일 점검 대상으로 포함
- `not_indicated`: 자동 규칙에서 단서가 관측되지 않은 문서. 적용 대상 아님이나 누락 없음으로 확정하지 않음
- 청크별 metadata 미기재 수, 원문별 시행일 확인 상태, 게시 학과 `department_id` 기재 여부는 별도로 표시

학과 조건은 `applicable_department`, 학번 조건은 연도 목록인 `applicable_admission_years`, 시행일은
유효한 ISO 날짜 `effective_at`을 확인합니다. 명시적으로 `unconfirmed`인 시행일은 사용 가능으로 세지 않습니다.
유효한 날짜 문자열이 있다는 사실만으로 원문에서 시행일을 확인했다는 뜻은 아니므로
`rule_effective_statuses`도 함께 봐야 합니다. 게시일·파일명 날짜·강좌 개설 연도는 시행일/입학연도를 대신하지 않습니다.
게시 학과와 실제 적용 학과도 서로 대체하지 않습니다.

`current_replacement`는 과거 청크의 현재 U+FFFD 잔존 수이며, DB 점검에서는
`active_current_replacement`와 개별 `database_source_status`로 검색 노출 범위를 구분합니다.
청크가 없거나 비활성화되었다고 원본 복구가 완료된 것으로 표시하지 않습니다. 중복 위치에 다른 본문이 있으면
`ambiguous_duplicate`로 남깁니다. U+FFFD 탐지는 전체 OCR 정확도·모든 문자 깨짐을 측정하지 못합니다.

이 변경의 새 `audit` 명령·DB 조회·전체 로컬 감사·테스트는 에이전트가 실행하지 않았습니다.
필요한 회귀 검증 명령은 다음과 같습니다.

```powershell
.\.venv\Scripts\python.exe -m pytest scripts/tests/test_recovery_control.py
```

### 원본 비교와 승인된 복구

`probe --ocr`는 기본 32쪽을 넘는 PDF의 OCR을 보류하고 `ocr_deferred`로 남깁니다.
원본 손상이나 복구 불가 판정이 아니며, 별도 범위 검토 후 `--max-ocr-pages`로 상한을 지정합니다.
문서 시작 진행 상황과 `comparisons.partial.json` 중간 결과를 보존합니다. 완료된 대조의 오류만
다시 처리하려면 `probe --retry-errors`를 사용합니다. 기존 성공 후보의 해시를 확인해 재사용합니다.
`stage`의 `review_required`는 미검토 결정을 알리는 보호 장치입니다. 후보를 검토하기 전에
일괄 승인하거나 기대 개수에 맞춰 PDF를 임의로 선택하면 안 됩니다.

`review_text_quality`는 로컬 인덱스의 대체문자 U+FFFD(`�`) 포함 청크와 LMS/ZOOM PDF의
품질 경고를 선별합니다. 이전 101개 청크·3개 PDF는 실행 시 재확인할 기대값이며,
다르면 계획만 남기고 중단합니다. 이 수치는 DB의 현재 오류 개수나 전체 OCR 정확도가 아닙니다.
이번 요청에서는 코드만 수정했으며 검사·OCR·테스트·DB 조회·검색은 실행하지 않았습니다.

```powershell
$qualityRun = "var\recovery\text_quality_20261108"
.\.venv\Scripts\python.exe -m pytest scripts\tests\test_recovery_control.py scripts\tests\core\test_downstream_schema.py scripts\tests\core\test_attachment_schema.py -q
# 테스트 통과 후 각 명령이 성공하면 다음 단계로 진행
.\.venv\Scripts\python.exe -m scripts.preprocessing.review_text_quality scan --run $qualityRun
.\.venv\Scripts\python.exe -m scripts.preprocessing.review_text_quality probe --run $qualityRun --ocr
```

후보 선정이 다르면 `plan.json.pdf_candidates`에서 문서 ID·제목을 확인합니다. 검토한 3개의
`--pdf-source-id`를 반복 지정하거나 검토한 `--expected-chunks`, `--expected-pdfs`로
새 작업 폴더에서 다시 선별합니다. 계획 파일을 직접 바꿔 일치 검사를 우회하지 않습니다.

JSON/TXT는 정리 전의 원본 본문을 확인합니다. 원본 텍스트에 이미 `�`가 있으면
`replacement_present_in_source_text`로 기록하며 누락 글자를 추측하지 않습니다.
TXT는 UTF-8을 엄격히 해석하며 실패를 임의의 다른 인코딩으로 덮지 않습니다.
XLS는 원본 셀 값도 비교합니다. ZIP은 기존 전처리와 같은 파일명 디코더로 내부 경로가 정확히
일치하는 단일 멤버만 읽고, ZIP과 내부 원본의 해시를 보존합니다. 경로가 없거나 중복이면 추측하지 않습니다.
다른 바이너리 형식은 원본 손상/디코더 문제를 단정하지 않고 별도 검토로 남깁니다.
메타데이터/제목에만 깨짐이 있는 경우는 `prefix_or_metadata_only`로 구분합니다.

PDF는 정리 전 텍스트 레이어, 원시 OCR 출력, 페이지별 이미지와 빈/미인식 페이지를
나란히 보존합니다. OCR의 `preserve_raw_text=True` 검토 경로는 반복 물음표를 지우지 않고
UTF-8 오류도 무시하지 않습니다. 기존 일반 OCR의 기본 동작은 변경하지 않습니다.
원시 텍스트가 깨지고 OCR이 깨끗해 보여도 `text_layer_or_ocr_problem_visual_review_required`일
뿐입니다. 원본 이미지 자체의 판독 불가와 추출 오류를 구분하려면 페이지 이미지를 확인해야 합니다.
파일 부재·암호화·파싱 오류는 복원 불가 확정이 아닌 미확인 사유로 기록합니다.

`comparisons.json`과 `sources/<dataset>/<source_id>/`의 원본 사본·페이지 이미지를 확인해
`decisions.json`에 다음 중 하나를 지정합니다. 자동 언어모델 보정이나 새 문장 입력은 지원하지 않습니다.

- `retain`: 기존 결과 유지. 미확인 상태를 원본 복원 성공으로 표시하지 않습니다.
- `exclude_chunks`: 확인한 후보 위치만 `exclude_positions`에 지정. 원래 텍스트를 삭제하지 않고
  `search_excluded=true`와 사유를 격리 전처리 결과에 기록합니다.
- `replace_from_source`: 깨짐 없는 JSON/TXT/XLS 원본에서 생성한 `candidate=source`만 선택합니다.
- `replace_pdf`: `candidate=native` 또는 `ocr`를 선택하며 `reviewed_pages`에 모든 페이지를
  기록합니다. 후보에서 빈/미인식 페이지는 실제로 빈 페이지인지 확인한 경우에만
  `approved_blank_pages`에 넣습니다. 내용 있는 페이지가 빠졌다면 그 후보를 채택하지 않습니다.

유지 외의 선택은 `original_reviewed=true`와 구체적 `reason`이 필요합니다. 후보 해시·원본 해시와
로컬 청크/인덱스 정렬이 검증되지 않으면 변경을 차단합니다. `review` 항목이 남으면 stage를
중단하므로, 원본 손상으로 복원이 불가능한 경우도 유지/검색 제외 사유를 명시해야 합니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.preprocessing.review_text_quality stage --run $qualityRun
.\.venv\Scripts\python.exe -m scripts.preprocessing.review_text_quality status --run $qualityRun
```

위 명령은 `var/recovery/<작업명>/preprocessed/<dataset>/`의 격리 산출물까지만 만듭니다.
공용 전처리와 DB는 변경하지 않으며 최종 상태도 `staged_pending_scoped_sync`입니다.
일반 벡터화와 적재기는 명시적으로 검색 제외된 청크를 재포함하지 않도록 검사하지만,
**이미 DB에 있는 청크가 이 코드 변경만으로 사라지는 것은 아닙니다.** 검토된 출처만
최신 DB와 재대조한 뒤 게시·재벡터화·선별 동기화하고, 정상 청크/출처가 남고 제외 청크는
검색되지 않는지 확인하는 후속 단계가 필요합니다. 격리 인덱스를 전체 데이터셋 대체본으로
적재하거나, 깨진 문자만 삭제해서 복원 성공으로 세면 안 됩니다.

## 의존성과 처리 한계

스캔 PDF 처리를 위해 **Tesseract OCR을 별도로 설치**해야 합니다. 기본 OCR 언어는
`kor+eng`이므로 두 언어 데이터가 모두 필요합니다. Python 패키지 설치와는 별개입니다.

```powershell
winget install --id UB-Mannheim.TesseractOCR --exact --source winget
# 설치 후 새 터미널에서 실행 파일과 언어 데이터 확인
tesseract --version
tesseract --list-langs
```

설치 폴더(일반적으로 `C:\Program Files\Tesseract-OCR`)를 PATH에 추가해야 합니다.
언어 목록에 `kor`가 없으면 [공식 한국어 언어 데이터](https://github.com/tesseract-ocr/tessdata_fast/blob/main/kor.traineddata)를
설치 폴더의 `tessdata/kor.traineddata`에 저장하세요.

- JSON, PDF, DOCX, HWP/HWPX, CSV, XLS/XLSX, PPTX, TXT, ZIP을 지원합니다.
- PDF는 PyMuPDF, DOCX는 python-docx, XLS는 xlrd, PPTX는 python-pptx,
  HWP는 pyhwp, HTML은 beautifulsoup4가 필요합니다. XLSX는 표준 ZIP/XML
  추출기를 사용하므로 openpyxl이 필요하지 않습니다.
- DOC는 pywin32와 Microsoft Word가 필요합니다. 기존 변환기는 원본 옆에
  DOCX를 생성하므로 DOC 일괄 실행 전 변환 환경을 확인하세요.
- 스캔 PDF는 PATH의 Tesseract와 kor/eng 언어 데이터가 필요합니다.
  OCR이 불가능하면 내용 누락 또는 메타데이터만 생성될 수 있습니다.
- ZIP은 지원 멤버만 처리하며 멤버 수·크기를 제한합니다. 다른 압축 형식,
  구형 PPT, XLSB, 영상 등은 자동 처리하지 않습니다.
- 성공 로그만으로 본문 추출 성공을 판단하지 마세요. 결과의
  `used_metadata_fallback`, 청크 내용, ZIP 내부 실패도 확인해야 합니다.
- 읽기 전용 점검은 범위·형식·기본 의존성을 확인합니다. 암호화·손상·인코딩·
  텍스트 품질 검증은 실제 추출이 필요합니다. 이번 정리에서는 전체 일괄 전처리를
  실행하지 않았으며 표본 추출과 회귀 테스트로 검증했습니다.
