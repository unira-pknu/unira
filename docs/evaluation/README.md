# Evaluation datasets

아래 데이터 경로(`cases/`, `baselines/`, `results/` 등)는 코드·데이터 폴더
[`eval/`](../../eval) 기준입니다. 전체 기준점 실행 절차와 지표 정의는 [BASELINES.md](BASELINES.md)에 있습니다. 실제 전체 평가를 실행하기 전에는 현재 정답률을 알 수 없습니다.

Evaluation commands are in `eval/tools/` and run from the repository root with
`python -m eval.tools.<command>`.

[전체 범위 100문항 개선 기록](FULL_SCOPE_FEEDBACK_20261124.md)에 11월 24일 평가의 실패 유형, 수정 내역과 재평가 명령을 정리했습니다.

## 로컬 실제 검색·답변 평가

`eval_local`은 `backend/.env`의 실제 임베딩·재순위화 모델, DB 검색, OpenAI 답변 생성을
사용하면서 `/ask` 처리 함수를 직접 호출합니다. DB 데이터는 변경하지 않습니다.
인증을 거친 HTTP 요청, 스트리밍 전송, 프런트엔드 동작까지 검증하는 평가는 아닙니다.
이 범위는 결과 manifest의 `execution_mode`와 `http_auth_tested`에도 기록됩니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.eval_local --questions eval/cases/regression.jsonl --per-category 1 --output eval/results/local_quality.jsonl --max-calls 35 --data-snapshot "확인한-적재-receipt-ID"
.\.venv\Scripts\python.exe -m eval.tools.grade_eval_results --results eval/results/local_quality.jsonl --model gpt-4o-mini --schema-retries 1
```

`--max-calls`는 질문 계획과 답변 교정 호출을 합친 OpenAI 요청 상한입니다. 기존 출력은 덮어쓰지
않으므로 재실행할 때 새 출력 경로를 사용합니다. `--case-id`를 반복 지정하면 특정 질문만
실행할 수 있습니다. 실제 검색 장애는 응답 성공과 별개로 `retrieval_status`와 근거 snapshot에서
확인합니다. 데이터 snapshot 이름은 적재 이력을 가리키는 표식이며 DB를 고정한 복제본은 아닙니다.
`--workers`는 동시에 평가할 질문 수입니다(1~4, 기본 1). 여러 질문을 실행해도 임베딩과 재순위화 모델은
각각 한 번만 로드합니다. 비교할 때는 같은 동시 실행 수를 사용합니다.
모델 설정이 바뀌는 동안 비교한다면 `--model gpt-4o-mini`처럼 모델을 고정합니다. 이 옵션은
`.env`를 변경하지 않으며 manifest의 `configuration_override`와 각 답변의 설정에 기록됩니다.
manifest의 `completions`에는 질문 ID별 실제 응답 모델, 종료 사유, 출력·추론 토큰 수가 기록됩니다.
`finish_reason=length`인 빈/부분 출력과 근거가 없다는 실제 답변을 구분할 때 사용합니다.
백엔드는 출력 한도에 걸린 첫 답변을 한 번만 확장된 예산으로 다시 생성하고 근거를 재검증합니다.
이 재생성은 기존 답변 교정 한도를 공유합니다. 두 번째 출력도 잘리거나 모델이 빈 출력을 반환하면
생성 오류로 처리하며, 이를 정보 부재 답변으로 바꾸지 않습니다. 명시적인 정보 부재 답변은 유지합니다.

프로그램 조회·셔틀 이용·실시간 정보의 부분 답변을 묶어서 확인하려면 다음과 같이 실행합니다.
출력 경로는 매번 새 이름을 지정하고, `--data-snapshot`에는 실제 적재 이력을 기록합니다.
검색 함수 변경은 [021 마이그레이션](../../supabase/migrations/021_lexical_late_hydration.sql)을 참고합니다.
자동 채점 모델은 평가 대상 답변 모델과 별도로 지정합니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.eval_local --questions eval/cases/regression.jsonl --case-id notice_attachment_001 --case-id student_life_001 --case-id partial_003 --output eval/results/targeted_quality_new.jsonl --max-calls 10 --data-snapshot "적재-이력-ID+021"
```
반복 수정에 사용한 질문의 점수를 독립 평가나 전체 정답률로 제시하지 않습니다. 자동 채점의
`judge_error`가 남으면 채점이 완료된 것으로 간주하지 않습니다.

실행 중인 백엔드와 유효한 로그인 토큰이 있다면 `ASK_API_TOKEN` 환경변수를 설정한 뒤
다음 명령으로 인증을 포함한 HTTP 경로를 별도로 확인합니다. 프런트엔드 검사는 Node/npm이
설치된 환경에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.eval_ask --base-url http://127.0.0.1:8000 --questions eval/cases/smoke.jsonl --output eval/results/http_smoke.jsonl
npm --prefix frontend run lint
```

다음 학습을 준비할 때는 [이후 학습 진행 계획](FEEDBACK_TRAINING_NEXT_STEPS.md)에서
현재 데이터 상태, 추가 근거·검수, 비용 확인, 시범 학습과 전체 평가 순서를 확인합니다.

평가 케이스는 목적에 따라 분리합니다.

- `cases/smoke.jsonl`: 빠른 확인용 핵심 질문 8~15개. 작은 변경 후 실행합니다.
- `cases/regression.jsonl`: 출처와 정답 라벨 검수를 마친 100개 질문의 회귀 평가 세트입니다. 모든 행이 `ready` 상태입니다.
- `cases/faculty_regression_20261124.jsonl`: 교수진 명단, 연락처, 연구분야, 상세 프로필, 소속·직함·확인 불가를 각각 20개씩 다룹니다. 답변 가능 94개와 공개 목록의 빈 필드·대상 확인 6개를 포함합니다. 라벨은 `review_notes`에 기록된 공식 페이지 수집본 기준입니다. 상세 프로필은 URL의 `action`과 `no` 값까지 대조해야 합니다.
- `cases/feedback_graduation_20261115.jsonl`: 졸업요건 피드백 두 질문의 별도 평가 세트입니다. [공식 자료 기반 라벨 수정 내역](GRADUATION_LABEL_REVIEW_20261115.md)을 참고합니다.
- `cases/challenge.jsonl`: 범위 밖, 애매한 표현, 오타, 복합 조건처럼 실패 가능성이 높은 질문만 집중 평가합니다. 회귀 세트와 일부 중복될 수 있습니다.
- `schemas/eval_case.schema.json`: 구조화 평가 케이스의 JSON Schema입니다.
- `baselines/`: 코드와 모델 및 RAG 설정별 기준 점수를 보관합니다.
- `results/`: 로컬 평가 실행 결과를 저장합니다. 생성 결과는 Git에 커밋하지 않습니다.
- `drafts/question_bank_100.jsonl`: 유형 균형을 맞춘 100개 질문 초안입니다. 정답 출처와 사실 라벨을 검수하기 전에는 실행용 회귀 세트로 사용하지 않습니다.

교수진 기준 실행에서 73/100건이 통과했습니다. 상세 프로필 실패 19건의 정답은 운영 DB의 활성 문서에 있었지만 검색 후보에 들어오지 않았습니다. 수정 후에는 같은 문항을 사용해 이름·학과가 일치하는 상세 출처를 인용하는지 확인합니다. 명단과 연락처도 함께 비교하고, 연구분야는 같은 사람의 이름과 분야가 답변 근거에 남는지 확인합니다. 앞선 대화에 인물이 없는 경우에는 대상 확인 질문만 반환해야 합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_faculty.py backend/tests/test_query_plan.py backend/tests/test_evidence_context.py backend/tests/test_syllabus.py -q
.\.venv\Scripts\python.exe -m eval.tools.eval_local --questions eval/cases/faculty_regression_20261124.jsonl --output eval/results/faculty_regression_recheck.jsonl --workers 3 --max-calls 300 --data-snapshot "확인한-적재-receipt-ID"
```

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
- 일반 `/ask.sources`는 화면용 발췌입니다. 평가 요청의 `include_evidence=true`는 별도
  `evaluation_evidence`에 검색 청크 원문과 존재하는 source/chunk ID, RPC, 데이터셋·학과 metadata를 전달합니다.
  채점기는 실제 인용 본문을 사용해 근거 정확도와 학과·시점 오류를 정답 체크리스트와 별개로 판정합니다.
  `expected_source`의 별칭 정규화와 ID 기준 Recall@k·MRR 계산은 아직 구현하지 않았습니다.
- 현재 smoke 8문항과 regression 100문항에는 강의계획서용 명시적 분류가 없습니다.
  공지 외 본교 자료 및 여러 학과를 포함한 분류별 평가 질문을 출처 검수 후 보강해야 합니다.
- 싫어요 피드백의 관리자 API CLI 수집·검수 결과 대조와 승인된 정답의 학습/평가 JSONL 변환은
  [피드백 학습 데이터](FEEDBACK_TRAINING.md) 절차를 사용합니다.
  [피드백 기반 AI 후보 생성](FEEDBACK_AI_CANDIDATES.md)은 검색 원문을 바탕으로 수정 답변과 관련 질문을
  검수 대기 초안으로 생성합니다.
  [② 검수·데이터 변환 자동화](FEEDBACK_REVIEW_AUTOMATION.md)는 합성 질문까지 포함한 검수 큐를 만들고
  승인·근거·분리 검사를 통과한 항목만 변환하며 보류 사유를 기록합니다.
  [③ 학습·비교 평가 자동화](FEEDBACK_TRAINING_AUTOMATION.md)는 승인 데이터 재검사, 선택적 OpenAI 학습 제출,
  상태 확인, 동일 검색 자료에서의 두 모델 비교와 보고서를 연결합니다. 현재 3건은 최소 학습 건수에 미달합니다.
  이관 후 출처 ID를 검증하고 같은 출처의 여러 청크가 학습·평가 양쪽에 섞이지 않도록 분리해야 합니다.

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
