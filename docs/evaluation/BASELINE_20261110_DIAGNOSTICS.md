# 2026-11-10 첫 전체 실행 진단

> 이 문서는 최초 채점 오류의 과거 진단입니다. 이후 직접 측정 요청으로 44개 테스트와 재채점 결과 검증,
> 로컬 판정 보완, 기준점 저장을 완료했습니다. 현재 점수와 한계는 [실제 측정 결과](QUALITY_MEASUREMENT_20261110.md)를 확인하세요.

대상 실행: `baseline_20261110_034945`. 사용자가 실행한 로컬 결과를 읽어 확인한 기록입니다.
원본은 `eval/results/baseline_20261110_034945.jsonl`, 채점은 같은 이름의 `.graded.jsonl`,
요약은 `.graded.summary.json`입니다. 이 파일들은 Git에서 제외됩니다.

## 확인된 상태

| 항목 | 결과 |
| --- | --- |
| 전체 문항 / 원본 실행 완료 | 100문항 / 완료 |
| HTTP 200 및 원문 캡처 | 99문항 |
| API 실패 | 1문항: `unanswerable_010`, HTTP 429, 일일 100회 한도 소진 |
| 유효한 채점 | 25문항 |
| 채점 JSON 검증 실패 | 74문항 |
| 원본 해시와 실행 manifest·채점 provenance 일치 | 일치 |
| 채점 파일 해시와 summary 일치 | 일치 |
| 질문 세트 해시와 manifest 일치 | 일치 |

74건의 오류는 답변 인용문 불일치 47건, 인용 번호 불일치 25건,
facts에 허용되지 않는 `violated` 사용 2건입니다. LLM이 답변을 요약·재작성하거나
다른 문장의 인용 번호를 붙이면서 기존 검증기가 문항 전체를 제외했습니다.
이 오류는 원본 답변이 모두 오답이라는 뜻이 아닙니다.

`record_baseline`은 성공 응답의 채점 오류를 허용하지 않으므로 이 실행은 현재
기준점으로 저장할 수 없습니다. API 429 자체는 실제 시스템 실패로 보관할 수 있으며,
정답률의 전체 분모에 남습니다. 채점 오류를 0건으로 만들기 위해 API 실패를 삭제하지 않습니다.

## 현재 숫자의 해석

- 엄격 정답률 9%: 확인된 정답 9 / 전체 100. 채점 오류 74건을 포함하므로 품질 확정치가 아닙니다.
- 채점된 문항만의 정답률 36%: 9 / 25. 채점 성공 집합으로 편향된 부분 점수입니다.
- 근거 정확도 0%: 기존 추출 주장 20개 중 supported 0, unsupported 16, 검증 불가 4.
  실제 사례에서 '졸업논문 제출 기간에 대한 정보는 확인되지 않습니다' 같은 거절 설명도
  무인용 사실 주장으로 집계됐습니다. 이 값을 전체 답변의 근거 정확도로 확정할 수 없습니다.
- 과도한 거절률 0%: 답변 가능 90문항 중 거절 판정은 16문항뿐입니다. 전체 거절률을 뜻하지 않습니다.
- 학과·시점 오류율 `null`: 해당 구분을 확인한 유효 분모가 0입니다. 오류가 없다는 뜻이 아닙니다.

## 수정과 재측정 범위

채점 계약 v2는 코드가 답변을 문장·줄 단위로 나누고 `u001` 같은 고정 ID와 실제 인용 번호를
부여합니다. LLM은 이 ID마다 판정하며, 답변이나 인용 번호를 다시 작성하지 않습니다.
제목·거절·확인 요청은 `not_factual`로 분리하고 거절 적절성은 별도로 측정합니다.
모든 ID의 누락·중복을 검사하며, 형식 오류에는 기본 한 번의 재요청만 허용합니다.
형식·근거 인용 검증을 완화해 기존 점수를 강제로 통과시키지 않습니다.

기존 원본 100행은 그대로 두고 새 채점 파일을 생성합니다. 서버·DB·답변 생성을 재실행할
필요가 없습니다. 429 한 건은 원래 실행의 실패로 유지합니다. 정상 100응답을 새로 측정하려면
일일 한도에 여유가 있는 평가 계정으로 별도 전체 실행을 수행하고 서로 다른 실행으로 보관해야 합니다.

```powershell
cd D:\Github\campus-rag\campus-rag

# 오프라인 회귀 테스트
.\.venv\Scripts\python.exe -m pytest eval/tests/test_quality_baseline.py eval/tests/test_grade_eval_results.py
if ($LASTEXITCODE -ne 0) { throw "테스트 실패" }

$rawPath = "eval/results/baseline_20261110_034945.jsonl"
$gradedPath = "eval/results/baseline_20261110_034945.regraded_v2.jsonl"

# 선택 사항: 전체 재채점 전에 기존 첫 답변 하나로 새 채점 형식 확인
# .\.venv\Scripts\python.exe -m eval.tools.grade_eval_results --results $rawPath --output eval/results/regrade_v2_smoke.jsonl --limit 1 --model gpt-4o-mini --timeout 60 --schema-retries 1

# 기존 답변과 출처를 새 계약으로 재채점; OpenAI 채점 호출만 수행
.\.venv\Scripts\python.exe -m eval.tools.grade_eval_results --results $rawPath --output $gradedPath --model gpt-4o-mini --timeout 60 --schema-retries 1
if ($LASTEXITCODE -ne 0) { throw "채점 미완료: 새 summary의 judge_error_reasons를 확인하세요." }

# 원본의 API 실패 한 건을 포함한 실행 기준점 저장
.\.venv\Scripts\python.exe -m eval.tools.record_baseline --questions eval/cases/regression.jsonl --results $rawPath --graded $gradedPath --output eval/baselines/baseline_20261110_034945.regraded_v2.json
if ($LASTEXITCODE -ne 0) { throw "기준점 저장 실패: 표시된 사유를 확인하세요." }

$report = Get-Content eval/results/baseline_20261110_034945.regraded_v2.summary.json -Raw -Encoding utf8 | ConvertFrom-Json
$report.answer_accuracy
$report.quality | ConvertTo-Json -Depth 8
$report.judge_error_reasons | ConvertTo-Json -Depth 5
```

이 진단·수정 중 에이전트는 테스트, API 요청, 재채점, DB 조회, 기준점 저장 명령을 실행하지
않았습니다. 새 채점 결과는 아직 없습니다. 채점 계약이 달라졌으므로 이전의 9%·0%와
새 숫자의 차이를 챗봇 자체의 품질 개선으로 해석하지 않습니다.
