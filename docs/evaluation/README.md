# Evaluation datasets

아래 데이터 경로(`cases/`, `baselines/`, `results/` 등)는 코드·데이터 폴더
[`eval/`](../../eval) 기준입니다. `baselines/`는 기준점 보관을 위한 계획 경로이며, 현재 별도 기준점 관리 문서는 없습니다.

Evaluation commands are in `eval/tools/` and run from the repository root with
`python -m eval.tools.<command>`.

평가 케이스는 목적에 따라 분리합니다.

- `cases/smoke.jsonl`: 빠른 확인용 핵심 질문 8~15개. 작은 변경 후 실행합니다.
- `cases/regression.jsonl`: 출처와 정답 라벨 검수를 마친 100개 질문의 회귀 평가 세트입니다. 모든 행이 `ready` 상태입니다.
- `cases/challenge.jsonl`: 범위 밖, 애매한 표현, 오타, 복합 조건처럼 실패 가능성이 높은 질문만 집중 평가합니다. 회귀 세트와 일부 중복될 수 있습니다.
- `schemas/eval_case.schema.json`: 구조화 평가 케이스의 JSON Schema입니다.
- `baselines/`: 코드와 모델 및 RAG 설정별 기준 점수를 보관합니다.
- `results/`: 로컬 평가 실행 결과를 저장합니다. 생성 결과는 Git에 커밋하지 않습니다.
- `drafts/question_bank_100.jsonl`: 유형 균형을 맞춘 100개 질문 초안입니다. 정답 출처와 사실 라벨을 검수하기 전에는 실행용 회귀 세트로 사용하지 않습니다.

## 다섯 테이블 분리 이후의 호환성

`018_split_dataset_tables.sql`은 출처·청크 ID, 본문, 원본 벡터, URL과 원본 데이터셋 metadata를 보존합니다.
기존 질문·정답 문장을 테이블명 변경만으로 다시 만들 필요는 없습니다. 다만 검색 후보 범위가 달라졌으므로
기존 평가 점수와 동일하다고 가정하지 말고, 새 DB 구조와 백엔드 버전에서 기준 점수를 다시 측정해야 합니다.

| 검색 대상 | 출처 / 청크 테이블 | RPC |
|---|---|---|
| 학과·단과대 | `rag_sources` / `rag_chunks` | `match_rag_documents` |
| 본교 공지·첨부 | `pknu_notice_sources` / `pknu_notice_chunks` | `match_pknu_notice_documents` |
| 본교 공지 외·대학생활 | `pknu_main_sources` / `pknu_main_chunks` | `match_pknu_main_documents` |
| 규정집 | `rule_sources` / `rule_chunks` | `match_rule_documents` |
| 강의계획서 | `pknu_syllabus_sources` / `pknu_syllabus_chunks` | `match_pknu_syllabus_documents` |

`eval_ask`와 LLM 채점기는 `/ask` 응답을 사용하며 물리 테이블명을 직접 참조하지 않습니다.
백엔드의 `RPC_NAMES` 환경 변수도 위 다섯 RPC로 설정되어 있어야 합니다.
로컬 layout 평가는 별도의 `test_layout_rag_*` 저장소를 사용하므로 운영 DB 분리와 독립적입니다.
`search_smoke`는 이동된 `scripts/vectorization/query_supabase.py`를 호출합니다.
이 도구의 기본 데이터셋은 `ce`이므로 다섯 분류 전체의 평가를 대체하지 않습니다.

향후 학습·평가 체계에서 구분해야 할 사항:

- 물리 테이블 계열, 원본 `metadata.source_dataset`, 적재 ID의 기반인 `metadata.ingestion_dataset`은 서로 다른 값입니다.
  예를 들어 본교 공지는 테이블 계열이 `pknu_notice`, 원본 데이터셋이 `pknu_notice`, 적재 데이터셋이 `pknu_main`일 수 있습니다.
  `pknu_student_life` 원본 라벨은 자료가 `pknu_main_*`으로 이동해도 유효합니다. 이를 일괄 치환하면 원본 분류를 잃습니다.
- 현재 `expected_source.dataset`은 smoke에서는 `rule`·`ce` 같은 수집 데이터셋 별칭을,
  regression에서는 `학칙`·`학과공지` 같은 사람이 작성한 분류를 사용합니다.
  향후 데이터셋 일치율을 자동 채점하기 전에 라벨 의미를 통일하고 기존 라벨을 출처와 대조해 검수해야 합니다.
  `ready`의 현재 구조 검사는 DB 출처 일치나 검색 정확도 검사가 아닙니다.
- 현재 `/ask.sources`에는 `source_id`, `chunk_id`, 데이터셋 및 학과 식별자가 포함되지 않습니다.
  `expected_source` 조건도 현재 채점기에서 자동 판정하지 않습니다. 분류별 Recall@k·MRR 및 학과별 정확도를
  측정하려면 검색 결과의 ID·계열·원본 데이터셋·`department_id`를 평가 경로에 전달하는 기능이 필요합니다.
- 현재 smoke 8문항과 regression 100문항에는 강의계획서용 명시적 분류가 없습니다.
  공지 외 본교 자료 및 여러 학과를 포함한 분류별 평가 질문을 출처 검수 후 보강해야 합니다.
- 현재 repo에는 별도 모델 파인튜닝 실행 파이프라인이 없습니다. 향후 학습셋을 만들 때는 이관 후 출처 ID를
  검증하고 같은 출처의 여러 청크가 학습·평가 양쪽에 섞이지 않도록 출처 단위로 분리해야 합니다.

평가 도구의 단위 테스트와 라벨 형식 검사만으로 실제 검색·답변 품질을 판단할 수는 없습니다.
새 구조의 기준 점수는 백엔드 배포 및 출처 라벨 검수 후 별도로 측정합니다.

## 실행 예시

```powershell
# 빠른 확인
.\.venv\Scripts\python.exe -m eval.tools.eval_ask --questions eval\cases\smoke.jsonl --output eval\results\smoke.jsonl

# 빠른 smoke 평가 (기본값)
.\.venv\Scripts\python.exe -m eval.tools.eval_ask

# 실패 경계 집중 평가
.\.venv\Scripts\python.exe -m eval.tools.eval_ask --questions eval\cases\challenge.jsonl --output eval\results\challenge.jsonl
```

기본 평가 입력은 빠른 확인용 `smoke.jsonl`입니다. 전체 평가에는 검수를 마친
`regression.jsonl`을 지정합니다.

## 100개 질문 초안

질문 초안은 `draft_question.schema.json` 형식이며, `case_type`,
`scenario_tags`, `difficulty`, `label_status`, `origin`을 포함합니다.
`origin=existing`은 기존 regression 질문과 정확히 일치하는 질문이고,
`origin=new`는 유형 균형을 위해 추가한 질문입니다.

```powershell
# 원본 정의로 질문 뱅크 재생성
.\.venv\Scripts\python.exe -m eval.tools.build_eval_question_bank

# 수량, 유형 분포, 중복, 필수 시나리오 검사
.\.venv\Scripts\python.exe -m eval.tools.audit_eval_question_bank
```

라벨 검수 시 각 행을 `eval_case.schema.json` 형식으로 확장한 뒤
`label_status`를 `needs_review → source_verified → label_verified → ready`
순서로 변경합니다. `needs_review`인 행은 정확도 평가의 정답 데이터로 간주하지 않습니다.

## Regression 라벨 입력

각 행에서 다음 필드를 채웁니다.

- `answerable`: 답변 가능 여부
- `expected_source`: 예상 데이터셋, 제목 키워드, URL 또는 source ID
- `required_facts`: 반드시 포함되어야 하는 사실
- `forbidden_claims`: 생성하면 안 되는 주장
- `expected_no_info`: `answerable`의 반대 값
- `tags`: 검색과 답변 특성
- `expected_behavior`: 사람이 확인할 정답 행동
- `review_notes`: 출처 확인 기록

답변 불가 질문은 `expected_source`를 `null`로 설정합니다. 답변 가능 질문은
출처 조건 중 하나 이상을 반드시 채워야 합니다.

```powershell
# 현재 라벨 검수 진행률과 ready 행의 유효성 확인
.\.venv\Scripts\python.exe -m eval.tools.audit_regression_labels

# 질문 뱅크로부터 빈 서식을 다시 만들기(기존 입력 내용이 사라짐)
.\.venv\Scripts\python.exe -m eval.tools.build_regression_template
```

두 번째 명령은 `regression.jsonl`을 초기화하므로 최초 생성 또는 명시적인
재초기화가 필요할 때만 사용합니다.

## Layout-aware OCR RAG evaluation

이 평가는 격리된 로컬 테스트 저장소를 사용하며 Supabase를 변경하지 않습니다.

```powershell
python -m scripts.vectorization.ingest_layout_data      # var/reviewed/body_image_ocr/*.reviewed.jsonl -> var/test_db/test_layout_rag_YYYYMMDD/
python -m eval.tools.generate_testset         # 최대 50개 QA 쌍 -> eval/results/layout_eval_dataset.csv (--mode rule: API 비용 없음)
python -m eval.tools.run_evaluation           # eval/results/layout_eval_report.md (+ .json) (--retrieval-only: API 비용 없음)
```

표는 각 청크에 헤더를 반복하는 Markdown으로 나누고, 다이어그램은 문장으로 바꿉니다.
모든 청크는 원본 경로, 이미지 해시, 검수 및 추출 출처를 `provenance`에 보관합니다.
테스트 저장소 이름은 `test_layout_rag_YYYYMMDD[_suffix]` 형식을 사용합니다.
`eval/tools/llm_budget.py`는 호출당 최대 3회 시도, 실행별 요청 수 상한,
400/401/403/404 응답 재시도 금지를 적용합니다.
`scripts/tests/fixtures/layout_rag/`에 같은 스키마의 smoke 테스트 입력이 있습니다.
