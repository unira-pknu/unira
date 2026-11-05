# Crawler Commands

## Crawler document schema migration

Inspect legacy-to-1.0 conversion without writing files:

```powershell
python scripts\migrations\crawler_documents.py --dataset ce
python scripts\migrations\crawler_documents.py --dataset rule
```

Apply after reviewing the report:

```powershell
python scripts\migrations\crawler_documents.py --dataset ce --apply
```

Applied conversion keeps `<document>.json.legacy.bak`, refuses to overwrite an
existing backup, and verifies document content, attachment count, and the new
content hash before replacement.

프로젝트 루트(`D:\Git\campus-rag`)에서 실행하는 것을 기준으로 정리합니다.
PowerShell에서는 가급적 프로젝트 가상환경의 Python을 직접 사용합니다.

```powershell
.\.venv\Scripts\python.exe --version
```

로그를 바로 보고 싶으면 `-u` 옵션을 붙입니다.

```powershell
.\.venv\Scripts\python.exe -u scripts\crawlers\pknu_rule.py --laws --bylaws
```

실시간 로그 확인 예시:

```powershell
Get-Content logs\rule_crawler.log -Wait -Tail 30
Get-Content logs\rule_preprocessing.log -Wait -Tail 30
Get-Content logs\main_notice_crawler.log -Wait -Tail 30
Get-Content logs\main_student_life_crawler.log -Wait -Tail 30
```

## Main Website

### `scripts/crawlers/pknu_notice.py`

부경대학교 메인 홈페이지 공지사항(`/main/163`)을 크롤링합니다.

출력:

- `files/pknu_notice/output/json/<category>/<slug>.json`
- `files/pknu_notice/output/html/<category>/<slug>.html`
- `files/pknu_notice/output/files/<category>/<slug>/<attachment>`
- `files/pknu_notice/output/deleted/<category>/<slug>.json`

첨부파일은 `output/files` 아래에 저장되며, JSON `attachments`에는 `saved_path` 등 로컬 저장 메타데이터가 함께 기록됩니다.

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\pknu_notice.py
.\.venv\Scripts\python.exe scripts\crawlers\pknu_notice.py --recent-only 5
```

옵션:

- `--full-resync`: 전체 페이지를 다시 수집합니다.
- `--recent-only N`: 카테고리별 최근 N페이지만 목록 수집합니다.
- `--reset-state`: `state_pknu_notice.json`을 삭제하고 다시 시작합니다.
- `--once`: 1회 실행합니다. 현재 기본 동작과 같습니다.
- `--only-cd CODE`: 특정 공지 카테고리 코드만 수집합니다. 예: `10001`

### `scripts/crawlers/pknu_student_life.py`

부경대학교 대학생활 가이드(`/main/434`)와 E-하나로 eBook을 수집합니다. E-하나로는 `output/json/학사정보/`에 저장하며, 원본 PDF가 없으면 뷰어의 페이지별 `basic-html` 본문을 수집합니다. 각 `pages[]` 항목의 `content`에서 정리된 쪽별 텍스트를 확인할 수 있으며, 전체 `content`는 이를 쪽 번호와 함께 이어 붙인 값입니다. 텍스트를 읽을 수 없는 쪽은 `text_status`와 `crawl.warnings`에 표시합니다.

출력:

- `files/pknu_student_life/output/json/<subcategory>/<slug>.json`
- `files/pknu_student_life/output/files/<subcategory>/<slug>/*.pdf`
- `files/pknu_student_life/output/deleted/`

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\pknu_student_life.py
.\.venv\Scripts\python.exe scripts\crawlers\pknu_student_life.py --mode guide --limit 3
.\.venv\Scripts\python.exe scripts\crawlers\pknu_student_life.py --mode ebook
```

옵션:

- `--mode guide|ebook|all`: `guide`는 `/main/434`, `ebook`은 E-하나로, `all`은 둘 다 수집합니다. 기본값은 `all`입니다.
- `--full-resync`: `content_hash` 비교를 무시하고 재수집합니다.
- `--reset-state`: `state_pknu_student_life.json`을 삭제합니다.
- `--limit N`: `guide` 모드에서 처리할 PDF 개수를 제한합니다. smoke test용입니다.

### `scripts/crawlers/main/run.py`

검증된 메인 홈페이지 페이지 ID와 수집 경로는 `scripts/crawlers/main/routes.py`에서 관리합니다.
미등록 ID는 `needs_review`로 표시하고 자동 크롤링하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.crawlers.main.run --list
.\.venv\Scripts\python.exe -m scripts.crawlers.main.run --page-ids 31 92 95 --dry-run
.\.venv\Scripts\python.exe -m scripts.crawlers.main.run --page-ids 31 --year 2026
.\.venv\Scripts\python.exe -m scripts.crawlers.main.run --page-ids 92 230 231 232
.\.venv\Scripts\python.exe -m scripts.crawlers.main.run --page-ids 233 234 235
.\.venv\Scripts\python.exe -m scripts.crawlers.main.run --page-ids 94
.\.venv\Scripts\python.exe -m scripts.crawlers.main.run --page-ids 101 104 247
.\.venv\Scripts\python.exe -m scripts.crawlers.main.run --page-ids 242 243 244
.\.venv\Scripts\python.exe -m scripts.crawlers.main.run --page-ids 245 246
.\.venv\Scripts\python.exe -m scripts.crawlers.main.run --page-ids 95
```

전체 등록 페이지 실행은 `--all`을 명시합니다. 자세한 구조는 [메인 홈페이지 가이드](../crawlers/MAIN.md)를 참고하세요.

기존 정적 크롤러는 문단·목록, 표의 행·열 및 병합 셀, 절차도의 카드 순서를
공통 파서로 구조화합니다. `/main/92`, `/main/94`, `/main/230`–`232`도
이 경로를 사용하며 결과는 `files/pknu_student_life/output/json/`에 저장됩니다.

`/main/233`–`235`는 이미지 안내문 원본과 한국어 OCR 블록을 수집합니다.
OCR은 오인식 가능성이 있어 `needs_review`로 저장하며, 결과는
`files/pknu_main/output/major_program/main_<id>.json`에 있습니다.

`/main/94`를 실행하면 상단 탭에서 파일 경로 `/main/238`을 발견해
기존 PDF 파일 크롤러도 실행합니다. 원본 PDF와 추출 텍스트는
`files/pknu_student_life/output/` 아래에 저장됩니다.

### `scripts/crawlers/main/collectors/academic_calendar.py`

학사일정(`/main/31`)의 월별 JSON API를 조회합니다. 기본값은 현재 연도의 12개월이며,
`--months`로 필요한 달만 지정할 수 있습니다. 같은 일정이 여러 달에 나타나면
`source_record_id`로 합치고 조회된 달을 `source_months`에 남깁니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.crawlers.main.collectors.academic_calendar --year 2026
.\.venv\Scripts\python.exe -m scripts.crawlers.main.collectors.academic_calendar --year 2026 --months 9 10
```

결과는 `files/pknu_main/output/academic_calendar/main_31_<year>.json`에 저장됩니다.
일부 달만 조회하면 파일명에 `_m09-10`처럼 조회한 달이 붙습니다.
날짜가 잘못된 원본 행은 `rejected_events`에 기록되고 결과 상태가 `needs_review`가 됩니다.

### Main 분석/점검 스크립트

- `scripts/crawlers/main/student_life_stats.py`: `files/pknu_student_life/output/json` 결과의 PDF 텍스트 추출 상태를 요약합니다.

## Department Crawler

### `scripts/crawlers/departments/engine.py`

`scripts/crawlers/departments/engine.py`는 특정 학과에 종속되지 않는 공통 학과 크롤러입니다. 대상 학과의 사이트·게시판·CMS adapter 설정은 `scripts/crawlers/departments/registry.json`에서 읽습니다. CMS별 HTML 파싱은 `scripts/crawlers/departments/adapters/`에 두며, 다른 학과를 추가할 때 엔진을 복사하지 않습니다. `adapter`를 생략하면 기본 `numeric_cms`가 사용됩니다.

`?mcode=...` 메뉴와 `?menucode=...&mode=2&no=...` 상세 URL을 쓰는 사이트는 `query_mcode` adapter를 선택합니다. 이 형식에서는 section ID가 `mcode_<mcode>`, 문서 `source_id`가 상세 URL의 `no`이며 `bbs_id`는 사용하지 않습니다.

루트에서 동일 호스트의 PHP 메인 페이지로 meta-refresh한 뒤 `bbscode`, `idx`, `kind=view`를 사용하는 사이트는 `legacy_php` adapter를 선택합니다. meta-refresh는 HTTP(S) 동일 호스트 URL만 최대 한 번 따라가며, 게시판 section은 `bbscode`, 문서는 `idx`를 안정적인 식별자로 사용합니다.

`/html/<section>/<page>.php` 메뉴에서 `mode=read&idx=<id>` 상세 URL을 사용하는 사이트는 `html_php` adapter를 선택합니다. 목록 페이지는 `pagenum`으로 이동하며 `category_idx`는 필터로만 유지하고 문서 ID로 사용하지 않습니다.

학과 주소와 registry 검증:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py validate
```

`--all` 실행 전 대상과 제외 이유를 네트워크 요청 없이 확인:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py list-ready
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py list-ready --json
```

`list-ready`는 registry와 이미 저장된 `files/_discovery` 결과만 읽으며 크롤링,
상태 파일 저장, discovery 갱신을 수행하지 않습니다. `ready`, `disabled`,
`pending_review`, `blocked`, `requires_adapter`, `hub_only` 상태와 adapter,
활성 section 수, 제외 이유를 출력합니다. `ready` 목록은 실제 `--all` 대상과
동일합니다. Registry를 읽거나 검증할 수 없으면 종료 코드 1, 잘못된 옵션은 2입니다.

전체 활성 학과를 소량으로 확인하려면 각 dataset의 첫 게시판 하나에서 최근
1페이지와 문서 최대 1건만 처리하고 첨부파일 다운로드를 생략합니다.

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\engine.py --all --once --max-board-sections 1 --recent-only 1 --max-items 1 --no-download-files
```

`--max-board-sections N`은 registry 순서상 활성 게시판을 dataset마다 최대 N개
선택하고 정적 페이지는 제외합니다. `--max-items` 역시 dataset마다 새로 적용됩니다.
두 옵션의 0 이하 값과 `--section`/`--max-board-sections` 조합은 CLI 오류인 종료
코드 2를 반환합니다.

단일 홈페이지 구조 probe와 section discovery:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py probe --site-key ce.pknu.ac.kr
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py discover --site-key ce.pknu.ac.kr
```

`probe`와 `discover`는 실제 네트워크를 사용합니다. 결과는 `files/_discovery/<site_key>/probe.json`과 `discovery.json`에 원자적으로 저장됩니다. discovery 후보는 검토 없이 `registry.json`에 자동 반영되지 않습니다.

새 결과 파일의 `provenance`에는 adapter 이름·버전, 실행에 사용한 registry 설정
hash, registry URL, 실행 시각이 기록됩니다. `list-ready`는 이를 현재 registry와
비교해 `fresh`, `stale`, `legacy`, `missing`을 표시합니다. URL·adapter·adapter
버전·관련 registry 입력이 달라진 결과와 provenance가 없는 기존 결과는
`apply-discovery`에서 자동 반영되지 않으므로 해당 dataset의 probe/discovery를
다시 실행해야 합니다.

크롤링 준비가 되지 않은 전체 dataset 일괄 probe와 discovery:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py probe-all
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py discover-all
```

호환성 probe를 통과하지 못한 사이트도 discovery를 시도하려면 `--force`를 사용합니다.

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py discover-all --force
```

일괄 실행은 dataset 하나가 실패해도 다음 항목을 계속 처리합니다. 개별 결과는 기존 `files/_discovery/<site_key>/` 아래에 저장하고, 전체 요약은 `files/_discovery/probe-all.json` 또는 `discover-all.json`에 저장합니다.

일괄 실행 중에는 각 dataset의 probe/discovery 시작과 완료, 현재 순번, 전체 개수, 진행률, 누적 성공·실패·건너뜀 수와 경과 시간이 터미널에 즉시 출력됩니다.

```text
2026-09-09T18:30:00+09:00 INFO department_discovery batch_progress run_id=... completed=14 total=97 percent=14.4 dataset_name=fashion status=success successful=13 failed=0 skipped=1 elapsed_seconds=620.3
```

동일한 로그는 다음 파일에도 UTF-8로 저장됩니다.

```text
logs/crawlers/department_discovery.log
logs/crawlers/department_discovery.jsonl
```

PowerShell 출력 지연이 의심되면 Python의 비버퍼링 옵션을 함께 사용할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -u scripts\crawlers\departments\cli.py discover-all --force
```

discovery 결과를 registry에 반영하기 전 기본 dry-run:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py apply-discovery
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py apply-discovery --dataset econ
```

dry-run의 `changes`, `skipped`, section 수와 경고를 검토한 뒤 실제 반영합니다.

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py apply-discovery --dataset econ --apply --confirm-reviewed
```

실제 반영에는 `--apply`와 `--confirm-reviewed`가 모두 필요합니다. 반영 전 기존 registry는 `registry.json.bak`으로 백업하며, 기존 백업이 있으면 덮어쓰지 않고 중단합니다. 사용할 수 없는 section, 중복 ID, `bbs_id`가 없는 게시판 후보는 자동으로 제외됩니다. 자동 discovery의 `category`는 `미분류`일 수 있으므로 운영 크롤링 전에 registry에서 최종 분류를 확인해야 합니다.

학과 홈페이지는 모두 공통 엔진으로 크롤링합니다. 게시글 JSON, 원본 HTML,
첨부파일은 `files/<dataset>/output` 아래에 저장합니다. CE도 별도 진입점 없이
동일한 명령 구조를 사용합니다.

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\engine.py --dataset <dataset> --once
.\.venv\Scripts\python.exe scripts\crawlers\departments\engine.py --dataset ce --once
```

registry에서 활성화되고 section 설정이 준비된 모든 학과를 순차적으로 크롤링:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\engine.py --all --once
```

`--all`은 `enabled=true`이며 활성 section이 하나 이상인 dataset만 실행합니다. CSV에서 주소 정보만 가져온 미준비 dataset은 건너뛰고 시작 시 `ready`와 `skipped_not_ready` 개수를 출력합니다.

옵션:

- `--once`: 스케줄 루프 없이 즉시 1회 실행합니다.
- `--all`: registry에서 크롤링 준비가 끝난 모든 활성 dataset을 순차 실행합니다.
- `--reset-state`: `state.json`을 삭제하고 처음부터 다시 수집합니다.

## Rule Crawler

### `scripts/crawlers/pknu_rule.py`

부경대학교 규정집을 크롤링합니다.
규정 JSON, HTML, 첨부파일, 첨부파일 텍스트 추출 메타데이터를 `files/rule/output` 아래에 저장합니다.

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\pknu_rule.py --laws --bylaws
```

옵션:

- `--laws`: 학칙/규정 트리 항목을 수집합니다.
- `--bylaws`: 지침/세칙 목록과 상세 페이지를 수집합니다.
- `--download-files`: 첨부파일을 다운로드합니다. 기본값입니다.
- `--no-download-files`: 첨부파일 다운로드와 첨부파일 텍스트 추출을 생략합니다.
- `--max-law-items N`: smoke test용으로 학칙/규정 항목 수를 제한합니다.
- `--max-bylaw-pages N`: smoke test용으로 지침/세칙 목록 페이지 수를 제한합니다.

예시:

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\pknu_rule.py --laws --max-law-items 1
.\.venv\Scripts\python.exe scripts\crawlers\pknu_rule.py --bylaws --max-bylaw-pages 1
.\.venv\Scripts\python.exe scripts\crawlers\pknu_rule.py --laws --bylaws --no-download-files
```
