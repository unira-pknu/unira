# 이후 학습 진행 계획

작성 기준: 2026-11-16, 프로젝트 루트 `D:\Github\campus-rag\campus-rag`.
이 문서는 다음 작업의 순서와 완료 조건을 설명합니다.
세부 옵션·재개·복구 방법은 [③ 학습·비교 평가 자동화](FEEDBACK_TRAINING_AUTOMATION.md)를 참고합니다.
아래 명령은 사용자가 단계별로 실행할 예시입니다. 후속 확인에서 테스트 87개는 통과했으며,
실제 학습·검색 캡처·비교 평가는 실행하지 않았습니다.

## 1. 현재 위치와 첫 목표

마지막으로 확인한 결과는 `var/feedback/stage2_live_20261116_01`입니다.
현재 서버를 새로 조회한 결과가 아니라, 이전에 확인한 실행 결과를 기준으로 합니다.

| 항목 | 마지막 확인 상태 | 다음 작업 |
| --- | --- | --- |
| 실제 피드백 / 합성 질문 | 13건 / 1건 | 추가 피드백을 모으고 기존 대기 항목 검수 |
| 학습용 변환 | 3건 | 승인과 출처 본문 검사를 통과하는 서로 다른 예제 확보 |
| 신규 평가용 변환 | 0건 | 기존 보호 평가 유지, 추가 평가는 학습 전에 분리 |
| 보호 평가와 겹친 승인 항목 | 2건 보류 | 기존 분리 기준 유지 |
| 검수 대기 | 실제 8건 + 합성 1건 | 공식 자료와 대화 맥락을 확인해 승인 또는 반려 |
| ③ 학습·비교 자동화 | 관련 테스트 87개 통과, 실계정 실행 검증 전 | 대기 항목 검수와 데이터·근거 보강 |
| 실제 학습 / 비교 점수 | 이번 자동화로 실행하지 않음 | 조건 충족 후 소규모 실험부터 수행 |

첫 목표는 **출처 본문까지 준비된 학습 예제 10건 이상으로 제출 조건을 확인하는 것**입니다.
10건은 제공자의 최소 조건이며 충분한 품질을 보장하는 수량은 아닙니다.
공식 안내는 우선 잘 만든 예제 50건으로 평가할 것을 권장합니다.
따라서 먼저 소규모로 절차를 확인하고, 성과가 있으면 50건 수준으로 늘리는 방향을 권합니다.
[OpenAI 공식 SFT 안내](https://developers.openai.com/api/docs/guides/supervised-fine-tuning).

## 2. 비용을 쓰기 전에 확인할 사항

선택한 범위는 **이용 가능한 기존 OpenAI 파인튜닝 계정 전용**입니다.
계정이 실제로 작업을 생성할 수 있는지, 선택할 원본 모델이 학습·추론에 지원되는지 먼저 확인합니다.
계정 자격이나 지원 모델은 로컬 코드 검증만으로 확인되지 않습니다.

2026-11-16 확인한 공식 일정에는 신규 학습 작업 생성 종료일이 2027-01-06으로 안내되어 있고,
원본 모델이 먼저 종료되면 그 일정이 우선합니다. GPT-4.1/4.1-mini 및 해당 학습 모델의 추론 종료도
2027-01-14로 안내됩니다. 이번 파인튜닝 실험과 장기 서비스 운영 계획은 따로 판단하고,
데이터·평가 자료를 보존해 이후에도 활용할 수 있게 합니다. 제공자 변경은 현재 구현 범위에 포함하지 않습니다.
[공식 종료 일정](https://developers.openai.com/api/docs/deprecations).

2026-11-16 아래 테스트를 실행해 87개 통과를 확인했습니다. 코드를 변경한 뒤에는 같은 명령으로
다시 확인합니다. 실제 API나 학습 작업을 호출하지 않으며, 실패하면 원인을 해결한 뒤 유료 단계로 진행합니다.

```powershell
Set-Location D:\Github\campus-rag\campus-rag

.\.venv\Scripts\python.exe -m pytest `
  eval\tests\test_automate_feedback_training.py `
  eval\tests\test_automate_feedback_review.py `
  eval\tests\test_grade_eval_results.py `
  eval\tests\test_quality_baseline.py `
  backend\tests\test_generation.py
```

## 3. 추가 데이터와 근거 확보

질문을 단순히 늘리기 전에 실패 원인을 나눕니다.

| 관찰한 문제 | 먼저 할 일 | 학습 예제로 쓸 때 확인할 것 |
| --- | --- | --- |
| 공식 문서가 없거나 검색되지 않음 | 수집·본문 추출·검색 상태 점검 | 문서 확보 후 정답을 검수 |
| 필요한 근거가 있는데 잘못 해석함 | 조건·수치·인용 오류 분석 | 같은 근거에서 올바르게 답하는 예제 |
| 학과·학번·학기 조건을 섞음 | 적용 대상과 날짜를 명시 | 서로 다른 대상의 조건을 구분한 정답 |
| 답할 수 있는데 전체를 거절함 | 근거의 충분성과 누락 범위 확인 | 확인 가능한 부분과 미확인 부분을 나눈 정답 |
| 질문이 모호하거나 정보가 없음 | 추가 질문/답변 불가가 타당한지 확인 | 추정하지 않고 필요한 조건만 묻는 정답 |

실제 피드백을 우선하고 합성 질문은 부족한 실패 유형을 보완하는 데 사용합니다.
동일 질문의 말바꾸기를 대량 생성하거나 원본 오답을 정답처럼 사용하지 않습니다.
이미 맞는 정답의 형식·인용만 다듬는 경우에도 원래 질문의 적용 범위를 유지합니다.

**중요:** ②에서 승인·변환되어도 ③에 필요한 출처 본문이 없으면 학습 제출은 보류됩니다.
기존 ① 결과가 일부 피드백만 포함한다면 나머지 피드백의 근거도 새로 확보해야 합니다.
승인 숫자만 보고 학습 가능한 건수라고 판단하지 않습니다.

추가 수집과 후보 생성이 필요하면 새 차수 폴더를 사용합니다. 이 예시는 한 PowerShell 세션에서 이어서 실행합니다.

```powershell
$roundKey = Get-Date -Format yyyyMMdd_HHmmss
$backendUrl = 'http://localhost:8000'  # 실제 실행 중인 백엔드 주소
$rawDir = "var\feedback\raw_$roundKey"
$aiDir = "var\feedback\ai_$roundKey"

# 관리자 API 읽기. backend/.env의 FEEDBACK_API_TOKEN 사용
.\.venv\Scripts\python.exe -m eval.tools.prepare_feedback_training `
  --base-url $backendUrl --output-dir $rawDir
if ($LASTEXITCODE -ne 0) { throw '피드백 수집 결과를 먼저 확인하세요.' }

# 처리 ID·제외 사유·호출 한도만 확인. 이 명령은 모델·백엔드를 호출하지 않음
.\.venv\Scripts\python.exe -m eval.tools.generate_feedback_candidates `
  --input "$rawDir\candidates.jsonl" --base-url $backendUrl --output-dir $aiDir `
  --holdout eval\cases\feedback_graduation_20261115.jsonl `
  --limit 10 --variants 1 --max-calls 10 --dry-run
```

처리 목록과 예상 요청 수를 확인한 뒤에만 실제 생성을 실행합니다. **아래부터 모델 비용이 발생합니다.**
`--limit 10`은 원본 피드백 처리 상한이며 최종 학습 10건을 보장하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.generate_feedback_candidates `
  --input "$rawDir\candidates.jsonl" --base-url $backendUrl --output-dir $aiDir `
  --holdout eval\cases\feedback_graduation_20261115.jsonl `
  --limit 10 --variants 1 --max-calls 10
if ($LASTEXITCODE -ne 0) { throw '후보 생성 결과와 호출 기록을 확인하세요.' }
```

이 호출 상한은 초안 생성기 요청에 적용됩니다. `/ask` 내부의 검색 계획·답변 생성 호출은 별도입니다.
생성 후 `summary.json`, `events.jsonl`, `evidence.jsonl`에서 근거가 실제로 확보되었는지 확인합니다.
더 작은 시범 실행은 위 두 생성 명령의 `--limit`와 `--max-calls`를 함께 줄입니다.
소규모 생성 폴더 여러 개의 누적 병합은 현재 CLI가 자동으로 수행하지 않으므로,
최종 변환 차수에는 사용할 부모·합성 원본·근거가 갖춰진 하나의 `--generation-dir`이 필요합니다.

## 4. 전체 검수본을 유지하며 ② 재실행

새 생성 폴더로 큐를 만들 때는 이전 **전체 큐 검수본**을 함께 전달합니다.
기존 3건을 보존하려면 `needs_review.jsonl`만 따로 수정해 이전 전체 검수본을 대체하지 않습니다.
기존 합성 질문은 생성 원본과 연결되지 않으면 보류될 수 있으므로 사유를 확인합니다.

```powershell
$previousReviewed = Read-Host '이전 전체 검수본 JSONL의 실제 전체 경로'
$queueDir = "var\feedback\queue_$roundKey"

.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_review `
  --base-url $backendUrl --reviewed $previousReviewed `
  --generation-dir $aiDir --output-dir $queueDir
if ($LASTEXITCODE -ne 0) { throw '검수 큐 생성 결과를 확인하세요.' }
```

[HTML 검수 편집기](../../eval/tools/feedback_review_editor.html)에서 `$queueDir\review_queue.jsonl`과
같은 폴더의 `evidence.jsonl`을 엽니다. AI 초안과 원본 문서를 대조한 뒤 다음을 채웁니다.

- 질문과 수정 정답, 실제 확인한 공식 URL, 필요한 사실과 금지 주장.
- 학습/평가 배정, 검수자·검수 시각, 개인정보 및 맥락 확인.
- 합성 질문은 부모 근거의 인용문, 학과·연도·조건의 일치 여부.

전체 큐를 새 검수본으로 저장하고, 그 파일 하나로 다시 변환합니다.
동일 ID가 든 이전 검수본을 동시에 전달하면 중복으로 중단되므로 함께 넣지 않습니다.

```powershell
$reviewedPath = Read-Host '방금 저장한 전체 검수본 JSONL의 실제 전체 경로'
$datasetDir = "var\feedback\stage2_$roundKey"

.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_review `
  --base-url $backendUrl --reviewed $reviewedPath `
  --generation-dir $aiDir --output-dir $datasetDir
if ($LASTEXITCODE -ne 0) { throw '데이터 변환 결과를 확인하세요.' }

Get-Content "$datasetDir\report.md" -Encoding UTF8
```

완료 조건은 `freshness_checked=true`, 승인·분리 검사 통과, 보류 사유 확인입니다.
학습·평가 분리는 질문·URL·부모 질문으로 연결된 그룹 단위로 유지합니다.
보호된 졸업요건 두 질문을 학습에 넣거나 분리 기준을 완화해 10건을 채우지 않습니다.
독립적인 최종 평가를 추가한다면 학습 전에 정하고 ①·②·③에 동일 파일을 `--holdout`으로 전달합니다.

## 5. 소규모 학습과 비교 준비

비용과 작동 확인은 우선 **검수된 회귀 케이스 5문항**으로 진행할 수 있습니다.
현재 `smoke.jsonl`에는 ③이 요구하는 `label_status=ready`가 없으므로 그대로 사용하지 않습니다.
아래는 기존 회귀 세트의 처음 5행을 새 파일로 복사하며, 정답 라벨이나 보호 평가 원본을 수정하지 않습니다.

```powershell
$pilotCases = "var\feedback\comparison_pilot_$roundKey.jsonl"
$pilotRows = @(Get-Content eval\cases\regression.jsonl -Encoding UTF8 | Select-Object -First 5)
[System.IO.File]::WriteAllLines(
  (Join-Path (Get-Location).Path $pilotCases), [string[]]$pilotRows,
  [System.Text.UTF8Encoding]::new($false)
)

$trainBaseModel = Read-Host '계정에서 지원하는 학습 원본 모델 ID'
$baselineModel = Read-Host '현재 운영 OPENAI_MODEL의 모델 ID'
$judgeModel = Read-Host '고정 심사 모델 ID (후보와 다른 모델)'
$pilotDir = "var\feedback\training_pilot_$roundKey"

.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_training prepare `
  --dataset-dir $datasetDir --run-dir $pilotDir `
  --base-model $trainBaseModel --baseline-model $baselineModel --judge-model $judgeModel `
  --cases $pilotCases --epochs 3
if ($LASTEXITCODE -ne 0) { throw '실험 준비 오류를 확인하세요.' }

Get-Content "$pilotDir\preflight.md" -Encoding UTF8
```

`prepare`는 오프라인입니다. **학습 보류 사유가 없고 실제 업로드 예제가 10건 이상인지** 확인합니다.
`minimum_training_examples`, `missing_training_evidence`, `verified_source_body_missing` 등이 있으면
데이터·근거 확보 단계로 돌아갑니다. `prepare` 성공 종료만으로 제출 가능하다고 해석하지 않습니다.
5문항 시범 평가는 기본 최소 평가 건수 20에 미달하므로 `hold`가 정상입니다.
이 시범 결과만으로 운영 모델을 바꾸지 않습니다.

## 6. 비용 확인 후 학습 → 상태 확인 → 비교

실제 업로드 입력은 `$pilotDir\train.jsonl`입니다. 질문·정답 외에 시스템 프롬프트와 출처 본문도
포함되므로 원래 검수 파일 크기만으로 토큰을 추정하지 않습니다.
선택한 모델의 실제 단가와 예상 학습 토큰·epochs, 생성·심사·캡처 비용을 합쳐 예상 지출을 정합니다.
호출 수 제한은 달러 기준의 결제 한도가 아닙니다. 비용 계산식과 별도 과금 범위는
[③ 비용 설명](FEEDBACK_TRAINING_AUTOMATION.md#보고서와-판정-해석)을 참고합니다.

첫 실행은 비용 발생 시점을 확인하기 쉽도록 단계별 명령을 권합니다.

```powershell
# 여기서 학습 작업 생성 및 학습 비용 발생
.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_training submit `
  --run-dir $pilotDir --eligible-account
if ($LASTEXITCODE -ne 0) { throw '재제출 전 state.json과 복구 절차를 확인하세요.' }

.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_training status `
  --run-dir $pilotDir --max-polls 5 --poll-seconds 15
if ($LASTEXITCODE -ne 0) { throw '학습 상태 조회 오류를 확인하세요.' }
```

진행 중이면 `status` 명령만 나중에 다시 실행합니다. 성공 후 다음 단계로 진행합니다.
응답을 잃은 제출 요청은 자동 재제출하지 않고 [자동화 안내](FEEDBACK_TRAINING_AUTOMATION.md)의 중단·재개와 복구 절차를 따릅니다.

```powershell
$pilotState = Get-Content "$pilotDir\state.json" -Encoding UTF8 | ConvertFrom-Json
if ($pilotState.job_status -ne 'succeeded') { throw '학습 성공 후 진행하세요.' }
$candidateModel = $pilotState.trained_model
$dataSnapshot = Read-Host '실제 DB 적재 버전 식별자 (모르면 미확인임을 드러내는 이름)'

# 검색 캡처 및 그 백엔드 내부 모델 호출 비용 발생
.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_training capture `
  --run-dir $pilotDir --base-url $backendUrl --data-snapshot $dataSnapshot
if ($LASTEXITCODE -ne 0) { throw '검색 자료 캡처 상태를 확인하세요.' }

# 두 모델 생성·채점 비용 발생
.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_training compare --run-dir $pilotDir
Get-Content "$pilotDir\report.md" -Encoding UTF8
```

확인할 것은 동일 프롬프트 해시, 정상적인 인용·정답 판정, 사용 토큰, 오류·제외 사유입니다.
제외된 문항을 성공으로 세거나 `hold`를 감추기 위해 최소 평가 건수를 낮추지 않습니다.

## 7. 전체 비교와 운영 적용 판단

시범 결과가 정상적이면 **이미 학습한 후보를 재사용**해 새 전체 평가 실험을 준비합니다.
여기서는 추가 학습을 제출하지 않습니다. 기본 평가 범위는 회귀 100문항 + 졸업요건 2문항이며
②의 신규 평가용 데이터가 있으면 함께 포함됩니다.

```powershell
$fullDir = "var\feedback\training_full_$roundKey"
.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_training prepare `
  --dataset-dir $datasetDir --run-dir $fullDir `
  --base-model $trainBaseModel --baseline-model $baselineModel --judge-model $judgeModel
if ($LASTEXITCODE -ne 0) { throw '전체 평가 준비 상태를 확인하세요.' }

.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_training run `
  --run-dir $fullDir --base-url $backendUrl --data-snapshot $dataSnapshot `
  --candidate-model $candidateModel
Get-Content "$fullDir\report.md" -Encoding UTF8
```

전체 평가에서는 캡처·생성·채점 비용이 다시 발생합니다. 시범 5문항 캡처를 자동 재활용하지 않습니다.
동일 자료 비교는 생성 부분만 평가하므로 직접 답변·명확화 같은 다른 경로는 제외될 수 있습니다.
이 경우 전체 보고서는 불완전 평가로 보류될 수 있으며 모델 품질 실패와 구분해 사유를 봅니다.
단순히 제외 문항을 지워 기존 전체 회귀 점수와 같은 범위의 점수라고 보고하지 않습니다.

| 결과 | 다음 조치 |
| --- | --- |
| API/채점 오류 또는 제외 때문에 불완전 | 인증·모델 호환·검색 경로를 확인하고 필요한 별도 평가 수행 |
| 정상 완료했지만 개선 부족 | 실패 유형·프롬프트·정답 품질을 검토하고 데이터 보완 |
| 근거·금지 주장·학과/시점 오류 증가 | 적용 보류, 해당 사례를 분석 |
| `review_candidate` | 실제 개선·퇴보 답변을 사람이 검토하고 서비스 전체 평가 수행 |

서비스 전체 검증은 [기준점 평가 절차](BASELINES.md)를 사용합니다.
현재 모델과 후보 모델을 각각 설정한 격리된 백엔드에서 같은 질문·DB·설정으로 실행하고,
두 실행 사이에는 모델 외의 검색·프롬프트 설정을 바꾸지 않습니다.
기존 모델·프롬프트·설정과 보고서를 보관한 뒤 사람이 적용 여부를 결정합니다.
운영 적용과 재시작·배포는 현재 자동화가 수행하지 않으며, 문제가 있으면 기존 모델로 되돌립니다.

## 8. 반복 운영할 때 남길 기록

각 차수에서 전체 검수본, ① 생성 원본과 근거, ② 보고서, ③ 실험 폴더와 실제 과금 내역을 함께 보관합니다.
토큰·API 키는 기록에 넣지 않고 `backend/.env`에서만 관리합니다.
질문·근거가 포함된 로컬 결과는 Git 커밋 대신 접근을 제한한 별도 보관 위치를 사용합니다.

개선 실험은 한 번에 한 가지를 바꿉니다. 데이터 수정과 검색 변경을 동시에 했다면 별도 실험으로 구분합니다.
출력·원본·고정 케이스·실험 코드를 바꾼 뒤 같은 실험 폴더에서 재개하지 않습니다.
최종 평가를 반복해 보면서 정답을 맞추는 방식은 피하고, 실패 사례는 이후 차수의 개발 자료로 편입할지
결정한 뒤 독립적인 최종 평가를 새로 확보합니다.

지금 바로 진행할 순서는 **테스트 통과 확인(87개) → 대기 9건의 근거·검수 상태 확인 → 추가 데이터 확보 →
② 재변환 → ③ 오프라인 준비 보고서 확인**입니다. 이 단계까지 완료한 뒤 유료 학습을 시작합니다.
