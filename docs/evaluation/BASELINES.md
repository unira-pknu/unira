# Baselines

기준점 파일 저장 위치는 [`eval/baselines/`](../../eval/baselines)입니다.

2026-11-10 전체 100문항의 [측정 기준점](../../eval/baselines/baseline_20261110_034945.measured_final_v2.json)을 저장했습니다.
엄격 정답률 16%, 필수 사실 충족 점수 35.56%, 엄격한 문장별 근거 충족률 4.80%, 라벨 기준 과도한 거절률 4.44%입니다.
API 실패 1건은 전체 분모에 남습니다. 사용자 실행의 자동 채점에 Codex의 전체 판정 7건과 특정 필드 검토 6건을 합쳤으며,
독립적인 사람의 전수 검수 결과는 아닙니다. 학과·시점 오류율은 낮은 판정 커버리지 때문에 전체 품질을 대표하지 못합니다.

분모·검토 방식·측정 오류 수정·실행 이력·미적용 개선 과제는 [실제 측정 결과](QUALITY_MEASUREMENT_20261110.md)에 있습니다.
최초 미완료 채점의 9%·0%는 [첫 실행 진단](BASELINE_20261110_DIAGNOSTICS.md)에 기록한 과거 결과입니다.
DB의 실제 스냅샷은 확보하지 않았으며, 캡처한 응답·검색 청크·설정·해시를 기준점으로 보존합니다.

## 측정 기준

| 지표 | 분자 / 분모 | 해석 |
| --- | --- | --- |
| `answer_accuracy.strict_rate_all_labeled` | 필수 사실 모두 covered, 금지 주장 위반·미검증 없음, 기대 답변/거절 행동 충족 / 전체 라벨 문항 | API·채점 실패도 전체 분모에 남는 엄격한 정답률. 근거 정확도와 별도 |
| `fact_coverage_score` | covered + partial × 0.5 / 유효하게 채점한 필수 사실 | 일부 사실만 다룬 답변을 부분 점수로 반영. partial은 완전 정답이 아님 |
| `quality.claims.citation_accuracy` | supported / supported + unsupported + contradicted | 실제 인용한 청크 본문이 대상·단위·조건까지 뒷받침하는 사실 답변 단위 비율. 무인용 사실 주장도 unsupported |
| `quality.claims.evaluation_coverage` | 판정 가능 주장 / 추출한 전체 사실 주장 | 검증 불가 제외로 근거 정확도가 높아 보이는지 함께 확인 |
| `quality.claims.supported_fraction_all_claims` | supported / 검증 불가를 포함한 전체 사실 주장 | 검증 불가를 성공으로 인정하지 않는 보수적 보조 지표 |
| `quality.over_refusal.rate` | 라벨상 답변 가능한데 핵심을 포기한 문항 / 답변 가능 문항 중 거절 판정 가능한 문항 | 검색 실패로 생긴 거절도 포함. 기대 행동에 맞는 확인 질문과 정당한 부분 답변은 과도한 거절이 아님 |
| `quality.over_refusal.with_sufficient_retrieved_evidence` | 회수 자료로도 답변 가능했는데 거절한 문항 ID | 과도한 거절 중 생성 단계 개선이 필요한 사례 |
| `quality.department.error_rate` | 학과·전공·캠퍼스·대상 오류 / 해당 구분을 판정 가능한 문항 | `not_applicable`, `unverifiable` 수를 별도 표시 |
| `quality.temporal.error_rate` | 학기·연도·입학연도·시행일·현재성 오류 / 시점을 판정 가능한 문항 | 과거 예시임을 밝힌 값이나 오래되어도 유효한 규정은 자동 오류 처리하지 않음 |

분모가 없으면 `null`이며 0점이나 만점으로 해석하지 않습니다. `by_category`는 영역별 근거·거절·범위 지표를 제공합니다. API 오류, 채점 오류, 생략 문항 ID와 평가 커버리지를 항상 함께 봅니다.

채점 계약 v2는 코드가 답변을 문장·줄 단위로 나누어 고정 ID와 실제 인용 번호를 부여하고, LLM이 모든 ID를 한 번씩 판정하도록 합니다. 제목·거절·확인 요청은 `not_factual`로 분리해 사실 근거 정확도의 분모에서 제외하며 `non_factual_units`에 개수를 남깁니다. 인용 번호는 같은 단위에서만 추출하고 다른 문장의 번호를 추정해 붙이지 않습니다. 한 단위에 여러 사실이 있으면 모두 뒷받침될 때만 supported입니다.

LLM의 사실/비사실 분류와 의미 판정에는 오판 가능성이 있습니다. 학과·시점 판정에는 답변 단위와 출처 본문 인용을 요구하며, 실제 문자열이 확인되지 않으면 검증 불가로 바꿉니다. 금지 주장 재확인도 기존처럼 수행합니다. 오류 사례 전체와 정상 판정 사례 일부를 사람이 검수한 뒤 비교 기준으로 확정하세요.

## 출처 캡처 범위

`eval_ask`는 JSON `/ask` 요청에 `include_evidence=true`를 보냅니다. 수정된 백엔드를 실행해야 합니다. 일반 화면의 `sources.content`는 기존 발췌를 유지하고, `evaluation_evidence`에 다음을 별도로 보존합니다.

- 인용 번호와 같은 순서의 **검색 청크 원문 전체**(`content`), 모델에 표시한 구간(`shown_content`), 원문 해시
- source/chunk ID, RPC, 원본 데이터셋, 학과·날짜 metadata(해당 값이 검색 결과에 존재하는 경우)
- 질문 계획, KST 캡처 시각, 모델·검색 설정, 시스템 프롬프트 해시
- 생성 답변의 실제 자료 블록(`prompt_source_block`); 직접 생성한 공지 목록 등은 별도로 `direct` 표시

원문 문서 전체나 평가 시점의 DB를 다시 조회하지 않습니다. 인용 정확도는 캡처한 청크 범위의 검증이며 Recall@k·MRR을 의미하지 않습니다. `content`에는 있지만 `shown_content`에는 없는 정보도 원문 근거와 생성 컨텍스트를 구분해 검토할 수 있습니다. 출처가 짧은 발췌뿐인 과거 결과는 그대로 재채점할 수 있으나, 본문 부재를 근거 없음으로 단정하지 않으며 전체 기준점으로 저장할 수 없습니다. 스트리밍 캡처는 지원하지 않습니다.

## 실행 순서

프로젝트 루트에서 실행합니다. 변경된 백엔드가 실행 중이어야 하며, 배포 주소라면 이 변경이 적용된 버전을 먼저 사용해야 합니다. 설치·서버 실행·배포는 이 평가 명령이 대신 수행하지 않습니다. `ASK_API_TOKEN`은 로그인한 평가 계정의 access token입니다.

DB 스냅샷 식별자에는 실제 평가 대상 데이터의 적재 버전/백업 식별자를 입력하세요. 도구가 DB를 조회하거나 스냅샷을 만들지는 않습니다. 백엔드 버전에는 **평가할 서버**의 버전을 입력합니다. 로컬 Git commit은 원격 서버 버전의 증거가 아닙니다. 미커밋 로컬 변경은 실행 manifest의 파일 해시와 Git 상태로 함께 기록됩니다.

확인된 데이터 버전이 없으면 `live-db-20261110-unversioned`처럼 미확인 상태를 드러내는 평가용 라벨을 사용할 수 있습니다. 이 라벨은 실제 DB 스냅샷이나 동일 데이터 상태의 재현을 보장하지 않습니다.

```powershell
$env:ASK_API_TOKEN = Read-Host "평가 계정 access token"
$dataSnapshot = Read-Host "평가 대상 DB의 스냅샷 또는 적재 버전 식별자"
$backendRevision = Read-Host "평가할 백엔드의 코드 버전 식별자"
$runName = "baseline_20261110_run01"
$resultsPath = "eval/results/$runName.jsonl"
$gradedPath = "eval/results/$runName.graded.jsonl"

# 전체 100문항: --limit를 사용하지 않습니다.
.\.venv\Scripts\python.exe -m eval.tools.eval_ask --base-url http://localhost:8000 --questions eval/cases/regression.jsonl --output $resultsPath --timeout 120 --sleep 3.2 --data-snapshot $dataSnapshot --backend-revision $backendRevision

# 실제 출처 본문을 포함한 LLM 채점: OPENAI_API_KEY는 backend/.env 또는 환경 변수 사용
.\.venv\Scripts\python.exe -m eval.tools.grade_eval_results --results $resultsPath --output $gradedPath --model gpt-4o-mini --timeout 60 --schema-retries 1
if ($LASTEXITCODE -ne 0) { throw "채점 미완료: summary의 judge_error_reasons를 확인하세요." }

# 전체 실행·출처·채점 완결성과 해시 확인 후 기준점 보관
.\.venv\Scripts\python.exe -m eval.tools.record_baseline --questions eval/cases/regression.jsonl --results $resultsPath --graded $gradedPath --output "eval/baselines/$runName.json"
```

동일 조건에서 재측정할 때는 `run02`처럼 새 이름을 사용합니다. 원본 결과와 채점 JSONL, manifest, summary를 함께 보관하세요. `eval/results/`는 Git에서 제외되므로 별도 보관이 필요합니다. 기준점 JSON에는 점수·문항 ID·설정·해시와 로컬 결과 경로를 저장하며, 답변·출처 본문은 원본 JSONL에 남습니다. 기준점 파일은 기존 파일을 덮어쓰지 않습니다.

manifest에는 질문 세트 해시, 전체/선택 문항 수, 실행 시각, 대상 서버 주소(인증정보 제외), 데이터 버전, 백엔드 버전, 로컬 코드 해시가 기록됩니다. summary와 기준점에는 실제 백엔드 설정과 시스템 프롬프트 해시, judge 모델·프롬프트 해시 및 원본/채점 파일 해시가 기록됩니다. `record_baseline`은 누락/중복/부분 실행, 변경된 파일, 실행 중 백엔드 설정 변경, 캡처 없는 성공 응답, 채점 오류가 있으면 저장을 거부합니다. API 실패는 실제 시스템 실패로서 기록하고 엄격 정답률 분모에 남깁니다. 구버전 백엔드가 원문 캡처를 반환하지 않으면 평가 실행도 조기에 중단합니다.

채점 형식 오류에는 `--schema-retries`만큼만 재요청하며 기본값은 1입니다. 원문 인용을 임의로 고쳐 성공 처리하지 않습니다. 오류가 남으면 채점 명령은 종료 코드 2를 반환하고, summary의 `judge_error_reasons`와 `schema_repaired_case_ids`로 실패·수정 이력을 확인할 수 있습니다. 채점 결과는 문항마다 파일에 기록합니다. 재채점은 새 출력 파일을 지정하며 새 계약·코드·프롬프트 해시를 보관합니다. 기존 서버 응답의 해시는 바뀌지 않습니다.

초기 구현 단계에서는 실행을 사용자에게 맡겼으며, 이후 사용자의 직접 측정 요청에 따라 기존 결과 검증·로컬 검토·기준점 저장과
관련 오프라인 테스트 44개를 실행했습니다. DB 조회·새 응답 생성·배포·빌드는 수행하지 않았습니다.
아래는 더 넓은 검증 범위의 명령이며, 이번 직접 측정에서는 이 전체 묶음을 실행하지 않았습니다.

```powershell
.\.venv\Scripts\python.exe -m pytest eval/tests/test_eval_ask.py eval/tests/test_grade_eval_results.py eval/tests/test_quality_baseline.py backend/tests/test_evaluation_evidence.py backend/tests/test_ask_json.py backend/tests/test_question_flow.py
```
