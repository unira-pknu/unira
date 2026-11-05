# 전체 크롤러 실행 가이드

메인 홈페이지, 연구기관, 단과대학·학부·학과·전공 홈페이지, 학칙·규정 크롤러의 실행 방법입니다.
모든 명령은 **프로젝트 루트**에서 PowerShell로 실행합니다. 등록된 범위만 수집하며,
주소 목록에 있는 모든 외부 홈페이지를 자동으로 탐색하지 않습니다.

## 1. 실행 준비

```powershell
cd D:\Github\campus-rag\campus-rag

# 최초 한 번만 실행: 기존 .venv가 있으면 생성 단계 생략
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
python -m pip check
```

가상환경 활성화가 실행 정책으로 차단될 때만 다음 명령을 실행하고 다시 활성화합니다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

`requirements-runtime.txt`는 API 서버용이므로 크롤링에는 전체 `requirements.txt`를 사용합니다.
활성화 없이 실행하려면 아래 명령의 `python`을 `.\.venv\Scripts\python.exe`로 바꾸세요.
크롤링은 로컬 파일 수집 단계이며 Supabase 적재·임베딩·API 서버 실행과는 별개입니다.

## 2. 전체 크롤러 실행 명령

아래 세 명령으로 서로 다른 수집 범위를 한 번씩 실행합니다.

```powershell
# 메인 홈페이지: 활성 등록 페이지 전체
python -m scripts.crawlers.main.run --all

# 단과대학·학부·학과·전공: 활성 상태이며 수집 section이 있는 데이터셋 전체
python -m scripts.crawlers.departments.engine --all --once

# 별도 학칙·규정 사이트: 학칙/규정과 지침/내규 모두
python -m scripts.crawlers.pknu_rule --laws --bylaws
```

메인 통합 실행에는 공지사항, 대학생활 가이드·E-하나로, 등록된 연구기관 페이지도 포함됩니다.
이후 소개하는 개별 명령은 해당 범위만 다시 실행할 때 사용합니다.
세 범위를 한 번에 묶은 별도 통합 CLI는 없으므로 위 명령을 순서대로 실행하세요.
각 명령의 종료 상태와 보고서를 확인한 후 다음 범위로 넘어가는 것을 권장합니다.

**전체 대상 선택과 전체 이력 재수집은 다릅니다.** 메인의 일반 게시판은 기본 목록 1페이지,
공지사항은 기본 최근 3페이지를 확인합니다. 기존 결과는 각 수집기의 증분·캐시 정책에 따라 재사용합니다.
게시판 형태의 메인 페이지들만 골라 전체 목록을 수집하려면 다음 명령을 사용합니다.

```powershell
# 대상 페이지 ID와 전체 목록 수집 설정 확인
python -m scripts.crawlers.main.run --boards --all-board-pages --dry-run

# 활성 게시판·공지사항·등록금 FAQ 전체 목록과 게시글 수집
python -m scripts.crawlers.main.run --boards --all-board-pages --report files/pknu_main/_runs/main_boards_run_report.json
```

`--boards`는 활성 등록된 일반 게시판·공지사항과 FAQ를 포함한 등록금 안내 페이지만 선택합니다.
`--all-board-pages`는 일반 게시판의 1페이지 제한과 공지사항의 3페이지 제한을 해제합니다.
등록금 FAQ는 기존처럼 전체 목록을 수집합니다. 등록금 안내 본문도 함께 갱신됩니다.
기존 성공 결과와 첨부파일은 재사용하며, 강제 갱신이 필요할 때만 `--full-resync`를 추가합니다.
비활성 게시판과 다른 안내 페이지는 이 명령의 대상에 포함되지 않습니다.

학과의 `--all`은 전체 사이트 선택이며 `--recent-only`를 생략하면 최근 목록 제한 없이 동작합니다.
학과 엔진은 현재 한 번 실행하고 종료하며 `--once`는 의도를 명시하는 호환 옵션입니다.

## 3. 수집 대상 확인과 제한 실행

### 메인 홈페이지

```powershell
# 목록 및 실행 계획 확인: 실제 수집하지 않음
python -m scripts.crawlers.main.run --list
python -m scripts.crawlers.main.run --all --dry-run
python -m scripts.crawlers.main.run --page-ids 31 92 95 --dry-run

# 개별 페이지 / 여러 페이지
python -m scripts.crawlers.main.run --page-ids 481
python -m scripts.crawlers.main.run --page-ids 224 225

# 일반 게시판 목록 1페이지 / 학사일정 지정 연월
python -m scripts.crawlers.main.run --page-ids 32 --board-pages 1
python -m scripts.crawlers.main.run --page-ids 31 --year 2026 --months 9 10

# 기존 결과 재사용을 해제하는 개별 재수집
python -m scripts.crawlers.main.run --page-ids 481 --full-resync
```

`--page-ids`, `--category`, `--all`, `--boards`, `--list`는 하나만 선택합니다.
`--board-pages N`은 게시글 N개가 아닌 **목록 N페이지**이며 공지사항과 등록금 FAQ에는 적용되지 않습니다.
`--all-board-pages`와 `--board-pages N`은 동시에 사용할 수 없습니다.
`--all --full-resync`는 공지사항 전체 이력도 포함하는 큰 재수집이므로 필요할 때만 사용하세요.

| 메인 카테고리 | 명령 |
| --- | --- |
| 대학/대학원 | `python -m scripts.crawlers.main.run --category "대학/대학원"` |
| 연구/산학 | `python -m scripts.crawlers.main.run --category "연구/산학"` |
| 대학생활 | `python -m scripts.crawlers.main.run --category "대학생활"` |
| 입학/취업 | `python -m scripts.crawlers.main.run --category "입학/취업"` |
| 커뮤니티 | `python -m scripts.crawlers.main.run --category "커뮤니티"` |
| 대학소개 | `python -m scripts.crawlers.main.run --category "대학소개"` |

현재 커뮤니티는 공지사항만 활성화되어 있습니다. 사용자 제외 페이지, 대학소개/홍보관,
개교 80주년 등 비활성 등록은 전체 실행에도 포함되지 않습니다.
카테고리에는 저장 폴더명 `연구_산학` 대신 메뉴명 `연구/산학`을 사용합니다.
세부 옵션과 수집 정책은 [메인 홈페이지 README](MAIN.md)를 참고하세요.

### 학과·단과대학 홈페이지

```powershell
# 설정 검증 / 현재 실행 가능 데이터셋 확인
python -m scripts.crawlers.departments.cli validate
python -m scripts.crawlers.departments.cli list-ready
python -m scripts.crawlers.departments.cli list-ready --json

# 주소 카탈로그의 모든 행 대조: 미등록·보류·홈페이지 없음도 표시
python -m scripts.crawlers.departments.cli coverage

# 컴퓨터·인공지능공학부 단일 실행
python -m scripts.crawlers.departments.engine --dataset ce --once

# 모든 학과의 게시판별 최근 목록 1페이지 확인 (정적 페이지도 수집)
python -m scripts.crawlers.departments.engine --all --once --recent-only 1

# 소량 확인: 데이터셋별 첫 게시판 1개에서 최대 문서 2개, 첨부 다운로드 생략
python -m scripts.crawlers.departments.engine --dataset ce --once --recent-only 1 --max-board-sections 1 --max-items 2 --no-download-files
```

단일 홈페이지를 바꾸려면 `--dataset ce`의 `ce`를 아래 표의 데이터셋으로 바꿉니다.
특정 메뉴만 실행하려면 레지스트리에 있는 활성 section ID를 확인하여 지정합니다.

```powershell
# ce의 활성 section ID와 이름 확인
$registry = Get-Content scripts/crawlers/departments/registry.json -Raw -Encoding UTF8 | ConvertFrom-Json
($registry.departments | Where-Object dataset -eq 'ce').sections |
    Where-Object { $_.enabled -ne $false } | Select-Object id,name,kind

# 위 출력에서 선택한 실제 ID를 입력
$sectionId = Read-Host '수집할 활성 section ID'
python -m scripts.crawlers.departments.engine --dataset ce --once --section $sectionId
```

| 옵션 | 의미 |
| --- | --- |
| `--all` / `--dataset NAME` | 전체 활성 데이터셋 / 단일 데이터셋 선택 |
| `--recent-only N` | 게시판별 최근 목록 N페이지로 제한 |
| `--section ID` | 단일 활성 메뉴 선택. `--all`과 함께 사용 불가 |
| `--max-board-sections N` | 앞쪽 게시판 N개만 선택하고 정적 페이지 생략 |
| `--max-items N` | 데이터셋별 처리 문서 수 제한 |
| `--no-download-files` | 첨부 메타데이터만 보존하고 다운로드 생략 |
| `--reset-state` | 선택 데이터셋의 증분 상태 초기화 후 수집. 일반 실행에는 불필요 |

`--section`과 `--max-board-sections`는 함께 사용할 수 없습니다.
실행 범위의 기준은 [registry.json](../../scripts/crawlers/departments/registry.json)의 활성 데이터셋·section입니다.
[sites.csv](../../scripts/crawlers/departments/sites.csv)는 주소 카탈로그이므로 그 자체가 전체 실행 대상은 아닙니다.
`list-ready`는 로컬 설정 기준 상태이며 실제 원격 접속 성공을 보장하지 않습니다.

`coverage`는 카탈로그의 학과·전공을 한 행도 생략하지 않고 표시합니다.
현재 `dmfbe`는 두 전공 홈페이지 `mbe`·`ree`로 안내하는 허브여서 별도 게시판 수집은 하지 않고,
두 전공은 `--all`에 포함합니다. `humanict`는 접근 거부가 확인되어 보류 상태로 유지합니다.
주소가 없는 사회계열 항목도 `missing_homepage`로 표시합니다. 접근 거부·주소 없음·미등록·미설정이 있으면
`coverage` 종료 코드는 1이며, 등록 데이터가 사라졌다는 뜻으로 단정하지 말고 각 사유를 확인하세요.

전체 학과 실행 시 대상과 보류 사유를 먼저 표시하고, 기본 배치 보고서
`files/department/_runs/department_batch_report.json`에 카탈로그 전체·실행 예정 데이터셋·각 실행 결과를 기록합니다.
보고서는 학과마다 갱신되며 예외가 난 학과도 실패로 남기고 다음 학과를 계속 실행합니다.
보고서의 `expected_datasets`와 `results`를 대조하면 실행 누락이나 중단된 위치를 확인할 수 있습니다.
학과별 부분 성공·카탈로그 보류가 있으면 배치 상태는 `needs_attention`입니다.
`--batch-report PATH`로 보고서를 분리할 수 있습니다. 실제 수집 실패의 종료 코드는 기존처럼 1이며,
접근 보류나 부분 성공은 보고서를 별도로 확인해야 합니다.

검증한 Python 3.12 환경에서 전체 실행:

```powershell
.\.venv\Scripts\python.exe -u -m scripts.crawlers.departments.engine --all --once
```

학과 크롤러의 실행·저장 구조·TLS 설정·전체 대상 및 보류 사유는
[학과·단과대학 크롤러 README](DEPARTMENTS.md)를 참고하세요.

## 4. 별도·개별 크롤러

### 메인 공지사항

```powershell
# 기본 증분 수집: 분류별 최근 목록 3페이지
python -m scripts.crawlers.pknu_notice

# 분류별 최근 목록 1페이지 / 특정 분류만
python -m scripts.crawlers.pknu_notice --recent-only 1
python -m scripts.crawlers.pknu_notice --only-cd 10001 --recent-only 1

# 전체 목록 재수집
python -m scripts.crawlers.pknu_notice --full-resync
```

분류 코드는 [pknu_notice.py](../../scripts/crawlers/pknu_notice.py)의 `CD_TO_LABEL`을 참고하세요.

### 연구기관

```powershell
python -m scripts.crawlers.pknu_research_institutes --list
python -m scripts.crawlers.pknu_research_institutes --all --dry-run
python -m scripts.crawlers.pknu_research_institutes --all
python -m scripts.crawlers.pknu_research_institutes --page-ids 81
```

메인 `연구/산학/연구기관`의 활성 등록 페이지만 수집합니다. 기관 외부 홈페이지 전체를 탐색하지 않습니다.

### 대학생활 가이드·E-하나로

```powershell
python -m scripts.crawlers.pknu_student_life --mode all
python -m scripts.crawlers.pknu_student_life --mode guide --limit 1
python -m scripts.crawlers.pknu_student_life --mode ebook
```

이 호환 CLI의 `all`은 대학생활 전용 범위입니다. 메인 전체 수집은 `scripts.crawlers.main.run --all`을 사용합니다.
`--limit 1`은 가이드 PDF 1개이며 E-하나로 페이지 수를 제한하지 않습니다.

### 학칙·규정·지침·내규

```powershell
# 두 종류 모두 (옵션 없이 실행해도 동일)
python -m scripts.crawlers.pknu_rule --laws --bylaws

# 종류별 실행
python -m scripts.crawlers.pknu_rule --laws
python -m scripts.crawlers.pknu_rule --bylaws

# 제한 수집 / 첨부 다운로드 생략
python -m scripts.crawlers.pknu_rule --laws --max-law-items 1 --no-download-files
python -m scripts.crawlers.pknu_rule --bylaws --max-bylaw-pages 1 --no-download-files
```

`--max-law-items`는 규정 트리 항목 제한이며 별도 추가 규정 검색은 여전히 실행됩니다.
`--max-bylaw-pages`는 목록 페이지 제한입니다. 첨부파일은 기본적으로 다운로드합니다.

규정 크롤러는 부경대·국가법령정보센터·첨부파일 요청 모두 HTTPS 인증서를 검증합니다.
부경대 호스트에만 TLS 호환 설정과 [누락된 중간 인증서](TLS_CERTIFICATES.md)를 적용합니다.
프록시·리다이렉트에도 검증이 적용되며, 사용자 CA는 Requests의 `REQUESTS_CA_BUNDLE` 설정을 사용할 수 있습니다.
일시적 연결 오류·응답 대기 시간 초과와 HTTP 429/500/502/503/504는 최대 3회 재시도합니다.
재시도는 `retry_scheduled`, 최종 실패는 `request_failed`로 로그에 남습니다.
403·404·인증서 검증 실패는 검증을 생략해 우회하지 않으며, 접근 거부 파일은 기존 보류 정책을 따릅니다.

## 5. 결과와 재실행

### 실시간 진행 로그

기존 실행 명령 그대로 터미널에 진행 로그가 즉시 표시됩니다.
`request_started`는 요청 전 URL·HTTP 메서드를 표시하므로 응답을 기다리는 중인 대상을 확인할 수 있습니다.
메인 통합 실행은 카테고리 순서·페이지 ID·완료 상태를, 학과 전체 실행은 현재 학과 번호/전체 개수를 표시합니다.
실행이 끝날 때까지 기다리지 않고 진행 상황을 확인할 수 있으며, `request_started` 자체는 요청 성공을 뜻하지 않습니다.

메인 통합 진행 로그는 `logs/crawlers/pknu_main.log`와 `.jsonl`,
각 수집기 로그는 `logs/crawlers/<데이터셋>.log`와 `.jsonl`에 누적됩니다.
학과 크롤러의 `url_skipped`는 건너뛴 `url`, 원본 페이지 `base_url`, 사유 `reason`,
판단 단계 `stage`를 해당 학과 로그에 기록합니다. 이메일·페이지 내부 이동처럼 HTTP 수집 대상이 아닌 링크는
`INFO`(`NON_FETCHABLE_URL`), 잘못된 호스트명 등 유효하지 않은 주소는 `WARNING`(`INVALID_URL`)으로 구분합니다.
메인 공통 수집기의 상세 요청은 `pknu_student_life` 로그를 공유합니다.
메인 통합 실행은 카테고리 처리가 끝날 때 각 페이지를 `[페이지 결과]` 태그로 한 줄씩 요약합니다.
페이지 번호·분류 이름 뒤에 상태를 표시하며, 실패·검토 사유와 첨부 실패 개수도 함께 남깁니다.

```text
[페이지 결과] main/71 대학_대학원 > 대학원 > 경영대학원: 성공 (success)
[페이지 결과] main/102 대학생활 > 학사정보 > 등록금_안내: 부분 성공 (partial_success) — INSTALLMENT_GROUPS_NEED_REVIEW
[페이지 결과] main/163 커뮤니티 > 공지사항: 검토 필요 (completed) — 첨부 실패 3건
```

괄호 안은 수집기가 반환한 원래 상태입니다. `completed`여도 첨부 실패나 경고가 있으면 `검토 필요`로 표시합니다.
수집 결과가 명시적으로 `fallback_used` 또는 fallback 경고를 보고하면 `FALLBACK`으로 표시합니다.
JSONL에서는 `event="page_result"`와 `page_id`, `title`, `status`, `status_label`, `reason`, `fallback_used`로 검색할 수 있습니다.
실행 후 페이지 결과만 확인하거나 문제 항목만 찾으려면 다음을 사용하세요.

```powershell
Select-String -Path logs/crawlers/pknu_main.log -Encoding utf8 -Pattern '\[페이지 결과\]'
Select-String -Path logs/crawlers/pknu_main.log -Encoding utf8 -Pattern '\[페이지 결과\].*(검토 필요|부분 성공|실패|FALLBACK)'
```

다른 터미널에서 파일을 실시간 확인하려면 다음을 실행합니다.

```powershell
Get-Content logs/crawlers/pknu_main.log -Tail 20 -Wait
# 특정 학과 로그 확인 (해당 로그 파일 생성 후)
Get-Content logs/crawlers/ce.log -Tail 20 -Wait
```

출력을 다른 프로그램으로 전달할 때 Python 출력 버퍼링도 해제하려면
`python -u -m scripts.crawlers.main.run --all`처럼 `-u`를 사용할 수 있습니다.

| 수집 범위 | 결과 위치 |
| --- | --- |
| 메인 홈페이지·공지사항·대학생활·연구기관 | `files/pknu_main/<카테고리>/` |
| 메인 통합 보고서 | `files/pknu_main/_runs/main_run_report.json` |
| 연구기관 전용 보고서 | `files/pknu_main/_runs/research_institutes_run_report.json` |
| 학과·단과대학 | `files/department/<storage_path>/output/` |
| 학과 증분 상태 | `files/department/<storage_path>/state.json` |
| 학칙·규정 | `files/rule/output/` |

학과의 `<storage_path>`는 레지스트리에 지정된 단과대학/학부·학과/전공/데이터셋 경로입니다.
실행 로그에 표시된 run-result JSON에서 `status`, 오류와 첨부 다운로드 실패를 확인하세요.
`partial_success`는 전체 수집 완료를 의미하지 않습니다. 메인 보고서는 같은 경로로 재실행하면 갱신됩니다.
중단 후에는 먼저 같은 명령을 다시 실행하여 기존 상태를 활용하세요. 상태 초기화는 필요한 경우에만 사용합니다.

메인 본문 이미지 OCR은 현재 정책상 비활성입니다. 로그인·접근 거부 파일은 보류 상태를 확인하세요.
크롤러 실행은 인벤토리 CSV 동기화나 Supabase 적재를 수행하지 않습니다.

### HWP HTML 변환 대기

HWP는 표 구조를 활용하는 HTML 변환이 완료될 때까지 시간 제한 없이 기다립니다.
대학통계처럼 큰 문서는 변환에 수 분 이상 걸릴 수 있으며, 변환기가 멈춘 경우에도 자동 종료하지 않습니다.
기존의 `falling back to hwp5txt`와 `falling back to paragraph records` 경로는 사용하지 않습니다.
변환 실패·변환기 누락·잘못된 HTML은 추출 오류로 처리하며 문단 텍스트로 대체하지 않습니다.
현재 Python에 설치된 pyhwp를 직접 실행합니다.
코드를 수정하기 전에 시작한 크롤러에는 변경이 적용되지 않으므로 중단 후 같은 명령으로 다시 실행하세요.

## 6. 학과·단과대학 데이터셋 목록

아래 목록은 문서 작성 시점(2026-10-25)의 실행 가능한 등록 항목입니다.
등록 변경 후 최신 목록은 `python -m scripts.crawlers.departments.cli list-ready`로 확인하세요.
각 행은 `python -m scripts.crawlers.departments.engine --dataset <데이터셋> --once`로 실행합니다.

| 단과대학 / 소속 | 홈페이지 이름 | 데이터셋 |
| --- | --- | --- |
| 인문사회과학대학 | 인문사회과학대학 | `chss` |
| 인문사회과학대학 | 국어국문학과 | `kukmun` |
| 인문사회과학대학 | 영어영문학부 | `english` |
| 인문사회과학대학 | 일어일문학부 | `japanese` |
| 인문사회과학대학 | 사학과 | `history` |
| 인문사회과학대학 | 경제학과 | `econ` |
| 인문사회과학대학 | 법학과 | `pknulaw` |
| 인문사회과학대학 | 행정복지학부 | `pknupa` |
| 인문사회과학대학 | 행정복지학부 | `ps1` |
| 인문사회과학대학 | 국제지역학부 | `dias` |
| 인문사회과학대학 | 중국학과 | `chinese` |
| 인문사회과학대학 | 정치외교학과 | `politics` |
| 인문사회과학대학 | 유아교육과 | `education` |
| 인문사회과학대학 | 패션디자인학과 | `fashion` |
| 인문사회과학대학 | 인문사회과학대학 자유전공학부 | `hssliberal` |
| 자연과학대학 | 자연과학대학 | `cns` |
| 자연과학대학 | 응용수학과 | `math` |
| 자연과학대학 | 물리학과 | `physics` |
| 자연과학대학 | 화학과 | `chem` |
| 자연과학대학 | 미생물학과 | `microbiology` |
| 자연과학대학 | 간호학과 | `nursing` |
| 자연과학대학 | 과학컴퓨팅학과 | `scicomp` |
| 자연과학대학 | 자연과학대학 자유전공학부 | `nsum` |
| 경영대학 | 경영대학 | `cba` |
| 경영대학 | 경영학부 | `dba` |
| 경영대학 | 국제통상학부 | `pknudic` |
| 경영대학 | 경영대학 자유전공학부 | `cbaliberal` |
| 공과대학 | 공과대학 | `coe` |
| 공과대학 | 전기공학부 | `electric-eng` |
| 공과대학 | 전기공학부 | `control` |
| 공과대학 | 전기공학부 | `semicon` |
| 공과대학 | 기계공학부 | `me` |
| 공과대학 | 기계공학부 | `mae` |
| 공과대학 | 에너지수송시스템공학부 | `ref` |
| 공과대학 | 에너지수송시스템공학부 | `mse` |
| 공과대학 | 에너지수송시스템공학부 | `name` |
| 공과대학 | 화학공학과 | `chemeng` |
| 공과대학 | 고분자화학소재공학부 | `induseng` |
| 공과대학 | 고분자화학소재공학부 | `polymer` |
| 공과대학 | 나노융합반도체공학부 | `nano` |
| 공과대학 | 나노융합반도체공학부 | `semiconductor` |
| 공과대학 | 시스템경영안전공학부 | `sme` |
| 공과대학 | 시스템경영안전공학부 | `safety` |
| 공과대학 | 소방공학과 | `fire` |
| 공과대학 | 융합소재공학부 | `metal` |
| 공과대학 | 융합소재공학부 | `materials-engineering` |
| 공과대학 | 융합소재공학부 | `materials` |
| 공과대학 | 건축공학과 | `archieng` |
| 공과대학 | 지속가능공학부 | `civil` |
| 공과대학 | 지속가능공학부 | `ecoeng` |
| 공과대학 | 공과대학 자유전공학부 | `engopen` |
| 수산과학대학 | 수산과학대학 | `fishsci` |
| 수산과학대학 | 식품과학부 | `food` |
| 수산과학대학 | 식품과학부 | `nulife` |
| 수산과학대학 | 생물공학과 | `biotech` |
| 수산과학대학 | 해양생산시스템관리학부 | `mpsm` |
| 수산과학대학 | 수산생명과학부 | `aqua-culture` |
| 수산과학대학 | 수산생명과학부 | `marinebio` |
| 수산과학대학 | 수산생명의학과 | `aquamed` |
| 수산과학대학 | 수해양산업교육과 | `fedu` |
| 수산과학대학 | 해양수산경영경제학부 | `mbe` |
| 수산과학대학 | 해양수산경영경제학부 | `ree` |
| 수산과학대학 | 수산과학대학 자유전공학부 | `fisheries` |
| 환경·해양대학 | 환경·해양대학 | `enma` |
| 환경·해양대학 | 지구환경시스템과학부 | `env-eng` |
| 환경·해양대학 | 지구환경시스템과학부 | `pkuocean` |
| 환경·해양대학 | 지구환경시스템과학부 | `earth` |
| 환경·해양대학 | 지구환경시스템과학부 | `envatm` |
| 환경·해양대학 | 지구환경시스템과학부 | `geoinfo` |
| 환경·해양대학 | 해양공학과 | `oceaneng` |
| 환경·해양대학 | 에너지자원공학과 | `ere` |
| 환경·해양대학 | 환경·해양대학 자유전공학부 | `lem` |
| 정보융합대학 | 정보융합대학 | `itc` |
| 정보융합대학 | 데이터정보과학부 | `bigdata` |
| 정보융합대학 | 데이터정보과학부 | `stat` |
| 정보융합대학 | 미디어커뮤니케이션학부 | `masscom` |
| 정보융합대학 | 스마트헬스케어학부 | `bme` |
| 정보융합대학 | 스마트헬스케어학부 | `marinesports` |
| 정보융합대학 | 스마트헬스케어학부 | `humanbio` |
| 정보융합대학 | 전자정보통신공학부 | `ee` |
| 정보융합대학 | 전자정보통신공학부 | `ice` |
| 정보융합대학 | 조형학부 | `pknuarchi` |
| 정보융합대학 | 조형학부 | `industrial` |
| 정보융합대학 | 조형학부 | `visual` |
| 정보융합대학 | 컴퓨터·인공지능공학부 | `ce` |
| 정보융합대학 | 디지털금융학과 | `digitalfinance` |
| 정보융합대학 | 스마트모빌리티공학과 | `smartmobility` |
| 정보융합대학 | 정보융합대학 자유전공학부 | `liberalitc` |
| 미래융합학부 | 미래융합학부 | `future` |
| 미래융합학부 | 평생교육·상담학전공 | `lifelong` |
| 미래융합학부 | 경찰범죄심리학전공 | `safe` |
| 미래융합학부 | 사회복지서비스학전공 | `welfare` |
| 미래융합학부 | 기계조선에너지시스템공학전공 | `msce` |
| 미래융합학부 | 전기전자SW공학전공 | `eese` |
| 글로벌자율전공학부 | 글로벌자율전공학부 | `duem` |
| 학부대학 | 학부대학 | `college` |
| AX혁신대학 | AX혁신대학 | `axic` |
| AX혁신대학 | AX지속가능융합학과 | `axcs` |
| AX혁신대학 | AI융합시스템공학과 | `aics` |
| AX혁신대학 | AXIC자유전공학부 | `axliberal` |
| 글로벌혁신대학 | 글로벌혁신대학 | `gic` |
