# Scripts layout

메인 홈페이지·학과·단과대학·학칙/규정을 포함한 전체 실행 방법은
[전체 크롤러 실행 가이드](../crawlers/README.md)를 참고하세요.
학과·단과대학의 실행 및 수집 대상 관리는 [학과 크롤러 README](../crawlers/DEPARTMENTS.md)에 정리되어 있습니다.

`scripts` is organized by responsibility. New code should import and execute the canonical paths below.

Evaluation commands and datasets live under `eval/`; see the [evaluation guide](../evaluation/README.md).
Main-site page routes and discovery tools live under `scripts/crawlers/main/`; see [메인 홈페이지 크롤링 명령어](../crawlers/MAIN.md) for full, category, page and limited-board runs.
Main-site collection settings are in `scripts/crawlers/main/pages.json`. Its results share
`files/pknu_main/`, organized by site category and then file format. Incremental
state is in `_state/`; RAG intermediates are in `_derived/<logical-dataset>/`.

College, faculty, department, and major websites use the common department
crawler. Their explicit `storage_path` in `crawlers/departments/registry.json`
places each dataset under `files/department/<college>/<faculty-or-department>/<major>/<dataset>/`.
Levels without a separate organization/site are omitted; a website shared by
multiple majors stays at the faculty level and is stored once. Independent
faculties use their own name as the first level. The existing dataset alias
(for example, `ce`) remains the leaf folder and document identity is preserved.
Each dataset keeps `output/{json,html,files,images,runs}` and `state.json` inside that folder.
Derived data lives centrally in `preprocessed/<dataset>/` and `vectorized/<dataset>/`.

```text
files/department/
├─ 공과대학/
│  ├─ coe/                                 # college website
│  ├─ 화학공학과/chemeng/
│  └─ 기계공학부/기계학전공/me/
└─ 정보융합대학/컴퓨터·인공지능공학부/ce/    # shared faculty website
```

Existing department directories can be reorganized without crawling:
`python -m scripts.migrations.crawler_storage --department-layout` previews
the moves; add `--apply` to move them and update local JSON/JSONL references.
The command refuses destination collisions and records its verification in
`files/department/_migration/`. Legacy flat folders remain readable for older
checkouts that have not yet been migrated.

```text
scripts/
├─ crawlers/
│  ├─ common/
│  │  ├─ schema.py       # document, attachment, stats, and run-result contracts
│  │  ├─ reader.py       # schema-version reader and legacy fallback
│  │  ├─ storage.py      # dataset paths and atomic state persistence
│  │  └─ logging.py      # console and JSONL structured logging
│  ├─ departments/
│  │  ├─ config.py       # DepartmentConfig, SectionConfig, registry validation
│  │  ├─ engine.py       # shared PKNU department CMS parser/downloader
│  │  ├─ sites.csv       # source catalog of colleges, departments, majors, and homepages
│  │  └─ registry.json   # crawl-ready datasets and verified sections
│  ├─ main/              # main-site routes, collectors, configuration, and discovery
│  ├─ syllabus/          # syllabus catalog and PDF collection
│  ├─ pknu_notice.py
│  ├─ pknu_student_life.py
│  └─ pknu_rule.py
├─ migrations/
│  ├─ crawler_documents.py
│  ├─ crawler_storage.py
│  └─ main_storage.py
├─ preprocessing/        # text cleaning, chunking, OCR review, and preprocessing CLIs
│  └─ extractors/        # attachment/file format extractors
├─ vectorization/        # embeddings, vector files, DB ingestion/search, and pipelines
├─ priority/             # document priority scoring and DB update commands
│  ├─ department/        # shared department priorities
│  ├─ rule/              # university rule priorities
│  └─ rag/               # main-site, notice and syllabus priorities
└─ tests/
```

Priority command options and examples are in [scripts/priority/README.md](../../scripts/priority/README.md).

Tests are grouped by the feature they verify:

```text
scripts/tests/
  core/          # shared crawler contracts, storage, logging, repository layout
  departments/   # department discovery, CMS adapters, registry and transport
  main/          # main-site routes and student-life page collectors
  media/         # body images, OCR, layout classification and review
  fixtures/      # shared offline test inputs
  _paths.py      # stable project and fixture paths
```

Run all tests from the repository root with:

```powershell
python -m unittest discover -s scripts/tests -t . -p "test_*.py"
```

Canonical crawler commands:

```powershell
python scripts/crawlers/departments/engine.py --dataset ce --once
python scripts/crawlers/pknu_notice.py --recent-only 1
python scripts/crawlers/pknu_student_life.py --mode guide --limit 1
python scripts/crawlers/pknu_rule.py --laws --max-law-items 1
```

The former crawler and root-level common-module paths have been removed. Internal code, jobs, and documentation must use the canonical paths above.

`sites.csv` is the address catalog and may contain sites that are not yet crawler-compatible. `registry.json` contains only configurations with verified menu paths and board IDs. A registry entry links back to the source catalog through `source_catalog_key`.

Department catalog and discovery commands:

```powershell
python scripts/crawlers/departments/cli.py validate
python scripts/crawlers/departments/cli.py probe --site-key ce.pknu.ac.kr
python scripts/crawlers/departments/cli.py discover --site-key ce.pknu.ac.kr
python scripts/crawlers/departments/cli.py discover --dataset ce
```

`probe` performs a shallow homepage fingerprint check. `discover` first requires a compatible probe, then inspects same-host numeric menu pages and writes candidates under `files/_discovery/<site_key>/`. Discovery output is never merged into `registry.json` automatically.

Layout-aware OCR RAG evaluation (isolated test store, never touches Supabase):

```powershell
python -m scripts.vectorization.ingest_layout_data            # var/reviewed/body_image_ocr/*.reviewed.jsonl -> var/test_db/test_layout_rag_YYYYMMDD/
python -m eval.tools.generate_testset               # <= 50 QA pairs -> eval/results/layout_eval_dataset.csv (--mode rule: no API cost)
python -m eval.tools.run_evaluation                 # -> eval/results/layout_eval_report.md (+ .json) (--retrieval-only: no API cost)
```

Tables are chunked as markdown (header repeated per chunk), diagrams are rewritten as sentences, and every chunk keeps `provenance` (source path, image hash, review/extraction origin). Test store names must match `test_layout_rag_YYYYMMDD[_suffix]`. LLM calls go through `eval/tools/llm_budget.py`: at most 3 attempts per call, a hard per-run request cap, and no retry on 400/401/403/404. `scripts/tests/fixtures/layout_rag/` holds a synthetic fixture with the same schema for smoke runs.
