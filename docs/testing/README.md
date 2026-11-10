# Test layout

pytest 실행 시 임시 파일은 루트 `conftest.py`가 선택한 `var/pytest/run-<고유 ID>/`에 생성합니다.
Windows의 사용자 Temp 아래 `pytest-of-Server` 접근 오류를 피하고, 다른 실행의 임시 폴더를
재사용하지 않기 위한 설정입니다. `--basetemp`를 직접 지정하면 그 경로가 우선합니다.
실행별 폴더는 검사 후에도 남을 수 있으며 `var/`는 Git에서 제외됩니다.

평가 채점기의 회귀 테스트는 프로젝트 루트에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest eval/tests/test_quality_baseline.py eval/tests/test_grade_eval_results.py
```

아래 디렉터리는 [`scripts/tests/`](../../scripts/tests) 기준입니다.

Tests are grouped by the feature they verify:

| Directory | Purpose |
| --- | --- |
| `core/` | Shared crawler schema, storage, logging, and script layout |
| `departments/` | Department discovery, adapters, registry, and transport |
| `main/` | Main-site routes, collectors, and student-life pages |
| `media/` | Body images, OCR, layout classification, and review |
| `fixtures/` | Shared offline inputs for department tests |

`_paths.py` defines the repository root and fixture directory once, so test paths stay valid when modules move.

## When to add a test

Extend an existing parser or collector test when a new HTML shape, failure mode, or data-loss regression needs protection. A new page ID or department using an existing parser is covered by configuration validation and a sample crawl review; it does not need a new test file. Avoid assertions against the current number or order of registered pages and sections.

Reusable validation belongs in crawler configuration, preflight, and parser code. Test-only mock responses and fixtures stay here. The rules for future changes are also recorded in `scripts/AGENTS.md`.

From the repository root:

```powershell
python -m unittest discover -s scripts/tests -t . -p "test_*.py"
```
