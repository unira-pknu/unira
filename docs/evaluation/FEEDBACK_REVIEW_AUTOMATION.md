# ② 검수·데이터 변환 자동화

`automate_feedback_review`는 실제 피드백과 AI 합성 질문의 검수 큐를 구성하고,
승인·출처·중복·데이터 분리 검사를 통과한 항목만 학습·평가 JSONL로 변환합니다.
사람의 승인·정답·출처 기록을 임의로 바꾸지 않으며 원본 파일, DB, 모델을 변경하지 않습니다.
사실성·개인정보·문맥 확인은 사람의 검수 단계로 유지합니다.

## 실행

기존 ① 생성 폴더에는 `review.jsonl`, `synthetic_candidates.jsonl`, `evidence.jsonl`이 있어야 합니다.
`--reviewed`에는 이미 검수한 파일을 지정합니다. 처음 큐를 만들 때는 생략할 수 있고,
실제 피드백과 합성 질문을 별도 파일에서 검수했다면 이 옵션을 여러 번 지정할 수 있습니다.
같은 ID를 여러 파일에 넣어 검수 결과가 충돌하면 전체 실행을 중단합니다.

```powershell
Set-Location "D:\Github\campus-rag\campus-rag"

.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_review `
  --base-url http://localhost:8000 `
  --reviewed "$env:USERPROFILE\Downloads\reviewed.jsonl" `
  --generation-dir var\feedback\ai_20261114_212453 `
  --output-dir var\feedback\stage2_live_v1
```

`backend/.env`의 `FEEDBACK_API_TOKEN`을 읽어 관리자 API에서 현재 싫어요를 조회합니다.
기존과 같은 HTTPS·관리자 인증을 사용하고 토큰을 결과에 저장하지 않습니다.
401이면 새 관리자 로그인 토큰으로 갱신한 뒤 실행합니다. API 실패 때 오프라인으로 자동 전환하지 않습니다.
기존 출력 폴더는 덮어쓰지 않으므로 실행마다 새 폴더 이름을 사용합니다.

생성 폴더를 생략하면 실제 피드백만 처리할 수 있습니다. 합성 질문의 승인 기록에는 생성 원본과
부모·근거를 대조할 자료가 필요하므로 생성 폴더 없이 합성 질문을 승격할 수 없습니다.

## 출력

| 파일 | 용도 |
| --- | --- |
| `review_queue.jsonl` | 전체 실제 피드백과 합성 질문의 통합 검수 큐. 원래 승인 상태와 별도로 자동 검사 상태를 기록 |
| `needs_review.jsonl` | 추가 검수 또는 보류 사유 확인이 필요한 항목 |
| `ready.jsonl` | 이번 실행에서 모든 검사를 통과한 승인 항목과 출처·부모 정보 |
| `train.jsonl` | 검사를 통과한 학습용 질문·정답의 `messages` 형식 |
| `evaluation.jsonl` | 검사를 통과한 평가용 사례. 기존 평가·채점 도구 형식 |
| `decisions.jsonl` | 모든 항목의 ready / blocked / pending / rejected 판정, 사유, 연관 그룹·겹친 평가 문항 |
| `report.md` | 사람이 읽는 처리 결과와 보류 사유 |
| `summary.json` | 건수, 최신 대조 여부, 입력·평가 파일 해시, 모델·DB 쓰기 호출 수 |
| `current_candidates.jsonl` | 이번 검사의 현재 피드백 스냅샷 |
| `evidence.jsonl` | 검수 UI에서 연결할 생성 시점 근거 |

변환 가능한 항목이 하나도 없으면 검수 큐와 보고서만 만들고 `train.jsonl`, `evaluation.jsonl`은
만들지 않습니다. 일부 승인 항목이 보류되어도 나머지 독립된 정상 항목은 변환합니다.
보류 항목을 묵묵히 버리지 않고 전체 항목 수와 ID를 보고서에 남깁니다.
`summary.json`의 `dataset_built`, `decisions`, `needs_review`를 함께 확인합니다.

## 합성 질문 검수와 재실행

1. [HTML 검수 편집기](../../eval/tools/feedback_review_editor.html)를 브라우저로 엽니다.
2. 2단계 출력의 `review_queue.jsonl` 또는 `needs_review.jsonl`을 선택합니다.
3. 같은 출력 폴더의 `evidence.jsonl`을 연결합니다. 합성 질문은 부모 피드백의 근거와 연결됩니다.
4. 실제 피드백과 합성 질문을 구분해 검수합니다. 수정 답변·검증 출처·학습/평가 구분·검수자·확인 항목을 채웁니다.
5. 검수본을 저장하고 그 파일을 `--reviewed`로 지정해 새 출력 폴더에 다시 실행합니다.
   전체 큐를 검수했다면 그 검수본 하나만 지정합니다. 전체 큐와 이전 원본 검수본을 동시에 넣으면
   ID가 중복되므로 함께 지정하지 않습니다.

생성기의 원래 `synthetic_candidates.jsonl`은 불변 원본으로 보관합니다. 합성 질문의 초안 답변과
필수 사실을 검수 칸에 준비하지만 승인이나 출처 검증 완료를 자동으로 표시하지 않습니다.
편집기는 2단계에서 만든 합성 질문 패키지를 지원하며 원래 생성 파일을 직접 승인 입력으로 쓰지 않습니다.

합성 질문에는 실제 싫어요 rating을 만들지 않습니다. 부모 피드백, 원래 질문 키, 생성 후보 해시,
부모 원본 해시를 보존합니다. 승인 후 생성 원본이나 부모가 변경·취소·삭제되면 그대로 재사용하지 못합니다.
기존 `build_feedback_training`·`prepare_feedback_training` 명령은 계속 실제 피드백 전용이며,
합성 질문의 변환에는 이 2단계 명령을 사용해야 합니다.

## 자동 검사 기준

- 실제 피드백: ID·수정 시각·원래 질문 키·기존 답변·출처·사유·의견을 현재 서버 값과 대조합니다.
- 합성 질문: 생성 원본과 패키지의 불변 필드를 대조하고 부모 피드백이 생성 당시와 일치하는지 확인합니다.
- 승인 요건: 정답, 공식 검증 URL, 검수자·시간대 포함 시각, 개인정보·맥락 확인, split을 검사합니다.
- 합성 근거: 부모 질문 키, 인용 ID, 실제 본문의 정확한 인용과 저장된 본문 해시를 확인합니다.
  검증 URL은 저장된 근거에 있어야 하고, 제안 인용 중 하나 이상을 직접 확인해야 합니다.
  추가 문서를 검수했다면 해당 문서의 본문도 근거 파일에 준비해야 합니다.
- 중복·분리: 원래/수정 질문, 문서 URL, 부모 질문으로 연결된 **승인 항목 전체 그룹**을 검사합니다.
  같은 그룹이 학습·평가 양쪽에 배정되거나 보호 평가에 겹치면 그룹의 승인 항목 전체를 보류합니다.
  중복 승인 질문도 임의로 하나를 선택하지 않고 검수자에게 돌려보냅니다.
- 보호 평가: 기본 smoke/regression/challenge와 `feedback_graduation_20261115.jsonl`을 유지하며
  `--holdout <경로>`로 추가할 수 있습니다. 출처가 맞아도 보호 평가에 속한 자료는 학습에 쓰지 않습니다.

검수 UI의 승인과 자동 변환 가능 상태는 별개입니다. 예를 들어 사람의 승인 기록은 `approved`로
보존하면서 자동 검사 상태는 `blocked`로 남길 수 있습니다. 확인해야 할 내용은
`review_pipeline` 또는 `decisions.jsonl`의 한글 사유와 원래 reason 코드에 기록합니다.
문서의 의미적 동일성이나 정답의 모든 주장을 자동 증명하지는 않습니다.

## 오프라인 검사

로그인 토큰 갱신 전 또는 재현 검사에는 명시적으로 저장된 원본 스냅샷을 사용할 수 있습니다.
네트워크 요청 없이 변환하며, 결과의 `freshness_checked=false`를 반드시 확인합니다.
오프라인 결과는 현재 서버에서 취소·변경된 피드백을 확인한 최종 결과가 아닙니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.automate_feedback_review `
  --current-candidates var\feedback\raw_20261114_212453\candidates.jsonl `
  --reviewed "$env:USERPROFILE\Downloads\reviewed.jsonl" `
  --generation-dir var\feedback\ai_20261114_212453 `
  --output-dir var\feedback\stage2_offline_v1
```

## 2026-11-16 실행 확인

최초 실시간 실행은 관리자 API의 HTTP 401로 중단되어 결과 폴더를 만들지 않았습니다.
토큰 갱신 전 검사한 오프라인 결과는 `var/feedback/stage2_offline_20261116_02`에 보관했습니다.
이후 사용자가 로그인 토큰을 갱신한 뒤 실시간 실행을 완료했습니다.
최종 실시간 결과는 `var/feedback/stage2_live_20261116_01`이며 `freshness_checked=true`입니다.

| 항목 | 건수 |
| --- | ---: |
| 현재 서버에서 조회한 실제 피드백 | 13 |
| 합성 질문 | 1 |
| 학습용 변환 | 3 |
| 평가용 변환 | 0 |
| 기존 보호 평가 출처와 겹쳐 보류 | 2 |
| 검수 대기 | 9 (실제 피드백 8 + 합성 질문 1) |

출력 메시지가 승인 정답과 앞뒤 공백 정리 후 일치하는지, 14건 전체가 보고서에 있는지,
검수 원본 파일의 해시가 바뀌지 않았는지 확인했습니다. 최종 결과는 최신 서버 대조를 마쳤으며
오프라인 결과는 별도로 구분합니다. API 여러 페이지는 단일 트랜잭션 스냅샷은 아닙니다.
기존에 승인한 다섯 질문 외의 여덟 실제 피드백은 전체 수집 대상에 있었지만 검수하지 않은 항목입니다.
이번 단계의 모델 호출과 DB 쓰기는 모두 0회입니다.

## 검증 명령

현재 실행에서 Python 관련 테스트는 117개 통과했고 Node가 없는 환경의 HTML 데이터 테스트
26개는 건너뛰었습니다. 브라우저 파일 선택·다운로드와 실제 화면은 별도 수동 확인이 필요합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest `
  eval\tests\test_automate_feedback_review.py `
  eval\tests\test_feedback_training.py `
  eval\tests\test_prepare_feedback_training.py `
  eval\tests\test_generate_feedback_candidates.py `
  eval\tests\test_feedback_review_editor.py `
  eval\tests\test_eval_ask.py
```
