# 부경대학교 메인 홈페이지 크롤러

메인 홈페이지(`https://www.pknu.ac.kr/main/…`)의 수집 명령어를 정리한 문서입니다.
모든 명령은 프로젝트 루트(`campus-rag/`)에서 실행합니다.
수집 대상과 저장 경로는 [pages.json](../../scripts/crawlers/main/pages.json), 실행 진입점은 [run.py](../../scripts/crawlers/main/run.py)입니다.

## 실행 준비

프로젝트 Python 가상환경과 의존성이 설치된 상태에서 실행합니다.
최초 설치 방법은 [프로젝트 README](../project/README.md#백엔드-설치)를 참고하세요.

```powershell
# 프로젝트 루트에서 가상환경 활성화
.\.venv\Scripts\Activate.ps1

# 지원 옵션 확인
python -m scripts.crawlers.main.run --help
```

가상환경을 활성화하지 않을 경우 아래 예시의 `python`을
`.\.venv\Scripts\python.exe`로 바꿔 실행할 수 있습니다.

## 실행 대상 확인

```powershell
# 활성 등록된 페이지 목록과 담당 수집기 확인
python -m scripts.crawlers.main.run --list

# 지정한 페이지의 실행 계획 확인
python -m scripts.crawlers.main.run --page-ids 31 92 95 --dry-run

# 특정 카테고리의 실행 계획 확인
python -m scripts.crawlers.main.run --category "대학소개" --dry-run
```

`--dry-run`은 수집 요청이나 결과 저장 없이 등록 정보만 확인합니다.
출력의 `configured`는 활성 등록, `excluded`는 비활성 등록,
`needs_review`는 미등록 페이지를 뜻합니다.
`--list`에는 활성 등록만 표시되므로 제외 여부는 페이지 ID를 지정해 확인합니다.

`--page-ids`, `--category`, `--all`, `--boards`, `--list` 중 하나만 선택할 수 있습니다.
페이지 ID는 `--page-ids 31 92 95`처럼 공백으로 구분합니다.

## 수집 명령어

### 페이지별 실행과 제한 수집

```powershell
# 한 페이지 수집
python -m scripts.crawlers.main.run --page-ids 481

# 여러 페이지 수집
python -m scripts.crawlers.main.run --page-ids 224 225

# 일반 게시판의 첫 번째 목록 페이지와 그 목록의 게시글 수집
python -m scripts.crawlers.main.run --page-ids 32 --board-pages 1

# 학사일정: 지정 연도의 전체 월 수집
python -m scripts.crawlers.main.run --page-ids 31 --year 2026

# 학사일정: 지정한 월만 수집
python -m scripts.crawlers.main.run --page-ids 31 --year 2026 --months 9 10
```

`--board-pages 1`은 **게시글 1개가 아니라 목록 1페이지**입니다.
통합 CLI에는 게시글 개수를 지정하는 `--max-posts` 옵션이 없습니다.
공지사항 #163과 등록금 안내 #102의 FAQ는 별도 수집 경로를 사용하므로
이 옵션으로 수집량을 제한할 수 없습니다. 공지사항의 제한 수집은 아래 명령을 사용하세요.

### 게시판만 전체 목록 수집

```powershell
# 실제 요청 없이 대상 페이지 ID 확인
python -m scripts.crawlers.main.run --boards --all-board-pages --dry-run

# 등록된 게시판만 선택해 모든 목록 페이지와 게시글 수집
python -m scripts.crawlers.main.run --boards --all-board-pages --report files/pknu_main/_runs/main_boards_run_report.json

# 지정 게시판 하나의 전체 목록 수집
python -m scripts.crawlers.main.run --page-ids 95 --all-board-pages
```

`--boards`는 활성 `board`·`notice`·`tuition` 수집기 페이지를 선택합니다.
등록금 안내는 FAQ를 포함하므로 본문과 FAQ를 함께 수집합니다.
비활성화된 커뮤니티 게시판이나 다른 안내 페이지는 선택하지 않습니다.
`--all-board-pages`는 일반 게시판·공지사항의 목록 제한을 해제하며 등록금 FAQ는 기존처럼 전체 목록을 수집합니다.
기존 성공 결과·첨부파일 재사용은 유지합니다. `--full-resync`를 추가하면 지원하는 수집기의 재사용도 해제합니다.
`--board-pages N`과 `--all-board-pages`는 함께 사용할 수 없습니다.

### 전체 및 상단 카테고리별 실행

```powershell
# 활성 등록된 모든 페이지 수집
python -m scripts.crawlers.main.run --all
```

| 상단 카테고리 | 실행 명령어 |
| --- | --- |
| 대학/대학원 | `python -m scripts.crawlers.main.run --category "대학/대학원"` |
| 연구/산학 | `python -m scripts.crawlers.main.run --category "연구/산학"` |
| 대학생활 | `python -m scripts.crawlers.main.run --category "대학생활"` |
| 입학/취업 | `python -m scripts.crawlers.main.run --category "입학/취업"` |
| 커뮤니티 | `python -m scripts.crawlers.main.run --category "커뮤니티"` |
| 대학소개 | `python -m scripts.crawlers.main.run --category "대학소개"` |
| 개교 80주년 | 현재 사용자 제외 상태. 활성 수집 대상 없음 |

카테고리 이름은 상단 메뉴 표기(`/` 포함)를 사용합니다.
저장 폴더 이름인 `연구_산학` 등을 `--category`에 넣으면 안 됩니다.
전체·카테고리 실행 범위는 `pages.json`의 `enabled: true`인 페이지이며,
사이트 전체 링크를 자동으로 탐색하는 방식이 아닙니다.

### 공지사항: 최근 목록만 수집

```powershell
# 공지사항의 각 분류에서 첫 번째 목록 페이지 수집
python -m scripts.crawlers.pknu_notice --recent-only 1

# 특정 공지 분류의 첫 번째 목록 페이지 수집
python -m scripts.crawlers.pknu_notice --only-cd 10001 --recent-only 1

# 기본 증분 수집: 각 분류에서 최근 3개 목록 페이지 확인
python -m scripts.crawlers.pknu_notice
```

공지사항 분류 코드는 `scripts/crawlers/pknu_notice.py`의 `CD_TO_LABEL`에서 확인합니다.
통합 명령의 `--page-ids 163` 및 `--category "커뮤니티"`도 이 수집기를 호출하며,
일반 실행에서는 최근 3개 목록 페이지를 확인합니다.
통합 명령의 `--board-pages N`은 공지사항에 전달되지 않습니다.
`--all-board-pages`를 사용하면 공지사항도 분류별 전체 목록을 확인합니다.
현재 커뮤니티는 공지사항만 활성화되어 있으며 #51·52·53 등 기존 수집 결과는 보존됩니다.

### 연구기관만 실행

```powershell
python -m scripts.crawlers.pknu_research_institutes --list
python -m scripts.crawlers.pknu_research_institutes --all --dry-run
python -m scripts.crawlers.pknu_research_institutes --page-ids 81
python -m scripts.crawlers.pknu_research_institutes --all
```

`연구/산학/연구기관` 아래 활성 등록된 페이지만 선택합니다.
`--category "연구/산학"`은 연구기관 외 연구 기사와 산학협력 페이지도 포함합니다.
학과·단과대학의 독립 홈페이지는 [학과 크롤러 README](DEPARTMENTS.md)를 참고하세요.

### 대학생활의 가이드·E-하나로 개별 실행

```powershell
# 대학생활 가이드 PDF 1개만 처리
python -m scripts.crawlers.pknu_student_life --mode guide --limit 1

# E-하나로 수집
python -m scripts.crawlers.pknu_student_life --mode ebook
```

이 CLI는 대학생활 전용입니다. `--limit`은 가이드 PDF 개수에만 적용되며
E-하나로의 페이지 수를 제한하지 않습니다.
일반 페이지 수집에는 `scripts.crawlers.main.run --page-ids …`를 사용합니다.

### 재수집과 보고서 경로 지정

```powershell
# 기존 결과 재사용을 해제하고 지정 페이지 갱신
python -m scripts.crawlers.main.run --page-ids 481 --full-resync

# 실행 보고서를 프로젝트 내부의 별도 경로에 저장
python -m scripts.crawlers.main.run --page-ids 481 --report files/pknu_main/_runs/parking_run.json
```

기본 실행은 각 수집기의 증분 처리·캐시 정책에 따라 기존 결과를 재사용합니다.
`--full-resync`는 이를 지원하는 수집기에서 기존 결과나 해시를 이용한 건너뛰기를 해제합니다.
공지사항에 적용하면 전체 목록을 수집하므로, `--all --full-resync`는
공지사항을 포함한 대규모 재수집이 필요할 때 사용합니다.

### 통합 CLI 옵션

| 옵션 | 의미 / 기본값 |
| --- | --- |
| `--page-ids ID [ID ...]` | 실행할 페이지 ID. 공백으로 구분 |
| `--category "카테고리"` | 해당 상단 카테고리의 활성 등록 페이지 선택 |
| `--all` | 모든 활성 등록 페이지 선택 |
| `--boards` | 활성 일반 게시판·공지사항·등록금 FAQ 페이지 선택 |
| `--list` | 활성 등록 목록 출력 |
| `--dry-run` | 수집 없이 실행 계획 출력 |
| `--year YEAR` | 학사일정 연도. 기본값은 한국 시간 기준 현재 연도, 범위 2000~2100 |
| `--months MONTH [MONTH ...]` | 학사일정 월. 기본값 1~12월, #31 선택 시 사용 가능 |
| `--board-pages N` | 일반 게시판 목록 페이지 수. 기본값 1, 최소 1. 공지사항·등록금 FAQ에는 미적용 |
| `--all-board-pages` | 일반 게시판·공지사항 전체 목록 수집. 기존 성공 결과는 재사용. `--board-pages`와 함께 사용 불가 |
| `--full-resync` | 지원하는 수집기에서 기존 결과 재사용 해제 |
| `--report PATH` | 통합 보고서 경로. 프로젝트 내부 경로만 허용 |

## 결과 확인 및 수집 정책

- 수집 결과: `files/pknu_main/<상단 카테고리>/<하위 카테고리>/`.
- 기본 통합 보고서: `files/pknu_main/_runs/main_run_report.json`. 같은 경로로 재실행하면 갱신됩니다.
- 연구기관 전용 보고서: `files/pknu_main/_runs/research_institutes_run_report.json`.
- 보고서의 전체 `status`, `routes`별 상태, `collectors` 내 오류와 결과 경로를 확인합니다.
  `needs_review`·`partial_success`가 있으면 해당 결과를 검토합니다.
- `enabled: false`인 사용자 제외·보류·중복 페이지는 자동 실행에서 제외됩니다.
- 본문 이미지 OCR은 현재 비활성화되어 있습니다. 이미지 원본 저장 여부는 페이지 설정을 따릅니다.
- 홈페이지 바로가기, 지도, 첨부파일의 수집 범위는 페이지별 등록 옵션을 따릅니다.
  로그인이나 접근 권한이 필요한 다운로드는 보류 기록을 확인합니다.
- 수집 명령은 인벤토리 CSV를 자동 갱신하지 않습니다. 변경 사항을 작업 기록에 모으고,
  사용자 요청 시 `pknu_main_collection_inventory.csv`의 기존 서식을 유지하여 반영합니다.

## Code structure

`run.py` groups requested pages by their top navigation category and calls the
corresponding module in `categories/`. Page IDs and collection policies live in
`pages.json`; collectors select behavior by handler and options rather than by
adding a branch for every page number.

| Navigation category | Entry point |
| --- | --- |
| 대학/대학원 | shared introduction and public staff-widget handler |
| 연구/산학 | shared page handlers for research articles, institute introductions and industry-cooperation tables |
| 대학생활 | `categories/student_life.py` |
| 입학/취업 | shared page handlers for registered main-site pages |
| 커뮤니티 | `categories/community.py` |
| 대학소개 | `categories/university_intro.py` |
| 개교 80주년 | user-excluded; no active collection targets |

`categories/registry.py` defines all seven categories. Navigation names containing
a slash use `_` in directory names, such as `대학_대학원`.
Unpopulated categories do not need an empty crawler module: registering a page
with a shared handler is sufficient.
The user excluded all `대학소개/홍보관` and `개교 80주년` pages on 2026-10-14.
Their confirmed aliases are also disabled; existing files are retained.

`common/` contains HTTP sessions, state/document storage, board parsing,
attachment downloads, PDF text extraction and shared page orchestration.
`collectors/` contains the reusable static/board/file collectors and specialized
calendar, guide, eBook, tuition, organization, campus-building and news collectors. Shared body
image handling lives in `scripts/crawlers/common/body_images.py` and is also used
by department crawlers.

`scripts/crawlers/pknu_student_life.py` remains a small compatibility CLI for
student-life pages, guides and E-하나로. It does not collect university-introduction
or community pages; use `scripts.crawlers.main.run` for those categories.
Existing state keys, document IDs and logical RAG dataset names are preserved.
Tests are separate from normal crawler execution.

## Storage

All main-website results live under `files/pknu_main/`:

```text
files/pknu_main/
  대학_대학원/
    대학(학부)/
      AX혁신대학/             # page 531 introduction and public staff table
      글로벌혁신대학/         # page 532 introduction, department list and public staff table
  연구_산학/
    PKNU Research 1000/       # page 32 English research articles
    연구기관/
      공동실험실습관/         # page 81 introduction and public staff table
    산학협력/
      연구비 수혜 현황/       # page 82; page 34 has the same body
      산학협력 추진 실적/     # page 83 prose and tables
  대학소개/
    대학이념과 인재상/
    조직도/
    총장실/                   # greeting, profile, former presidents, speeches
    현황/
      대학통계/               # page 16 tables and selected annual reports
    캠퍼스안내/
      오시는 길/              # pages 224 and 225; body only, maps excluded
      캠퍼스맵/               # page 133 building/floor/room information
      캠퍼스투어 신청/        # page 227 body and page 228 FAQ subfolder
      시설대관/               # page 385 guide/files and page 386 selection results
      3길 17경/               # page 302 image text and page 495 landmark selection index
      주차요금/               # page 481 parking fees with merged table cells
  대학생활/
    학사안내/                 # includes 학사일정, 전공제도, 학점교류
    학사정보/                 # includes E-하나로 and 등록금_안내
    교육과정/                 # includes 이수_로드맵
    수강신청/                 # page 110 lecture-catalog PDF only; page 28 refers to 110
    학생생활/                 # includes 교내_식당_주간식단표, 대학생활_가이드
  커뮤니티/
    공지사항/                 # subfolders for each notice category
    부경투데이/
    교수동정/
    부경나우/
  _runs/                      # run results and collection manifests
  _state/                     # incremental crawler state and backups
  _derived/<dataset>/
    preprocessed/             # RAG extraction and chunks
  _archive/<dataset>/         # retired, deleted and interrupted results
  _discovery/                 # inventory and page compatibility evidence
```

Each category uses the same format folders, created when needed:

```text
<category>/
  json/
    pages/                    # page body, calendar and collection summaries
    posts/                    # individual board/news articles
    attachments/              # extracted PDF, guide and eBook documents
  html/                       # saved source HTML
  files/<document-id>/
    images/
    pdf/
    office/                   # HWP/HWPX, Word, Excel and PowerPoint
    archives/
    other/
```

Existing hashed filenames and document IDs are preserved. The logical datasets
`pknu_student_life` and `pknu_notice` are also preserved for state, RAG processing
and database identity; their physical folders now share `pknu_main`.
The two incremental state JSON files are kept in Git; other collected data and
state backups remain ignored.
JSON `saved_path`, `document_json` and other local references point to the new
locations. Specialty summaries retain their `main_<id>.json` names.

The common static parser preserves paragraphs, lists, tables with merged cells,
and CMS process diagrams. Previously extracted image OCR remains reviewable in
the JSON; new runs follow the disabled body-image OCR policy.
PDF/eBook documents retain cleaned text by page.

## 페이지별 수집 참고

| 수집 대상 | 페이지별 실행 예시 | 등록된 수집 범위 |
| --- | --- | --- |
| 조직도 | `python -m scripts.crawlers.main.run --page-ids 39` | 조직도 노드와 연결된 소개·안내, 홈페이지 바로가기 제외 |
| 총장실 | `python -m scripts.crawlers.main.run --page-ids 40 41 42 374 --board-pages 1` | 본문과 연설모음 목록 1페이지 |
| 역대총장 카드 | `python -m scripts.crawlers.main.run --page-ids 212 213 214 215` | 카드 구조와 텍스트, 이미지 제외 |
| 연혁 | `python -m scripts.crawlers.main.run --page-ids 217 492` | 날짜별 연혁 |
| 대학통계 | `python -m scripts.crawlers.main.run --page-ids 16` | 본문 표와 최근 3개년 통계연보 |
| 캠퍼스맵 | `python -m scripts.crawlers.main.run --page-ids 133 134` | 건물번호와 공개 상세정보, 지도 제외 |
| 시설대관 | `python -m scripts.crawlers.main.run --page-ids 385 386` | 안내·첨부와 건물/시설 선택 결과 |
| 주차요금 | `python -m scripts.crawlers.main.run --page-ids 481` | 표와 본문 |
| 교육과정 | `python -m scripts.crawlers.main.run --page-ids 106 362` | PDF 첨부파일 |
| 강의편람 | `python -m scripts.crawlers.main.run --page-ids 110` | 강의편람 PDF만 수집, #28의 대표 페이지 |
| 식당 주간식단표 | `python -m scripts.crawlers.main.run --page-ids 399 --board-pages 1` | 목록 1페이지, 별도 하위 폴더 저장 |

Pages 51, 52 and 53 retain their earlier first-list-page samples with image/link
URLs; image binaries were not downloaded. Their routes are currently disabled.
Page 32 uses the shared board collector (`bbsId=9`) under
`연구_산학/PKNU Research 1000/`, preserving English paragraphs, image markers
and source alt text with `language: "en"`. The initial sample fetched exactly
two articles from list page 1; no translation was generated. Subsequent runs
use the standard `--board-pages` scope and include page 32 in `--all` and
`--category "연구/산학"`. Programmatic board samples can use `max_posts` to
bound detail requests without limiting the registered board's future scope.
Run reports are saved in `_runs/main_run_report.json` and
`_runs/student_life_route_inventory.json`. News manifests live in `_runs/pknu_today/`.

Page 94 includes the graduation-requirements PDF at page 238. Page 102 includes
the linked tuition FAQ board at page 250; the tuition payment page 251 is not
crawled. Curriculum pages 106 and 362 collect their PDF attachments only.
Registered link exclusions (including pages 262, 263, 306–308 and 494) are retained.
Pages 100, 416 and 472 are excluded.

Research institute pages use `category_path: ["연구_산학", "연구기관", "<기관명>"]`
in `pages.json`. Page 81 (공동실험실습관) already follows this layout, with its
result in `json/pages/main_81.json` and source in `html/main_81.html` inside the
institute directory. Register additional research institutes under the same
parent folder, keeping each institute's files in its own directory.
Use `scripts/crawlers/pknu_research_institutes.py` as the separate institute
collection entry point. It selects enabled routes under `연구_산학/연구기관`
from `pages.json` and delegates to the existing main-site collectors. Newly
registered institutes are included automatically; other research/industry
categories are outside this entry point's scope.

Page 81 uses `organization_intro` to retain introduction paragraphs, displayed
contact fields, analysis-area lists and the public staff table. Its homepage
shortcut is recorded only; the linked homepage is not fetched.

Page 531 uses the shared `organization_intro` handler and stores the AX Innovation
College introduction under `대학_대학원/대학(학부)/AX혁신대학/`. It preserves the
introductory paragraphs, location/phone/fax, school/department list and the public
staff table loaded by the page's own organization widget. The collector refreshes
the page and widget on each run. College and department homepage URLs are recorded
in `reference_links` with `followed: false`; their contents and attachments are
not fetched. This reuses the organization-chart introduction parser and staff
extraction without adding a page-specific crawler or category module.

Page 532 reuses `organization_intro` under `대학_대학원/대학(학부)/글로벌혁신대학/`.
It preserves the introduction, location/phone/fax, seven department names
(three English-track and four Korean-track entries) and the public staff table.
The department links currently point to `#`, so their names remain a list
without fabricated destination URLs. The college homepage shortcut is recorded
with `followed: false`; connected pages and attachments are not fetched.

The Global Innovation College homepage is registered separately as `gic` in
`scripts/crawlers/departments/registry.json` and `sites.csv`. Use the department
engine for its requested introduction, foreign-student department and
scholarship/living-support pages. Its data lives under
`files/department/글로벌혁신대학/gic/`; this does not change page 532's main-site
route. The empty notice board `/gic/8879` is registered with `enabled: false`
until the user confirms the collection method.

Page 38 stores the university philosophy and talent profile under
`대학소개/대학이념과 인재상/`. Its earlier sample has four body images and reviewable
OCR text stored separately for each image in the page JSON. New runs keep image
originals according to the route settings without running body-image OCR.
The inspected sample also includes `images[].visual_extraction` with text,
table rows and diagram relationships read from the saved originals by the
assistant. Tesseract output is stored in `images[].ocr_text`.

Page 39 stores the organization chart under `대학소개/조직도/json/pages/main_39.json`.
`nodes` and `edges` preserve the hierarchy, display groups, side branches and
annotations. Each linked node's `document_json` points to its introduction;
the introduction's `organization_links` points back to that node and its path.
Registered college and graduate-school introductions live under
`대학_대학원/<menu group>/<page name>/`; research institute introductions live
under `연구_산학/연구기관/<institute name>/`. Other organization introductions
remain under `대학소개/조직도/소개안내/`. Each directory keeps `json/pages/` and
`html/` together. Chart collection uses a registered introduction's own storage
path, including during a full refresh. Independent introduction refreshes keep
their `organization_links` and `organization_name` metadata. Existing results
such as page 533 are reused.
Only node destinations and introduction menus on directly linked university
sites are collected. Homepage shortcuts and other body links are recorded
without visiting them; boards and attachments are excluded.

Pages 40, 41 and 42 use the shared static collector and save under
`대학소개/총장실/json/pages/`. Page 42 collects the initially displayed `2001~`
tab; other year tabs are recorded as links without following them. Portraits and
the signature are saved as original images with OCR disabled by configuration.
Page 374 uses the shared card-board parser. Its list summary is saved under
`총장실/연설모음/json/pages/main_374.json`, and full speech documents under
`총장실/연설모음/json/posts/`.

Page 16 stores the three university-statistics tables under
`대학소개/현황/대학통계/json/pages/`. Table captions, reference dates, units and
merged-cell coordinates are retained. The shared static collector also reads
same-site attachment URLs from `option[download][value]` controls.
During testing, `attachment_limit: 3` selects the latest three available years
(currently 2023–2025), while recording every other year as an attachment link.
Original reports are stored under the page's `files/<document-id>/pdf/` and
`office/` folders; extracted text is included in the page JSON's `attachments`.
PDF text also retains `text.pages` and `text.page_count`.
Select-menu controls are excluded from body text through `exclude_body_selectors`.
The other pages in the status category are not followed.

Pages 224 and 225 use the existing static collector for Daeyeon and Yongdang
campus directions. Their configuration excludes `.campus_info_guide`, which
contains the interactive map and its controls. Only the text below the map is
collected: campus address, subway/bus routes, driving directions and campus
shuttle directions. Results live under `대학소개/캠퍼스안내/오시는 길/json/pages/`.
Linked pages and attachments are not collected.

Page 133 collects the visible Daeyeon campus building list and each building's
details from the page's `/buildingInfoAjax.do` widget. The result is saved under
`대학소개/캠퍼스안내/캠퍼스맵/json/pages/main_133.json`. The `buildings` array links
building codes/numbers to `floors[].rooms[]`, retaining room numbers and names.
Ground, basement and rooftop floors remain distinct; inactive records are omitted.
Cleaned text for all buildings is also stored in `content`. Building photo URLs
are recorded without downloading the images. Map tiles, coordinates, routing
links and the other campus tab are excluded. Completed results are reused unless
`--full-resync` is requested.

Pages 227 and 228 use the existing static/board collectors for campus-tour
applications and FAQ answers. Results live under
`대학소개/캠퍼스안내/캠퍼스투어 신청/`: page 227's body in `json/pages/`,
page 228's board summary in `FAQ/json/pages/main_228.json`, and FAQ answers in
`FAQ/json/posts/`. The initial collection is limited to one list page.
Page 227 excludes the Google Forms application link and does not follow body
links, including the VR tour. FAQ posts preserve paragraphs, lists and tables
through the existing structured-post option; links inside answers are not followed.
HTML comments used as editor/template markers are excluded from extracted text.

Page 385 uses the shared static collector for facility-rental rules, tables and
the application/fee HWP attachments, stored under `대학소개/캠퍼스안내/시설대관/`.
The shared parser retains struck-out text as `[취소선: ...]` so removed rooms and
facilities are distinguishable in plain text.
Page 386 uses `collectors/facility_rental.py` to enumerate every building option,
then every room/facility returned by `/facility/roomList.do`, and fetch its details
from `/facility/view.do`. The complete result lives in
`시설대관검색/json/pages/main_386.json`. `buildings[].facilities[]` preserves each
selection's seat count, area, purpose and structured guidance. Empty room lists
are recorded explicitly. Photo URLs are kept without downloading the photos;
links inside guidance are recorded without following them. Completed page results
are reused unless `--full-resync` is requested.

Pages 302 and 495 store results under `대학소개/캠퍼스안내/3길 17경/`.
Page 302's previously inspected sample uses `content_from_images` to store text
separately in `image_text_sections`, ordered by the source images: 시간으로,
바람으로 and 이야기로. The original images and raw OCR remain in `images`.
The inspected sample includes `images[].visual_extraction` with cleaned text,
map labels and references to the landmark documents. Reviewed text is reused
only when its `source_sha256` matches the downloaded original.
Since the user's 2026-10-14 decision, `collection_policy.body_image_ocr` is false.
Future static, major-program and discovery runs preserve original images and
available HTML text without running image OCR. This policy overrides page OCR
options; previously saved OCR samples remain available without being recrawled.

Page 495 uses the shared `linked_pages` handler in `collectors/navigation.py`.
Its configured `.spot-nav` selector limits collection to registered static pages
in the same category. The index at `17경/json/pages/main_495.json` lists all
17 choices, with `choices[].document_json` pointing to the 15 available pages.
The planned fifth and seventeenth landmarks have no destination and remain
`unavailable`; the crawler does not guess their URLs. `selected_document_json`
points to page 498, whose body also appears at page 495, avoiding a duplicate
body document. Each destination links back through `page_group_links`.
Existing complete results are reused unless `--full-resync` is requested.
Body links outside the configured selection list are not followed.

Page 481 stores the parking-fee guide under `대학소개/캠퍼스안내/주차요금/`.
The shared static parser retains the table caption, headers and each cell's
row/column coordinates and `rowspan`/`colspan` in `structured_content`, alongside
the effective-date note and footnote. Source HTML is saved under `html/`.
Linked pages and attachments are not collected.

To move a legacy checkout's data, first preview and then apply:

```powershell
python -m scripts.migrations.main_storage
python -m scripts.migrations.main_storage --apply
```

The migration validates destination collisions before moving files, verifies
file hashes, rewrites local JSON/state/RAG references, and checks that previously
valid file references still exist. Interrupted and retired results are archived.
The path mapping and summary are saved in `_runs/storage_migration_paths.json`
and `_runs/storage_migration.json`. Repeating the migration finds no legacy files
to move. This replaces the former academic-info and weekly-menu migration scripts.

## Adding collection scope

Add a verified page to `pages.json` with its existing handler and category path.
After sample verification and registration, queue the matching inventory changes
in `_runs/main_inventory_batch_20261013.json` (`csv_updates` and
`pending_csv_page_ids`), including status, registration, result paths, tested scope
and verification date. Update the project-root `pknu_main_collection_inventory.csv`
only when the user explicitly requests synchronization. Preserve other rows and
CSV formatting; distinguish a small successful sample from complete board collection.
Options include `board_id`, `board_title`, `collect_intro`, `structured_posts`,
`post_subcategory`, `document_subcategory`, `document_title`, `ocr_body_images`,
excluded link/image hosts and attachment validation requirements.
`collect_body_images: false` excludes body images from extraction and downloads.
`card_selector`, `card_title_selector` and `card_field_selectors` collect text
cards as separate ordered `card` blocks under `structured_content.sections`.
Each card keeps its title and named arrays of text fields; dates and positions
are preserved without inferring one-to-one pairings. Pages 212–215 use these
options for former presidents' cards, with image collection disabled.
`attachment_limit` limits downloads in descending year order, and
`exclude_body_selectors` removes configured interface controls from body text.
`record_attachment_links` preserves detected file links in the page JSON.
`download_pdf_attachments: false` keeps direct PDF and embedded viewer PDF URLs
as references without downloading them. Page 445 uses this scope by user choice.
`collect_audio_files` includes same-site audio sources as original attachments;
`attachment_extensions` limits downloads to the configured extensions (for
example `[".mp3"]`). Audio originals are stored under `files/<slug>/audio/`
and are not passed to document text extractors. `allow_asset_only_body` permits
a short HTML body when selected attachments or body images are present. Page
123 uses these options for its score image and three audio versions, retaining
the available HTML headings and excluding video and ZIP downloads.
`content_from_images`, `image_section_selector` and `image_section_title_selector`
enable named OCR sections on image-only static pages. `linked_pages` routes use
`selection_selector`, optionally `choice_name_selector` and
`choice_number_selector`, to collect a specific list of verified sibling pages.
If its structure requires another parser, implement that handler under
`collectors/` and dispatch it from the relevant category module. Shared behavior
belongs in `common/`. Discovery results in `_discovery/`
are evidence for review, not automatic crawl targets. Reusing an existing parser
does not require a new test file; follow `scripts/AGENTS.md` for regression tests.

```text
scripts/crawlers/main/
  pages.json                  # collection configuration
  routes.py                   # validated routes and compatibility constants
  paths.py                    # category and file-format storage paths
  run.py                      # registered-route entry point
  categories/                 # top-level category entry points and registry
    student_life.py
    community.py
    university_intro.py
    registry.py
  common/                     # HTTP, storage, PDF/attachments, board parsing, dispatch
  collectors/                 # static pages, boards, files and specialized collectors
  discovery/                  # inventory, catalog and coverage inspection
  migrate_storage.py          # legacy data relocation and path rewriting
  student_life_stats.py        # saved document diagnostics
  update_priorities.py        # RAG priority maintenance
```
