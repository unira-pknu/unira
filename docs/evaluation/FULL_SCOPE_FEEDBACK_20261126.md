# 11월 25일 전체 범위 평가의 피드백 반영

`full_scope_novel_20261125_workspace100`에서 통과하지 못한 30문항 중 28문항의 답을 수집 원본과 전처리 문서에서 확인했다. 타인의 장학금 수혜 내역 조회와 직원의 실시간 근무 여부를 묻는 2문항은 수집 범위 밖이어서 이번 작업에서 제외했다.

| 범위 내 미통과 영역 | 문항 수 |
|---|---:|
| 공지 첨부자료 | 7 |
| 본교·학사 공지 | 7 |
| 캠퍼스·생활 | 7 |
| 학사·장학 | 4 |
| 교수진·연구실 | 3 |

[공식 근거 데이터](../../backend/app/reviewed_campus_sources.json)에 26건을 추가하고 기존 2건을 보완했다. 정답을 확인한 본문을 요약해 공식 문서 URL, 수집 경로, 파일 해시와 함께 기록했다. 첨부 ZIP은 아카이브와 이용한 내부 문서의 해시를 각각 남겼다.

- 멘토링 자기소개서의 글자 수를 묻는 표현을 인식한다. 공식 서식의 분량을 묻는 경우 해당 첨부를 검색 범위에 포함하고, 개인 신청 자격에는 기존 대상 조건을 적용한다.
- 학점교류와 워털루형 코업을 각각 교류와 현장실습 문맥으로 인식한다. 첨부 자료를 인용할 때 파일명과 해당 ZIP 내부 문서명을 모델에 전달한다.
- 확인된 교수 상세 프로필 중 4,000자 이하의 본문을 보존한다. 검토 근거가 있으면 교수의 학과와 질문 범위를 확인한 후 검색 결과에 함께 포함한다.
- 연도·월·회차가 있는 자료는 해당 조건을 근거 선택 기준에 포함한다. 질문과 시점이 일치하는 원문을 인용하므로 9월 창업보육센터 모집 조건을 다른 달의 공고로 답하는 사례를 줄일 수 있다.

이번 반영은 수집한 공식 원문을 RAG 검색 근거로 사용하는 방식이다. 학습 데이터 추출기의 기존 평가 세트 제외 설정을 유지했다. 이 문항들의 재실행은 수정 확인에 사용하고, 이후 일반화 평가는 새로운 문항에서 확인한다.

## 재검증

저장소 루트에서 아래 명령으로 단위 테스트와 수집 범위 내 미통과 28문항 평가를 실행한다. 평가 실행은 `backend/.env`의 조회 설정과 모델 API를 사용한다.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests eval/tests/test_feedback_training.py -q

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$review = Get-Content -Raw -Encoding utf8 'eval/results/full_scope_novel_20261125_workspace100.assessment.json' | ConvertFrom-Json
$caseArgs = @($review.cases | Where-Object { $_.answerable -and $_.assessment -ne 'pass' } | ForEach-Object { '--case-id'; $_.id })
.\.venv\Scripts\python.exe -m eval.tools.eval_local --questions eval/cases/full_scope_20261125_novel100.jsonl @caseArgs --output "eval/results/full_scope_novel_in_scope28_$stamp.jsonl" --workers 3 --max-calls 110 --model gpt-6-luna
```

같은 기준으로 답변의 사실과 인용한 원문을 확인한다. 28문항 재검증 후 원래 통과 항목의 회귀는 전체 100문항을 한 번 더 실행해 확인한다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.eval_local --questions eval/cases/full_scope_20261125_novel100.jsonl --output "eval/results/full_scope_novel_regression100_$stamp.jsonl" --workers 3 --max-calls 300 --model gpt-6-luna
```

## 운영 후보에서 남은 3문항

- 교수·자녀 수강 지침은 해당 규정의 제목으로 직접 검색한다. 필수 교과목 분반 지도 질문에는 제3조를 우선한다. 연도가 지정된 판본과 사전신고 질문은 각각의 근거를 확인한다.
- 교수 상세 페이지는 탭 링크가 가리키는 본문에 탭 제목을 보존한다. 김정수 교수의 주연구분야는 저장된 HTML에서 확인해 기존 수집분에도 적용할 RAG 근거로 등록했다.
- 정규 등록금의 학기·과정·계열·학생 구분이 확인되면 수업료와 총액을 한 질의에서 조회한다. 운영에서 이 경로를 사용하려면 질의 계획, 행 식별, 검색 후보 확장, 답변 발췌 코드를 함께 반영해야 한다.

아래 명령을 저장소 루트에서 실행해 수집기와 백엔드 회귀 테스트를 확인한 뒤 세 문항을 재평가한다. 배포를 준비할 때는 운영 검색 설정을 반영한 후보에서도 동일 문항을 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest scripts/tests/departments/test_numeric_cms.py backend/tests/test_retrieval_parallel.py backend/tests/test_evidence_context.py backend/tests/test_query_plan.py -q

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
.\.venv\Scripts\python.exe -m eval.tools.eval_local --questions eval/cases/full_scope_20261125_novel100.jsonl --case-id novel_rule_008 --case-id novel_faculty_005 --case-id novel_academic_011 --output "eval/results/full_scope_residual3_$stamp.jsonl" --workers 1 --max-calls 18 --model gpt-6-luna --data-snapshot workspace-configured-db
```
