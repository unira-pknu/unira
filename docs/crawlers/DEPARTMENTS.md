# 학과·단과대학 홈페이지 크롤러

단과대학·학부·학과·전공 홈페이지를 수집하는 공통 크롤러입니다.
모든 명령은 프로젝트 루트에서 PowerShell로 실행합니다.
진입점은 [engine.py](../../scripts/crawlers/departments/engine.py), 수집 설정은 [registry.json](../../scripts/crawlers/departments/registry.json),
주소 카탈로그는 [sites.csv](../../scripts/crawlers/departments/sites.csv)입니다.

메인 홈페이지·학칙/규정 등 다른 크롤러는 [전체 크롤러 실행 가이드](README.md)를 참고하세요.
아래 대상 수와 TLS 검증 기록은 **2026-10-25 기준**이며, 문서는 2026-10-26에 정리했습니다.

## 실행 준비

Python 3.12.10과 전체 requirements로 검증했습니다.
기존 검증 환경 `.venv`가 있으면 설치 과정을 생략할 수 있습니다.
새 환경을 준비할 때는 설치된 Python 3.12 실행 파일로 가상환경을 생성하세요.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

`requirements-runtime.txt`는 API 서버용이므로 크롤러에는 전체 `requirements.txt`가 필요합니다.
명령 예시는 Python 버전 혼동을 피하도록 가상환경 경로를 명시합니다.
검증 당시의 테스트 결과와 로컬 진단 자료는 이 문서의 마지막 절에 정리했습니다.

## 실행 명령

```powershell
# 구성 및 카탈로그 연결 검증
.\.venv\Scripts\python.exe -X utf8 -m scripts.crawlers.departments.cli validate

# 활성 대상 확인 / 보류·주소 없음까지 포함한 전체 카탈로그 대조
.\.venv\Scripts\python.exe -X utf8 -m scripts.crawlers.departments.cli list-ready
.\.venv\Scripts\python.exe -X utf8 -m scripts.crawlers.departments.cli coverage

# 활성 학과 전체 실행
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --all --once

# 특정 학과만 실행: ce를 해당 데이터셋 별칭으로 변경
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset ce --once

# 전체 학과의 게시판별 최근 목록 1페이지 확인 (정적 페이지도 수집)
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --all --once --recent-only 1

# 소량 확인: 첫 게시판 1개, 문서 최대 2개, 첨부 다운로드 생략
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset ce --once --recent-only 1 --max-board-sections 1 --max-items 2 --no-download-files
```

`coverage`는 접근 거부·주소 없음·미등록·미설정이 있으면 종료 코드 1을 반환합니다.
각 행의 사유를 확인하세요. `list-ready`는 로컬 설정 기준이며 실제 접속 성공을 보장하지 않습니다.
최신 데이터셋 별칭은 `list-ready` 또는 [등록 설정](../../scripts/crawlers/departments/registry.json)에서 확인할 수 있습니다.

| 옵션 | 의미 |
| --- | --- |
| `--all` / `--dataset NAME` | 전체 활성 데이터셋 / 단일 데이터셋 선택 |
| `--once` | 한 번 실행하고 종료. 현재 기본 동작과 동일 |
| `--recent-only N` | 게시판별 최근 목록 N페이지로 제한. 게시글 N개가 아님 |
| `--section ID` | 등록된 활성 메뉴 하나만 실행. `--all`과 함께 사용 불가 |
| `--max-board-sections N` | 앞쪽 게시판 N개만 선택하고 정적 페이지 생략 |
| `--max-items N` | 데이터셋별 처리 문서 수 제한 |
| `--no-download-files` | 첨부 메타데이터 보존, 파일·본문 이미지 다운로드 생략 |
| `--batch-report PATH` | 전체 실행 배치 보고서 경로 지정 |
| `--reset-state` | 증분 상태 초기화 후 수집. 일반 재실행에는 불필요 |

`--section`과 `--max-board-sections`는 함께 사용할 수 없습니다.
기존 결과를 이어 수집할 때는 상태를 유지하고 같은 명령을 다시 실행합니다.
수집은 로컬 파일 저장 단계이며 임베딩·Supabase 적재는 별도 작업입니다.

## 2026-11-16 누락 게시판 등록

홈페이지 메뉴와 실제 게시판 ID를 대조해 다음 게시판을 활성 section으로 추가했습니다.
기존 학과 데이터셋과 저장 경로를 사용하며, 학과 전체 실행과 `--all`에도 포함됩니다.

| 데이터셋 | 게시판 | section ID | 경로 | 게시판 ID |
| --- | --- | --- | --- | --- |
| `civil` | 토목공학전공 학부 공지사항 | `menu_5345` | `/civil/5345` | `2402699` |
| `civil` | 토목공학전공 대학원 공지사항 | `menu_5372` | `/civil/5372` | `2402700` |
| `ce` | 컴퓨터·인공지능공학부 진로·취업 공지사항 | `career_notice` | `/ce/1813` | `2400535` |
| `masscom` | 언론정보전공 사이트의 기존 공지사항 | `menu_3119` | `/comm/3119` | `2401369` |

등록 변경만으로 기존 수집 결과가 갱신되지는 않습니다. 구성 검증과 새 게시판만의 수집은
프로젝트 루트 PowerShell에서 사용자가 실행합니다. 기존 증분 상태를 초기화할 필요는 없습니다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.crawlers.departments.cli validate

.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset civil --section menu_5345 --once
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset civil --section menu_5372 --once
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset ce --section career_notice --once
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset masscom --section menu_3119 --once
```

위 수집 명령은 최근 목록 제한 없이 해당 게시판의 전체 목록을 확인하고 첨부파일도 다운로드합니다.
먼저 소량만 확인하려면 각 수집 명령에 `--recent-only 1 --max-items 2 --no-download-files`를 추가하세요.
소량 확인 뒤에는 제한 옵션을 제거한 같은 section 명령으로 전체 범위를 수집합니다.
이번 변경은 기존 `numeric_cms` 파서를 재사용하는 설정 추가이며, 크롤링과 테스트는 실행하지 않았습니다.
기존 구성 회귀 테스트와 전체 회귀 테스트는 다음 명령으로 확인합니다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest scripts.tests.departments.test_department_config
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s scripts/tests -t . -p "test_*.py"
```

## 2026-11-17 학과별 핵심 메뉴 등록

공개 홈페이지의 메뉴 계층과 실제 이동 목적지를 확인해 교육과정·졸업요건·교수진 메뉴를 보완했습니다.
학부·대학원, 전공별 교육과정, 모듈형·마이크로전공·학생설계전공의 하위 메뉴도 포함합니다.
84개 데이터셋에 451개 section을 추가했으며, 447개는 활성이고 원본 본문이 빈 4개는 비활성 보류입니다.
교육과정 296개, 졸업요건 57개, 교수진 98개이며, 교육과정 수에는 비활성 보류 4개가 포함됩니다.

| 소속 | 보완 데이터셋 | 활성 추가 | 원본 공백 보류 |
| --- | ---: | ---: | ---: |
| 인문사회과학대학 | 13 | 80 | 0 |
| 자연과학대학 | 6 | 29 | 0 |
| 경영대학 | 3 | 21 | 0 |
| 공과대학 | 23 | 128 | 0 |
| 수산과학대학 | 10 | 43 | 0 |
| 환경·해양대학 | 8 | 48 | 0 |
| 정보융합대학 | 15 | 76 | 0 |
| 미래융합학부 | 4 | 10 | 4 |
| 글로벌자율전공학부 | 1 | 6 | 0 |
| 학부대학 | 1 | 6 | 0 |

기존 section의 ID·경로·설정과 데이터셋 저장 경로는 유지합니다.
동일한 이동 목적지를 가리키는 상위 메뉴는 대표 section으로 모았고,
확인한 이동 별칭 40개를 `menu_aliases`에 추가했습니다.
예를 들어 물리학과는 상위 교과과정·졸업요건 주소 대신 학부·대학원의 실제 하위 페이지를 각각 등록했습니다.
컴퓨터·인공지능공학부에는 기존 전공 교육과정에 더해 융합·마이크로·학생설계전공 교육과정 19개를 등록했습니다.

등록은 수집 완료를 뜻하지 않습니다. 기존 파서를 재사용하는 설정 변경이며, 크롤러·테스트·DB 작업은 실행하지 않았습니다.
공개 HTML에서 확인한 이미지 중심 본문, 별도 본문 영역, iframe 등의 제한은 각 신규 section의 `notes`에 남겼습니다.
이 메모는 검토용 설정이며 파서를 수정하거나 OCR을 실행하지 않습니다.
특히 영어영문학부의 `.e_sbCont #sub`, `#stance` 계열과 기존 선택자로 본문을 확인하지 못한 페이지는
파서 보완 및 실제 저장 결과 확인이 필요합니다. 교수진 등록은 메뉴에 있는 목록·구분 페이지 범위이며,
목록의 모든 교수 상세 프로필을 자동으로 따라가도록 바꾼 것은 아닙니다.
이 문단은 메뉴 등록 시점의 기록입니다. HTML 영역·이미지·교수 상세 프로필의 후속 보완은
아래 **2026-11-18 본문·이미지·교수 상세 프로필 처리**를 참고하세요.

### 원본 공백으로 비활성 등록한 메뉴

| 데이터셋 | section ID | 경로 | 사유 |
| --- | --- | --- | --- |
| `safe` | `menu_6438` | `/safe/6438` | 교과목별 역량: 공개 본문에 제목만 있음 |
| `safe` | `menu_6439` | `/safe/6439` | 교육과정표: 공개 본문에 제목만 있음 |
| `welfare` | `menu_7420` | `/Welfare/7420` | 교과목별 역량: 공개 본문에 제목만 있음 |
| `welfare` | `menu_7421` | `/Welfare/7421` | 교육과정표: 공개 본문에 제목만 있음 |

위 네 페이지는 제목 외 본문·첨부·이미지가 없는 것을 확인했습니다. 원본 내용이 게시된 뒤 활성화를 검토합니다.
기존 사용자 제외·보류 항목과 `humanict`·`dmfbe`의 상태는 유지합니다.

### 별도 연결·응답 검토가 필요한 메뉴

| 데이터셋 | 메뉴 주소 | 확인한 연결·제한 |
| --- | --- | --- |
| `english` | `/english/3845` | 교육대학원 `https://pknuedu.pknu.ac.kr/pknuedu/1383`으로 302 이동. 다른 홈페이지의 범위·TLS 검토 필요 |
| `chinese` | `/chinese/4293` | 학위수여 규정이 국가법령정보센터로 이동. 외부 규정 원문을 학과 자체 본문으로 등록하지 않음 |
| `industrial` | `/industrial/3854` | 교과과정이 `https://icms.pknu.ac.kr/visual/3678`로 이동. 다른 전공의 내용을 중복 활성화하지 않음 |
| `ice` | `/pknuice/5083` | 강의계획서 연결 요청의 응답 대기 시간 초과. 주소·접근 상태 추가 확인 필요 |

토목공학전공의 상위 `/civil/5304`는 교육목적·인재상으로 이동하므로 교육과정으로 등록하지 않았습니다.
실제 전공교육과정 `/civil/5331`과 그 하위 메뉴를 등록했습니다.

### 사용자 실행 확인

프로젝트 루트 PowerShell에서 구성 검증과 기존 회귀 테스트를 실행합니다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.crawlers.departments.cli validate
.\.venv\Scripts\python.exe -X utf8 -m unittest scripts.tests.departments.test_department_config
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s scripts/tests -t . -p "test_*.py"
```

학과별 실제 section ID·경로·검토 메모는 레지스트리에서 확인합니다.

```powershell
$registry = Get-Content scripts/crawlers/departments/registry.json -Raw -Encoding UTF8 | ConvertFrom-Json
($registry.departments | Where-Object dataset -eq 'physics').sections |
    Where-Object { $_.category -in @('교육과정', '교수진') } |
    Select-Object id,name,path,enabled,notes

# 먼저 표·본문이 있는 신규 정적 페이지 한 개만 확인
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset physics --section menu_449 --once --no-download-files
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset ce --section menu_4955 --once --no-download-files

# 파서 보완이 필요한 항목과 소량 결과를 확인한 후 학과 전체 수집
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset physics --once
```

`--no-download-files`를 제거하면 첨부파일·본문 이미지도 다운로드합니다.
활성으로 등록한 새 section은 기존 학과 전체 실행과 `--all`에 자동 포함되며, 증분 상태 초기화는 필요하지 않습니다.

## 2026-11-18 본문·이미지·교수 상세 프로필 처리

등록된 핵심 메뉴의 본문과 이미지가 같은 영역에서 추출되도록 어댑터에
`select_body_root`를 추가했습니다. 저장된 HTML과 공개 페이지에서 확인한 구조를 기준으로 보완했습니다.

| 구조 | 보완 내용 |
| --- | --- |
| 숫자 메뉴 CMS | 영어영문학부의 `.e_sbCont #sub`, `#stance`를 일반 레이아웃보다 먼저 선택. 기존 학과별 `.a_bdCont`·`.c_bdCont` 선택 규칙 유지 |
| 정보융합대학 PHP 교수진 | 교수 사진이 있는 `.container`를 본문으로 선택. 헤더·푸터와 다른 전공 선택 탭을 본문 텍스트에서 제외 |
| 공간정보 PHP | `.page_con`을 바깥 `#container`보다 먼저 선택. 표의 행·열 구분과 첨부·참조 링크 보존 |
| query 메뉴·게시판 | `.sub-content`, `.page-cont`, `.editor-data-box`, `.boardContents` 등 각 파서의 본문 영역을 이미지 수집에도 적용 |

크롤러는 선택한 본문 안의 이미지 원본을 기존 SHA-256 저장 방식으로 보존합니다.
이미지에 더 큰 원본으로 가는 링크가 있으면 원본을 대상으로 삼고, 표시용 이미지 URL도
`metadata.body_images[].display_url`에 남깁니다. 지연 로딩 이미지도 같은 본문에서 확인합니다.
제목만 있고 본문이 이미지인 페이지는 `IMAGE_ONLY_REQUIRES_OCR` 경고와 `partial_success`로 기록합니다.
이미지 원본 보존은 이미지 속 텍스트 추출 완료를 뜻하지 않습니다.

기존 이미지 OCR 전처리도 저장 HTML에서 같은 어댑터의 본문 영역을 사용합니다.
이전 원본을 재사용할 때 이미지 수뿐 아니라 순서와 URL을 대조하며, OCR 전에는 파일 해시를 확인합니다.
원본 크롤링 JSON·HTML·상태 파일은 OCR 전처리에서 수정하지 않습니다.
메인 홈페이지의 본문 이미지 OCR 제외 정책은 유지합니다.

### 교수 상세 프로필

`교수진` 카테고리의 숫자 메뉴 CMS 정적 페이지에서는 같은 메뉴의
`?action=view&no=...` 또는 `?action=view&profile=...` 링크를 한 단계 따라갑니다.
물리학과의 `.tch_list` 목록과 `.tch_in`·`.a_tch_name` 상세 구조처럼 확인된 형식을 지원합니다.
프로필에서는 교수 이름을 제목으로 사용하고, 연락처·학력·경력·연구분야 등의 본문과 표를 저장합니다.
목록의 탭 이동 버튼은 제거하되 HTML 안에 있는 상세 탭 본문은 유지합니다.

상세 문서는 기존 정적 문서 ID·저장 방식을 사용하며, 이미 등록된 상세 URL은 그 URL을 유지합니다.
`metadata.profile_list_url`에 목록 출처를, 목록 문서의 `metadata.faculty_profiles`에는 각 링크의
`collected`·`failed`·`already_collected`·`not_attempted` 상태를 기록합니다.
한 실행 안에서 같은 URL을 중복 요청하지 않습니다.
상세 요청이 다른 메뉴·다른 교수·외부 호스트로 이동하면 다음 요청 전에 차단합니다.
같은 메뉴와 교수 ID를 유지하는 `icms.pknu.ac.kr` 연결은 허용합니다.
상세 요청에 목록이나 로그인 화면이 반환되면 기존 상세 문서를 덮어쓰지 않고 실패를 기록합니다.

목록당 최대 100개 상세 링크를 처리하며, `--max-items`에는 목록과 상세 문서가 함께 포함됩니다.
제한으로 방문하지 못한 링크는 `PROFILE_DETAILS_LIMITED` 경고와 사유를 남깁니다.
`--no-download-files`는 상세 문서의 첨부·이미지에도 적용됩니다.
외부 연구실 홈페이지, JavaScript로만 여는 프로필, 다른 교수진 메뉴·전공 탭, 외부 iframe 본문은
자동으로 따라가지 않습니다. iframe·동영상 주소는 참조로 보존합니다.

### 검증 및 사용자 실행

이 보완에서는 코드·회귀 테스트를 추가하고 `git diff --check`를 확인했습니다.
테스트·재크롤링·OCR·DB 작업은 실행하지 않았습니다. 기존 병렬 수집과 같은 데이터셋을
중복 실행하지 않도록 해당 작업이 끝난 뒤 프로젝트 루트에서 실행하세요.

후속 2026-11-18 사용자 요청으로 설정 검증과 전체 회귀 테스트를 실행했습니다.
설정 검증은 `valid`, 회귀 테스트는 515개 통과입니다. 새 함수 인자와 응답 URL을 반영하지 못한
기존 테스트 대역 3곳을 수정한 뒤 다시 확인했습니다. 카탈로그 대조의 홈페이지 없음·링크 허브·접근 거부
3건은 기존 예외로 남아 있습니다. 실제 물리학과 교수 목록·상세 3건의 샘플 수집도 오류 없이 확인했고,
기존 실행과 겹치지 않는 범위별 재수집 및 종료 후 누락 대조를 시작했습니다. 별도 전처리·OCR·DB 작업은 실행하지 않았습니다.

```powershell
# 구성 검증과 전체 회귀 테스트
.\.venv\Scripts\python.exe -X utf8 -m scripts.crawlers.departments.cli validate
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s scripts/tests -t . -p "test_*.py"

# 교수 목록과 상세 2개까지: 원본 파일 다운로드를 생략한 소량 확인
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset physics --section menu_303 --once --max-items 3 --no-download-files

# 서로 다른 HTML 본문 영역 확인
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset english --section menu_3826 --once --no-download-files
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset itc --section html_html_03_01_php_category_1 --once --no-download-files

# 이미지 원본도 보존하여 재수집
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset geoinfo --section php_03specialty_02_php --once

# 이미지 전용 문서의 OCR 소량 확인: Tesseract와 kor+eng 언어 데이터 필요
.\.venv\Scripts\python.exe -X utf8 -m scripts.preprocessing.preprocess_body_images --dataset geoinfo --limit 3
```

샘플을 확인한 뒤 교수 목록 명령에서 `--max-items`·`--no-download-files`를 제거하면
등록된 같은 메뉴의 상세 프로필과 원본 파일을 수집합니다.
혼합 본문에 포함된 이미지도 OCR 대상으로 선택하려면 전처리 명령에 `--all-body-images`를 추가합니다.
OCR 결과는 `preprocessed/<dataset>/body_images/`에서 검토하며, 자동으로 RAG 본문에 합치지 않습니다.
기존 저장 결과는 코드 수정만으로 갱신되지 않습니다.

### 정적 본문 추출 실패 75개 후속 보완

2026-11-18 `scope_retry_20261118_030524/manifest.json`의
`completion_audit.remaining_static_gaps` 75개에 대해 **저장된 원본 HTML을 로컬에서 대조**했습니다.
이번 보완에서 학교 홈페이지에 추가 요청을 보내거나 기존 수집 결과를 갱신하지 않았습니다.

| 저장 HTML에서 확인한 원인 | 대상 수 | 코드 보완 또는 남은 조치 |
| --- | ---: | --- |
| `.d_sbCont` 본문 누락 | 37 | 사회복지·정치외교·전기·산업경영·평생교육 등 d 템플릿 본문 선택 추가 |
| `.b_bdCont` 교수진 본문 누락 | 5 | 냉동공조·수산과학대학 자유전공·학부대학 교수진의 본문과 상세 링크 탐색에 적용 |
| 전용 본문 클래스 없이 `#sub > .container`에 내용 배치 | 29 | 기존 검증된 7개 호스트에서만 대체 선택. 중첩 `#sub`의 바깥 사이드바 래퍼를 제외 |
| `gic / menu_8917` 가이드북 언어 링크 | 1 | 제목 태그 안의 실제 링크를 빈 제목으로 오판하지 않도록 보완. 목적지는 참조로 보존 |
| `geoinfo / php_01about_05_php` 학과소개 동영상 | 1 | 빈 `iframe#youtubeFrame`에 넣는 YouTube 영상 ID와 할당 구문을 저장 HTML에서 식별해 출처 보존 |
| `chinese / menu_4287` 중국학과 인스타 | 1 | 저장 응답은 Instagram 화면. 학교 HTML 선택자 보완으로 게시물 본문을 복구할 수 없음 |
| `microbiology / menu_518` 산업대학원 공지사항 | 1 | 저장 응답은 공과대학 `ERROR` 페이지. 실제 목적지·원본 오류 확인이 필요 |

위 73개는 **보완 코드가 다루는 구조의 수**이며, 재수집 성공 건수가 아닙니다.
이미지로만 구성된 교육과정·졸업요건 등은 `image_only`로 분류해 원본 이미지를 수집하고,
OCR 전까지 `partial_success`로 유지합니다. 가이드북은 언어별 링크, 동영상은 영상 URL을
보존하는 것이며 전자책 전체 내용이나 영상 자막을 추출한 것으로 간주하지 않습니다.
동영상 처리는 확인된 `geoinfo.pknu.ac.kr`의 요소 바인딩·리터럴 ID·주소 할당만 인식합니다.
JavaScript 실행이나 YouTube 추가 요청은 하지 않습니다.

텍스트·본문 이미지·교수 상세 링크 탐색은 같은 선택자를 사용합니다.
기존 `#sbCont`, 영어영문학부·`#stance`, 검증된 `.a_bdCont`·`.c_bdCont` 선택 우선순위는 유지합니다.
본문이 비어 있으면 메뉴나 푸터를 대신 수집해 성공으로 바꾸지 않습니다.
새 템플릿의 표·숨김 탭 본문·이미지 전용 상태·교수 링크 범위·가이드북 링크·영상 출처의
회귀 테스트를 기존 테스트 모듈에 추가했습니다. 이번 수정 후 테스트와 재크롤링은 실행하지 않았습니다.
앞 절의 515개 통과 기록은 이번 후속 보완 이전 결과입니다.

프로젝트 루트에서 사용자가 실행할 검증 명령입니다.

```powershell
# 변경한 두 파서의 회귀 테스트
.\.venv\Scripts\python.exe -X utf8 -m unittest scripts.tests.departments.test_numeric_cms scripts.tests.departments.test_php_adapters

# 공유 본문 처리 변경에 대한 전체 회귀 테스트
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s scripts/tests -t . -p "test_*.py"

# 대표 구조 소량 재수집: d 템플릿, b 템플릿 교수진, 본문 클래스 없는 교육과정
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset lifelong --section menu_4029 --once
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset ref --section menu_1757 --once --max-items 3
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset semiconductor --section menu_5514 --once
```

테스트와 대표 결과를 확인한 뒤, 같은 데이터셋의 기존 크롤러가 종료된 상태에서 다음 명령으로
기존 실패 목록 중 보완 대상 73개 section을 재수집할 수 있습니다.
교수진 section은 해당 목록의 상세 프로필도 수집하며, 이미지·첨부도 다운로드합니다.
상태 초기화는 필요하지 않습니다. 원본 오류·외부 화면 2개는 이 명령에서 제외합니다.

```powershell
$staticGapManifest = Get-Content -LiteralPath 'files/department/_runs/scope_retry_20261118_030524/manifest.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$staticGapTargets = $staticGapManifest.completion_audit.remaining_static_gaps | Where-Object {
    -not (($_.dataset -eq 'chinese' -and $_.section -eq 'menu_4287') -or
          ($_.dataset -eq 'microbiology' -and $_.section -eq 'menu_518'))
}
foreach ($staticGap in $staticGapTargets) {
    & .\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.engine --dataset $staticGap.dataset --section $staticGap.section --once
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "확인 필요: $($staticGap.dataset) / $($staticGap.section) (exit=$LASTEXITCODE)"
    }
}
```

재수집 후 각 section의 새 JSON·실행 보고서에서 본문·이미지 저장 여부와 `content_state`를 확인해야 합니다.
기존 manifest의 75개 목록은 과거 점검 기록이므로 코드 수정·재수집으로 자동 갱신되지 않습니다.
OCR·전처리·DB 반영은 별도 단계이며 이번 작업에서는 실행하지 않았습니다.

## 일부 학과의 본문 영역 선택

2026-10-28부터 다음 대상은 정적 안내 페이지에서도 `.a_bdCont`를 우선 선택합니다.
페이지 앞쪽에 있는 빈 `.container` 때문에 본문을 놓치지 않도록 선택 순서를 지정했습니다.

- `control`: 제어계측공학전공
- `fashion`: 패션디자인학과
- `materials`: 신소재시스템공학전공
- `college`: 학부대학
- `ref`: 냉동공조공학전공
- `fisheries`: 수산과학대학 자유전공학부
- `semiconductor`: 차세대반도체공학전공

이 대상들의 상세 게시글은 실제 저장 HTML에서 `.c_bdCont`를 사용합니다.
따라서 `.a_bdCont`를 먼저 찾고, 없으면 `.c_bdCont`에서 본문·첨부·이미지 상태를 추출합니다.
두 영역이 모두 없으면 정적 페이지는 기존 본문 탐색 방식으로 돌아갑니다.
이 우선순위와 대체 선택자는 위 7개 사이트의 설정된 `base_url` 호스트를 기준으로 적용합니다.
게시글 주소가 공유 CMS인 `icms.pknu.ac.kr`로 연결돼도 해당 학과의 선택 규칙을 유지합니다.
구현은 [numeric CMS 파서](../../scripts/crawlers/departments/adapters/numeric_cms.py)에 있습니다.

기존에 저장된 JSON과 실행 보고서는 코드 변경만으로 갱신되지 않습니다.
기존 HTML을 재파싱하거나 해당 대상을 다시 실행해야 누락 결과를 보완할 수 있습니다.

## 고정 공지가 반복되는 게시판의 수집

2026-10-27 확인된 대상은 수산과학대학(`fishsci`, `query_view_do`)과
해양생산시스템관리학부(`mpsm`, `query_view_legacy`)입니다.
이 사이트들은 마지막 일반 게시글 이후에도 고정 공지만 있는 목록을 HTTP 200으로 반환합니다.
수산과학대학 공지사항은 1,200페이지에서도 고정 공지 4개,
해양생산시스템관리학부 공지사항은 360페이지에서도 고정 공지 13개를 반환하는 것을 확인했습니다.

기존에는 목록의 `pageIndex`가 상세 URL에 붙어 같은 게시글을 매번 다른 URL로 판단했습니다.
이제 두 파서는 상세 URL에서 `pageIndex`를 제외하고 메뉴 번호·게시글 ID·상세 보기 설정을 유지합니다.
공통 게시판 엔진도 URL보다 `source_id`를 우선해 같은 실행에서 처리한 글을 구분합니다.
같은 고정 공지는 한 번만 처리하고, 새로운 ID가 함께 있는 목록은 계속 수집하며,
이미 처리한 ID만 반환되는 목록에 도달하면 해당 게시판을 종료하고 다음 section으로 이동합니다.

종료 근거는 학과 로그의 `pagination_stopped` 이벤트에 `section`, `page`,
`reason="no_new_post_ids"`, `duplicate_count`, `pinned_only`, `unique_posts`로 기록합니다.
이 기능은 기존 `engine`과 `parallel` 실행에 포함되며 별도 진입점이나 옵션이 필요하지 않습니다.
저장 파일·문서 ID·증분 상태는 유지합니다. 실행 중인 프로세스는 코드를 다시 불러오지 않으므로
Worker 3/4 터미널에서 Ctrl+C 후 정상 종료를 확인하고 같은 manifest와 worker 번호로 다시 실행해야 합니다.
완료된 학과는 건너뛰며, 중단된 학과는 목록을 처음부터 확인하지만 기존 문서·첨부파일은 재사용합니다.

## 수집 설정과 대상 관리

- `sites.csv`: 단과대학·학과·전공 주소와 데이터셋 별칭을 관리하는 카탈로그.
- `registry.json`: 검증된 메뉴·게시판 ID·파서·저장 경로·보류 사유를 관리하는 실행 설정.
- `engine.py --all`: `enabled`가 참이고 활성 section이 있는 데이터셋을 등록 순서대로 실행.
- `cli coverage`: 공유 홈페이지와 보류 항목까지 카탈로그의 모든 행을 대조.

새 홈페이지는 카탈로그와 레지스트리를 함께 갱신하고, 필요한 메뉴를 검증한 뒤 활성화합니다.
실패한 사이트를 대상에서 지우기보다 실행 결과와 보류 사유를 남겨 후속 확인이 가능하게 유지합니다.

## 저장 구조와 실시간 로그

저장 경로는 레지스트리의 `storage_path`를 따릅니다.
소속 수준이 필요 없는 경우 생략하며, 홈페이지를 공유하는 전공은 한 데이터셋으로 저장합니다.

```text
files/department/
  정보융합대학/컴퓨터·인공지능공학부/ce/
    state.json
    output/
      json/<카테고리>/<문서 ID>.json
      html/<카테고리>/<문서 ID>.html
      files/<카테고리>/<문서 ID>/<첨부파일>
      images/
      deleted/
      runs/<실행 ID>.json
  _runs/department_batch_report.json

logs/crawlers/
  ce.log
  ce.jsonl
  <데이터셋>.log
  <데이터셋>.jsonl
```

파일과 폴더는 필요한 경우 생성됩니다. 실행 중 터미널에 학과 진행 번호·요청 URL·저장·오류가 표시됩니다.
`.log`와 `.jsonl`은 같은 데이터셋 파일에 누적됩니다. 별도 터미널에서 로그를 확인하려면:

```powershell
Get-Content logs/crawlers/ce.log -Tail 20 -Wait
```

`크롤링 완료`나 `section_finished`만으로 성공을 판단하지 말고 최종 결과의 `status`와 오류를 확인하세요.
배치 보고서 확인 방법은 아래에 설명합니다.

## 실패한 첨부만 재수집

[retry_attachments.py](../../scripts/crawlers/departments/retry_attachments.py)는 성공한 첨부와
게시글 본문·증분 상태를 유지하면서 실패한 첨부만 처리합니다.
기본 대상은 TLS·일시적 요청·스트림·응답 형식 오류와 HTTP 429/일부 5xx입니다.
접근 거부·로그인 필요·404 등은 기본 재시도에서 제외합니다.

```powershell
# 대상 확인: 네트워크 요청이나 문서 변경 없음
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.retry_attachments --all --dry-run

# 일어일문학부의 일시적 요청 실패만 최대 10건 재수집
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.retry_attachments --dataset japanese --error-code REQUEST_FAILED --limit 10

# 여러 오류 유형을 함께 선택하고 별도 실행 폴더에 결과 저장
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.retry_attachments --all --error-code TLS_ERROR --error-code UNSUPPORTED_CONTENT_TYPE --error-code UNSUPPORTED_FILE_TYPE --limit 200 --output-root files/department/_runs/attachment_recovery
```

`--error-code`는 여러 번 지정할 수 있습니다. `--limit`는 데이터셋별 제한입니다.
`--document`로 해당 데이터셋에 속한 문서 JSON 하나만 지정할 수 있습니다.
이미 검토한 실패 로그 JSON에 `events` 배열과 `dataset`, `source_id`, `code`가 있으면
`--failure-report <JSON>`으로 해당 문서만 찾아 전체 저장 JSON 스캔을 생략할 수 있습니다.

변경 전 JSON은 실행 폴더의 `documents_before/<카테고리>/<문서 ID>.json`에 백업합니다.
결과는 `result.json`, 전체 실행은 `batch.json`에 기록합니다.
`--output-root`를 생략하면 해당 데이터셋의 `output/runs/attachment_retries/<실행 ID>/`에 저장합니다.
재수집 보고서는 `mode: attachment_retry`로 전체 크롤링 실행과 구분합니다.
기존 전체 실행 보고서의 통계는 역사적 기록으로 유지되므로, 재수집 후 현황은 현재 문서 JSON과
재수집 보고서를 기준으로 확인합니다.

일반 웹페이지·CMS 게시글을 첨부로 잘못 분류한 항목은 `reference_links`로 옮깁니다.
기존 첨부 메타데이터 전체를 `attachment_metadata`에 보존합니다.
구형 `cms.pknu.ac.kr` 게시글은 한 단계만 조회해 명시적 본문 영역에서 실제 첨부를 찾습니다.
안내 스크립트를 실행하거나 관련 게시글을 재귀적으로 따라가지 않습니다.
조회한 구형 CMS 응답 원문은 해당 데이터셋의 `output/html/attachment_references/`에
해시·크기와 함께 보존합니다.
실제 파일 링크를 찾지 못한 항목은 `no_file_links` 및 `REFERENCE_FILE_UNAVAILABLE`로 남기며
파일을 복구했다고 표시하지 않습니다.

서버가 `Content-Disposition: attachment`와 HTML 파일명을 명시한 정상 `.html`·`.htm` 첨부는
원본 바이트를 저장하고 `representation: html_attachment`, SHA-256, 파일 크기를 기록합니다.
로그인·접근 거부·파일 없음 응답은 정상 HTML 첨부와 구분해 실패로 유지합니다.
HTML 첨부에 포함된 외부 링크는 자동 다운로드하지 않습니다.

공간정보시스템공학전공의 `bbsExe.php?mode=down&idx=...` 다운로드 주소도 첨부로 인식합니다.
기존에 참조 링크로 보존된 항목에 새 다운로드 지원을 추가한 경우, 보존된 메타데이터를 이용해
다시 첨부 재시도 대상으로 처리합니다.

첨부의 연결 대기와 읽기 대기 제한은 `attachment_connect_timeout_seconds`(기본 10초),
`attachment_read_timeout_seconds`(기본 60초)로 별도 설정합니다.
일시적 오류는 최초 요청 포함 최대 3회 시도하고, DNS 이름 해석 오류는 별도로 분류합니다.

## 적용한 TLS 설정

등록된 103개 데이터셋의 홈페이지와 HTTP 리다이렉트를 확인했다.
인증서 검증을 유지하고 기본 TLS, 중간 인증서 보완, 암호군 호환+중간 인증서
순서로 비교하여 실제로 응답한 호스트만 정책 목록에 포함했다.

- 검증된 HTTPS 호스트 97개 중 기본 TLS로 응답한 2개는 기존 설정 유지.
- 나머지 95개는 [정확한 호스트 목록](../../scripts/crawlers/certificates/department-tls-hosts.json)에 등록.
- 93개 호스트: 기존 중간 인증서와 `DEFAULT@SECLEVEL=1` 적용.
- 2개 호스트: 기존 중간 인증서만 적용, 기본 암호군 유지.
- 공통 CMS `icms.pknu.ac.kr`도 별도 검증 후 명시적으로 포함.
- 인증서 체인·호스트명 검증 유지. 전역 `verify=False`, 와일드카드 호스트 허용 없음.
- 원래 HTTP로 운영되는 사이트의 주소는 변경하지 않음.
- 크롤러뿐 아니라 probe/discovery의 Requests 세션에도 같은 정책 적용.

정책은 사이트가 검증된 당시의 TLS 호환성 기록이다. 인증서 교체나 서버 설정 변경 시
재검증이 필요하며, 새 호스트를 자동으로 허용하지 않는다.

2026-10-28 첨부 복구를 위해 `cms.pknu.ac.kr`, `www.pknu.ac.kr`, `libweb.pknu.ac.kr`를
추가 검증했습니다. 세 호스트 모두 기본 암호군에서는 협상에 실패했고, 기존 중간 인증서와
`DEFAULT@SECLEVEL=1`에서 인증서 체인·호스트명 검증을 유지한 HTTPS 응답을 확인했습니다.
정확한 호스트 목록에 이 3개를 추가했으며, 위 2026-10-25의 호스트 수는 당시 기록입니다.

## 연결 검증과 접근 제한

2026-10-25, Python 3.12.10 / Windows x64에서 확인한 결과입니다.

변경된 공통 TLS 설정으로 103개 홈페이지와 각 활성 데이터셋의 대표 게시판·안내 페이지를
요청했다. **298건 중 일반 응답 296건, 접근 제한 2건, TLS 오류 0건**이었다.
대표 메뉴 검사이며 모든 메뉴·게시글·첨부파일을 재수집한 결과는 아니다.

접근 제한은 HTTP 200 여부와 별도로 URL·본문 증거를 확인했다.

| 대상 | 확인한 제한 | 처리 |
| --- | --- | --- |
| `humanict` | `/common/deny.jsp`로 이동, 접근 거부 제목·본문 | 등록 유지, `access_blocked`로 보류 |
| `chss`의 `menu_5024` 취창업정보 | `pknujob.pknu.ac.kr/main`로 이동 후 로그인 요구 | 학과 전체는 활성 유지. 해당 메뉴 접근 제한은 기존 런타임 검사로 기록 |

## 전체 대상 대조 결과

주소 카탈로그 105개 행을 전부 대조했다.

| 카탈로그 행 상태 | 행 수 | 의미 |
| --- | ---: | --- |
| `ready` | 102 | 101개 고유 데이터셋에 대응. 공유 홈페이지의 중복 행 포함 |
| `link_hub` | 1 | `dmfbe`: 전공 홈페이지로 안내하는 학부 허브 |
| `blocked` | 1 | `humanict`: 접근 거부로 현재 수집 불가 |
| `missing_homepage` | 1 | 사회계열: 카탈로그에 홈페이지 주소 없음 |
| `unregistered` / `unconfigured` | 0 | 설명되지 않은 홈페이지 누락 없음 |

`dmfbe`의 두 전공 `mbe`·`ree`는 모두 활성 수집 대상이다. 허브의 자체 본문 수집을
완료했다고 간주하지 않으며, 두 전공으로 연결되는 구조를 명시적으로 기록했다.
`humanict`와 주소 없는 사회계열은 수집 완료나 사용자 제외로 표시하지 않고 후속 확인 대상으로 남긴다.

`coverage` 명령은 홈페이지가 없거나 비활성인 항목도 출력한다. 신규 학과가 카탈로그에 추가되면
미등록 상태로 드러나며, 허브의 연결 대상 중 하나가 비활성화되어도 `unconfigured`로 나타난다.
카탈로그 자체에 없는 학과까지 자동으로 보장하는 것은 아니므로 카탈로그도 함께 관리해야 한다.

## 배치 보고서와 중단·실패 확인

`--all`은 실행 예정 데이터셋과 카탈로그의 보류 사유를 표시하고, 다음 배치 보고서를
학과 하나가 끝날 때마다 원자적으로 갱신한다.

```text
files/department/_runs/department_batch_report.json
```

- `coverage`: 카탈로그 전체 행과 상태
- `expected_datasets`: 실행 예정 데이터셋
- `results`: 시도한 학과별 실행 결과, 오류·통계·종료 코드
- `status`: 실행 중 `running`, 종료 후 `completed` 또는 `needs_attention`, 중단 시 `cancelled`

`expected_datasets`와 `results`의 데이터셋 목록을 대조하여 실행 중단·누락을 확인한다.
학과 하나에서 예상 밖 예외가 발생해도 실패를 기록한 뒤 다음 학과를 실행한다.
일반 실패는 종료 코드 1, 취소는 130이다. 카탈로그 보류나 부분 성공은 종료 코드와 별도로
배치 보고서의 `needs_attention` 및 각 결과를 확인해야 한다.
기존 증분 상태와 원본 파일을 초기화할 필요는 없다.

## 여러 터미널에서 병렬 실행

`parallel.py`는 순차 배치 보고서에서 이미 실행이 종료된 대상을 제외하고,
취소된 학과와 아직 시도하지 않은 학과를 여러 작업으로 나눕니다.
부분 성공·실패로 종료된 학과의 보완 재수집은 별도로 관리하며 자동 재시도 대상에 포함하지 않습니다.
기존 `engine --all` 실행은 먼저 Ctrl+C로 정상 중단하고 종료를 확인해야 합니다.
기존 순차 실행이 `running`인 동안에는 병렬 계획 생성을 거부합니다.

```powershell
# 새 실행마다 output-root에 새 폴더명을 지정
.\.venv\Scripts\python.exe -X utf8 -m scripts.crawlers.departments.parallel --prepare --source-report files/department/_runs/department_batch_report.json --workers 4 --output-root files/department/_runs/parallel_RUN

# 서로 다른 터미널에서 worker 번호를 1, 2, 3, 4로 지정해 각각 실행
.\.venv\Scripts\python.exe -u -X utf8 -m scripts.crawlers.departments.parallel --manifest files/department/_runs/parallel_RUN/manifest.json --worker 1

# 전체 병렬 작업 현황
.\.venv\Scripts\python.exe -X utf8 -m scripts.crawlers.departments.parallel --manifest files/department/_runs/parallel_RUN/manifest.json --status
```

`--host-evidence`는 앞서 검증한 리다이렉트 기록이 있을 때 지정합니다. 기록이 없으면
이 옵션을 생략하며 레지스트리의 기본 호스트·추가 TLS 호스트를 기준으로 묶습니다.
같은 호스트와 공통 CMS를 사용하는 대상은 같은 작업에 배정합니다.
각 작업은 학과별로 독립 Python 프로세스를 실행하므로 크롤러의 전역 설정을 공유하지 않습니다.
데이터셋·호스트별 파일 잠금을 사용해 겹치는 병렬 작업의 실행을 거부합니다.
별도의 일반 `engine --all`/`--dataset` 명령을 같은 대상에 동시에 실행하지 마세요.

계획은 `manifest.json`, 작업 진행 상황은 같은 폴더의 `worker_1.json`~`worker_4.json`에 저장됩니다.
기존 순차 보고서는 중단 당시의 기록으로 유지하며, 병렬 전환 후 현황은 `--status`와 작업별 보고서로 확인합니다.
중단된 작업은 동일 manifest와 worker 번호로 재실행하면 종료된 항목을 건너뛰고 이어 실행합니다.
진행 중이던 학과는 같은 증분 상태·저장 파일을 활용해 다시 실행하며 상태 초기화를 하지 않습니다.

2026-10-27 실제 실행은 `files/department/_runs/parallel_20261027_0100/manifest.json`을 사용합니다.
전환 당시 43개 데이터셋이 종료됐고, 나머지 58개를 네 작업(17·14·14·13개)에 배정했습니다.
각 작업을 실행하는 PowerShell 창은 `Campus RAG - Worker 1`~`4` 제목으로 열었습니다.

## 검증 기록

- 전체 자동 테스트: **374 passed**, 기존 사용 중단 경고 6개

위 검증은 연결·범위 점검이며 전체 게시글 수집, 첨부 다운로드, DB 적재는 실행하지 않았습니다.
`logs/`에는 실제 실행 로그만 유지합니다. 별도 요청 없이 임시 보고서·진단 표본·테스트 결과 파일을
생성하지 않으며, 점검 결과는 응답이나 터미널에서 확인합니다.
