# 강의계획서 PDF 직접 수집

공개 목록과 보고서 서버의 정상 생성·PDF 내보내기 요청을 Python의 직접 HTTP
요청으로 재현합니다.

강의계획서 수집 코드는 `scripts/crawlers/syllabus/`에서 관리합니다. 목록 요청, 보고서 생성,
PDF 검증, 파일 저장과 진행 이력을 이 패키지에서 처리합니다.

## 2026년 2학기 전체 수집

프로젝트 루트 PowerShell에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.crawlers.syllabus crawl --year 2026 --term 2
```

목록은 처음에만 생성하고 이후 재사용합니다. 기존
`files/pknu_syllabus/_state/catalog-2026-2.json`도 그대로 읽습니다.
학부와 대학원, 공개 목록에 게시된 국문·영문·공학인증 계획서 전체가 기본 대상입니다.
게재예정서는 포함하지 않습니다. 한 번에 하나씩 처리하고 PDF 사이에 기본 1초 대기합니다.

동일 명령으로 다시 실행하면 기존 다운로드 이력과 SHA-256을 확인하고 완료 건을
건너뜁니다.
`Ctrl+C`로 중단할 수 있으며, 한 건마다 검증과 이력 기록을 완료합니다.
기본 실행에는 다운로드 건수 제한이 없습니다.

서로 다른 학기의 `crawl`은 여러 터미널에서 동시에 실행할 수 있습니다. 학기별
`_state/run-<연도>-<학기>.lock`으로 중복 실행을 막고, 공통 다운로드 이력은 잠금 아래
최신 내용을 읽어 병합합니다. 여러 학기를 포함할 수 있는 `run`은 전체 실행 잠금을
사용하므로 다른 수집과 동시에 실행하지 않습니다. 학기별 오류는 이력의 `errors`에 남깁니다.

```powershell
# 우선 미수집 계획서 1건만 확인
.\.venv\Scripts\python.exe -m scripts.crawlers.syllabus crawl --year 2026 --term 2 --limit 1

# 특정 과목·분반만, 국문과 영문 저장
.\.venv\Scripts\python.exe -m scripts.crawlers.syllabus crawl --year 2026 --term 2 --course-no 104605 --section 101 --variants kor eng

# 새로 게시된 자료까지 확인하려면 목록 갱신
.\.venv\Scripts\python.exe -m scripts.crawlers.syllabus crawl --year 2026 --term 2 --refresh-catalog

# 진행 상태 및 실제 파일 해시 확인
.\.venv\Scripts\python.exe -m scripts.crawlers.syllabus status --manifest files/pknu_syllabus/_state/catalog-2026-2.json
```

기존 목록이 요청한 유형을 포함하면 그 목록에서 선택합니다. 요청 학기 또는 유형을
포함하지 않는 목록은 자동으로 덮어쓰지 않으며 `--refresh-catalog`를 안내합니다.
작업 ID와 성공 이력을 유지하며, 기존 원본 데이터와 실험 결과를 보존합니다.

## 설치와 범위

Python 3.12 프로젝트 `.venv`를 사용합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r scripts/crawlers/syllabus/requirements.txt
```

필요한 외부 라이브러리는 requests와 PyMuPDF입니다.

학기 값은 `1`, `summer`, `2`, `winter`, 유형 값은 `kor`, `eng`, `tech`입니다.
2021년 이후 보고서 방식을 대상으로 합니다. `--timeout 180`으로 요청 대기 시간을,
`--delay 2`로 PDF 사이 대기를 바꿀 수 있습니다. 로그인 요구·리디렉션·접근 거부는
우회하지 않고 중단하며 보류 상태와 해당 요청 단계를 기록합니다.

여러 학기 목록을 먼저 만든 뒤 수집할 수도 있습니다. `catalog`의 기본 범위는
한국 시간의 현재 연도를 포함한 최근 3개년이며, `--year`는 한 학년도만 선택합니다.

```powershell
# 2024~2026년, 정규·계절학기 목록
.\.venv\Scripts\python.exe -m scripts.crawlers.syllabus catalog --end-year 2026
.\.venv\Scripts\python.exe -m scripts.crawlers.syllabus run

# 2026년 2학기 목록만 별도로 생성
.\.venv\Scripts\python.exe -m scripts.crawlers.syllabus catalog --year 2026 --terms 2 --manifest files/pknu_syllabus/_state/catalog-2026-2.json
```

`catalog`는 PDF를 다운로드하지 않습니다. 정상적인 빈 학기와 서버 오류를 구분합니다.
`run`은 기본 `_state/catalog.json`을 사용하며 `--manifest`로 다른 목록을 지정합니다.
`--limit`는 이미 완료한 파일을 제외한 뒤 적용합니다.

## 저장과 검증

```text
files/pknu_syllabus/
  2026/2/<학수번호>_<분반>_<과목>_<유형>_<작업ID>.pdf
  _state/catalog-2026-2.json   # 한 학기 대상 목록
  _state/catalog.json         # catalog 명령의 기본 목록
  _state/downloads.json       # 성공 이력·과목 정보·SHA-256·마지막 오류
  _incoming/                  # 검증 후 저장 중인 PDF, 이력 기록 후 정리
```

임시 파일·목록·최종 PDF는 모두 프로젝트 `files/` 안에 둡니다. 다른 결과 경로는
`--output files/다른경로`로 지정할 수 있습니다. 기존 파일을 덮어쓰지 않습니다.

공개 EzReport 폼을 `/ReportingServer/embed`에 전송하고, Crownix 뷰어와 동일하게
파라미터 모드 3을 처리한 뒤 `opcode=500` PDF 내보내기와 다운로드를 요청합니다.
임의 JavaScript를 실행하지 않으며 프로그램이 만든 익명 세션만 사용합니다.
인증서·호스트명 검증을 유지하면서 기존 TLS 호환 어댑터를 두 대학 서버에만 적용합니다.

PDF 시그니처, 파싱, 첫 페이지의 학수번호·분반·학년도·학기·유형을 확인합니다.
국문·영문의 분리된 분반 항목과 공학인증의 `학수번호-분반` 결합 표기를 처리합니다.
HTML 오류 응답과 식별정보 불일치는 성공 처리하지 않습니다. 파일은 기본 50 MiB로
제한합니다. 전체 본문이 작성됐는지까지 보장하는 검증은 아닙니다.

완전히 쓴 임시 파일을 같은 파일시스템의 최종 위치에 원자적으로 연결하고 이력을
기록합니다. 상태 기록 전에 중단되어 원본만 남았다면 덮어쓰지 않고 중단하므로,
원본과 상태를 대조해 복구해야 합니다. `_state/run-<연도>-<학기>.lock` 또는 `run.lock`이
남으면 실행 중인 프로세스가 없는지 확인한 후 그 잠금 파일만 제거합니다. 오류가 발생하면 해당 항목에서 멈추며
완료한 파일은 유지합니다. 변경되거나 사라진 성공 파일도 조용히 재다운로드하지 않습니다.

Windows에서 상태 JSON 교체가 일시적인 파일 점유로 거부되면 이전 JSON을 유지한 채
최대 5초 재시도합니다. 계속 실패하면 오류를 그대로 반환하며, PDF와 임시 이력을 보존합니다.

## 검증 이력

2026-10-29에 직접 HTTP 요청으로 2026-2 국문(104605-101, 2페이지),
영문(104605-101, 2페이지), 공학인증(114970-101, 3페이지)을 실제 다운로드하고
식별정보를 확인했습니다. 전체 학기 수집 완료를 뜻하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m unittest scripts.tests.test_pknu_syllabus -v
```

공개 소스:
[조회 화면](https://irumi.pknu.ac.kr/nxui/uni/cour/open/LtPdocInq.xfdl.js),
[EzReport](https://irumi.pknu.ac.kr/nxui/nexacro17lib/component/EzReport/EzReport.js),
[Crownix Viewer](https://reporting.pknu.ac.kr/ReportingServer/html5/js/crownix-viewer.min.js).
