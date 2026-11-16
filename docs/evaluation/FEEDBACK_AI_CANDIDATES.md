# 피드백 기반 AI 후보 생성

학습 자동화의 첫 단계로, 실제 싫어요 피드백에서 **수정 답변과 관련 질문·답변 후보**를 생성합니다.
기존 [피드백 수집·검수](FEEDBACK_TRAINING.md) 기능의 JSONL을 입력으로 사용합니다.
AI 결과는 모두 `pending`이며 모델 학습, 자동 승인, 운영 설정 변경, DB 쓰기는 수행하지 않습니다.

## 처리 흐름

1. 원본 후보의 질문 연결 상태를 확인하고 동일 질문, 기존 평가 질문, 처리 한도 밖 항목을 제외합니다.
2. 실행 중인 백엔드 `/ask`에 `include_evidence=true`로 질문해 검색 청크 본문을 가져옵니다.
   이 요청에서 생성되는 챗봇 답변은 근거로 사용하지 않습니다. 저장된 근거 JSONL도 사용할 수 있습니다.
3. 허용한 공식 도메인의 자료만 사용하고, 기존 평가 세트에 명시된 출처 URL은 제외합니다.
4. AI가 실패 유형과 짧은 설명, 원래 질문의 수정 답변, 관련 질문·답변을 생성합니다.
5. JSON 구조, 필수 사실, 인용 ID, 인용문의 원문 포함 여부를 검사하고 중복·평가 질문을 제외합니다.
6. 검수 파일, 합성 질문 후보, 실제 사용한 근거와 실행 기록을 새 폴더에 저장합니다.

실패 유형은 `missing_evidence`, `retrieval`, `reasoning`, `scope`, `formatting`, `unclear`,
`other`입니다. 이 분류 역시 AI의 제안입니다. 자료가 없거나 기존 대화 맥락을 확정하지 못하면
수정 답변을 보류합니다. 질문을 연결하지 못한 피드백에는 모델 요청을 보내지 않습니다.

## 사용자 실행 명령

프로젝트 루트에서 실행합니다. 아래 명령은 구현 과정에서 실행하지 않았습니다.
프로젝트 가상환경과 기존 백엔드 의존성을 사용하며 추가 패키지는 필요하지 않습니다.

먼저 처리 대상을 확인합니다. `--dry-run`은 백엔드·OpenAI 호출, 키 로딩, 출력 파일 작성을
하지 않습니다. 출력에는 후보 ID와 제외 사유, 요청 한도만 표시하고 질문 본문은 표시하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.generate_feedback_candidates `
  --input "$env:USERPROFILE\Downloads\feedback-review-2026-11-11T06-18-15-803Z.jsonl" `
  --base-url http://localhost:8000 `
  --output-dir var\feedback\ai_round1 `
  --limit 5 `
  --dry-run
```

실제 후보 생성은 `--dry-run`을 빼고 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.generate_feedback_candidates `
  --input "$env:USERPROFILE\Downloads\feedback-review-2026-11-11T06-18-15-803Z.jsonl" `
  --base-url http://localhost:8000 `
  --output-dir var\feedback\ai_round1 `
  --limit 5 `
  --variants 2 `
  --max-calls 10
```

백엔드는 실행 중이어야 합니다. 원격 백엔드는 HTTPS 주소 또는 localhost SSH 터널을 사용합니다.
현재 프론트엔드 `/api/ask` 프록시는 `include_evidence`를 전달하지 않으므로
**백엔드 주소를 직접 지정**합니다. `evaluation_evidence`가 없는 응답이나 검색이 부분 실패한
응답은 사용하지 않습니다. HTTP 401이면 로그인 토큰을 갱신하고, 429이면 호출 제한을 확인합니다.

백엔드 로그인 `access_token`은 `backend/.env`의 `FEEDBACK_API_TOKEN`에 저장할 수 있습니다.
수집 CLI와 AI 후보 생성 CLI가 모두 토큰 조회 전에 해당 파일을 자동 로드하므로 값 하나를
공유할 수 있습니다. 생성 CLI는 `ASK_API_TOKEN`이 있으면 먼저 사용하고 없으면
`FEEDBACK_API_TOKEN`을 사용합니다. 두 값이 모두 없으면 숨김 입력을 받습니다.
각 변수는 이미 설정한 프로세스 환경 변수가 `.env`보다 우선합니다. `--dry-run`에서는
`.env`를 로드하지 않습니다. 토큰에는 `Bearer ` 없이 실제 `access_token`만 넣고,
HTTP 401로 만료가 확인되면 새 로그인 세션의 값으로 교체합니다.
OpenAI 키와 모델도 기존 `backend/.env`의 `OPENAI_API_KEY`, `OPENAI_MODEL`을 사용합니다.
`--model`로 생성 모델만 바꿀 수 있습니다. 로그인 토큰과 OpenAI 키는 서로 다른 값이며 출력에
저장하지 않습니다. 생성 모델은 저장소의 기존 Chat Completions JSON 모드 호출 설정과 호환되어야 합니다.

`--input`에는 원본 다운로드 또는 `prepare_feedback_training`의 `candidates.jsonl`을 사용합니다.
이미 검수한 파일을 입력으로 재사용하면 작업 내용을 덮어쓸 수 있으므로 허용하지 않습니다.
검수 사본은 별도로 유지하고 매 실행마다 새로운 출력 폴더를 지정합니다.

## 출력과 검수

| 파일 | 내용 |
| --- | --- |
| `review.jsonl` | 처리한 원래 피드백과 AI 수정 초안. 기존 검수 파이프라인의 `--reviewed` 입력 |
| `review.md` | 질문·기존 답변·수정 답변·검수 상태·근거 인용을 구분한 보기용 Markdown 보고서 |
| `synthetic_candidates.jsonl` | AI가 새로 만든 관련 질문·답변. 부모 피드백 ID, 질문 묶음, 인용, 생성 모델 기록 |
| `evidence.jsonl` | 부모 피드백별 실제 사용한 본문, 출처 URL, source/chunk ID, 적용 메타데이터와 본문 해시 |
| `events.jsonl` | 질문 연결 실패, 한도·중복·평가 세트 제외, 근거 부족, 생성 오류 기록 |
| `summary.json` | 입력·프롬프트·근거·평가 파일 해시, 설정, 생성 건수, 생성기 요청·토큰 사용량 |

### HTML로 보기·수정하기

로컬 [피드백 검수 편집기](../../eval/tools/feedback_review_editor.html)를 브라우저로 열면
JSON 문법을 직접 수정하지 않고 질문·정답·출처·검수 정보를 편집할 수 있습니다.
서버 실행, 로그인, 패키지 설치 없이 HTML 파일을 열어서 사용합니다.

```powershell
# 프로젝트 루트에서 사용자가 실행: 기본 브라우저로 편집기 열기
Start-Process ".\eval\tools\feedback_review_editor.html"
```

1. **피드백 JSONL 열기**에서 실제 `review.jsonl`을 선택합니다.
2. **근거 JSONL 연결**에서 같은 실행 폴더의 `evidence.jsonl`을 선택합니다.
   브라우저는 파일을 선택해야 읽을 수 있으므로 같은 폴더에 있어도 자동으로 읽지는 않습니다.
3. 왼쪽 목록에서 질문·답변·의견·ID를 검색하거나 검수 상태로 필터링합니다.
4. 질문과 수정 답변, 확인한 출처 URL, 필수 사실, 금지 주장을 편집합니다. 목록은 한 줄에 하나씩 입력합니다.
5. 학습·평가 구분과 검수자 식별명을 입력하고 개인정보·맥락 확인을 마칩니다.
6. **검수 완료 · 승인**을 누릅니다. 누락된 정보는 화면에 표시하며 검수 시각은 한국 시간으로 기록합니다.
7. **검수본 JSONL 저장**을 누르고 실제 저장 여부를 확인한 뒤 **파일 저장 완료 확인**을 누릅니다.

검수본은 `reviewed.jsonl`이라는 이름으로 브라우저 다운로드에 전달됩니다. 원본 파일을 자동으로
덮어쓰지 않습니다. 브라우저가 저장한 실제 이름과 위치를 확인하고, 예를 들어 생성 폴더 안의
`reviewed.jsonl`로 보관한 뒤 기존 변환 명령의 `--reviewed`에 그 경로를 지정합니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.prepare_feedback_training `
  --base-url http://localhost:8000 `
  --reviewed var\feedback\ai_20261114_212453\reviewed.jsonl `
  --output-dir var\feedback\dataset_reviewed_v1
```

저장할 때 검색·필터에 숨겨진 항목과 보류·제외 항목도 모두 포함합니다. 원본 ID, 질문 키,
기존 오답, 피드백 수정 시각, 출처 정보와 알 수 없는 추가 필드는 보존합니다. 편집한 승인 항목은
검수 대기로 돌아가며, 질문·답변·근거 조건을 바꾸면 해당 확인 항목도 다시 확인해야 합니다.
잘못된 승인, 중복 승인 질문, 학습·평가 사이 동일 출처는 저장 전에 검사합니다. 기존 평가 세트와의
대조·최신 피드백 변경 확인은 Python 변환 명령이 최종 수행합니다.

선택한 파일과 편집 내용은 브라우저 메모리에서 처리하며 외부 전송·자동 저장을 하지 않습니다.
파일 내용을 HTML로 실행하지 않고 텍스트로 표시합니다. 새로고침·탭 닫기 전에 검수본을 저장하세요.
합성 질문은 [② 검수·데이터 변환 자동화](FEEDBACK_REVIEW_AUTOMATION.md)에서 만든
`review_queue.jsonl`로 불러와 검수할 수 있습니다. 원래 `synthetic_candidates.jsonl`은 생성 원본으로 보관합니다.

### Markdown으로 읽기

긴 JSONL을 읽기 어려우면 `review.md`를 열고 VS Code에서 `Ctrl+Shift+V`로 미리보기를 엽니다.
앞으로 후보를 생성하면 자동으로 함께 저장합니다. 기존 생성 폴더는 다음 명령으로 변환합니다.
같은 폴더의 `evidence.jsonl`이 있으면 근거 본문과 URL도 연결합니다. 외부 요청은 하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.render_feedback_review `
  --input var\feedback\ai_20261114_212453\review.jsonl
```

출력은 같은 폴더의 `review.md`입니다. 기존 보고서는 덮어쓰지 않으므로 다시 만들 때는
`--output var\feedback\ai_20261114_212453\review_v2.md`처럼 새 경로를 지정합니다.
별도 근거 파일은 `--evidence <경로>`로 지정할 수 있습니다. Markdown은 읽기용이므로 승인과
정답 수정은 `review.jsonl`에 반영해야 합니다. 보고서를 만든 뒤 원본을 바꿨다면 보고서도 새로 만듭니다.
보고서의 본문은 텍스트 블록으로 표시해 내용에 포함된 이미지·HTML·링크를 자동 실행하지 않습니다.

원래 피드백의 `ai_generation.correction`에서 제안한 질문·답변·필수 사실·금지 주장·인용문을
확인합니다. 수정 답변은 `review.corrected_answer`에도 채워집니다. 원래 질문 키, ID,
피드백 시각과 기존 오답 등은 보존하므로 기존 최신 피드백 대조와 연결할 수 있습니다.

모든 결과의 `review.status`는 `pending`, `split`은 `null`입니다. `verified_sources`는 빈 목록이며
검수자·검수 시각도 비어 있고 개인정보·맥락 확인은 `false`입니다. 인용 URL과 인용문이 있다는
이유만으로 검증 완료로 표시하지 않습니다. 생성 실패 항목은 빈 수정 답변과 상태를 남깁니다.
`ai_generation.status`는 `suggested`, `needs_context_or_evidence`, `no_allowed_evidence`,
`generation_failed` 중 하나입니다.

사람이 원문과 대상·조건·시점을 확인한 뒤 `verified_sources`, 검수자·시각, 개인정보·맥락 확인,
`split`, 승인 상태를 채웁니다. 상세 기준은 [정답 검수](FEEDBACK_TRAINING.md#2-정답-검수)를 따릅니다.
검수한 원래 피드백은 다음 명령으로 최신 상태와 대조한 뒤 학습·평가 JSONL로 변환합니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.prepare_feedback_training `
  --base-url http://localhost:8000 `
  --reviewed var\feedback\ai_round1\review.jsonl `
  --output-dir var\feedback\dataset_v1
```

합성 질문은 실제 싫어요 행과 구분해 저장합니다. 합성 후보의 검수·승격은
[② 검수·데이터 변환 자동화](FEEDBACK_REVIEW_AUTOMATION.md)의 명령과 통합 검수 큐를 사용합니다.
기존 `prepare_feedback_training` 명령은 실제 피드백 전용으로 유지합니다.
같은 부모와 출처로 연결된 승인 질문은 한쪽 split으로 묶으며, 충돌한 그룹은 변환에서 보류합니다.

## 저장된 근거로 재생성

이전 실행의 `evidence.jsonl`이나 직접 준비한 본문을 사용하면 백엔드 재호출 없이 생성할 수 있습니다.
**생성용 OpenAI 호출은 발생**합니다. 네트워크 호출을 전부 생략하려면 `--dry-run`을 사용합니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.generate_feedback_candidates `
  --input var\feedback\round1\candidates.jsonl `
  --evidence var\feedback\ai_round1\evidence.jsonl `
  --output-dir var\feedback\ai_round2 `
  --limit 5
```

근거 JSONL은 각 줄에 하나의 피드백과 근거 목록을 담습니다. `feedback_id`와
`original_question_key`는 입력 피드백의 값과 일치해야 합니다.

```json
{"feedback_id":"원본 피드백 UUID","original_question_key":"원본 질문의 SHA-256 키","sources":[{"uri":"https://www.pknu.ac.kr/실제문서경로","title":"문서 제목","content":"공식 자료에서 확인한 본문","metadata":{"year":2026,"department_name":"해당 학과"}}]}
```

누락된 피드백의 근거는 없는 것으로 처리합니다. URL만 있는 항목이나 과거 화면용
`answer_sources` 발췌는 자동으로 근거 목록에 넣지 않습니다. 도구는 URL을 방문하지 않으므로
준비한 본문의 진위와 최신 여부는 별도 확인해야 합니다.

## 범위와 한도

- 기본 10개, 최대 50개 피드백을 처리합니다. 관련 질문은 피드백당 기본 2개, 최대 5개입니다.
  수정 답변만 생성하려면 `--variants 0`을 지정합니다.
- 생성기 HTTP 요청은 기본 `2 × 선택한 피드백 수` 이내이며 재시도도 포함합니다.
  `--max-calls`로 1~150 사이의 한도를 지정할 수 있습니다. 논리 요청당 시도는 최대 2회입니다.
  **백엔드 `/ask` 내부의 질의 계획·검색·답변 생성 비용은 이 한도에 포함되지 않습니다.**
  백엔드 요청 수는 선택한 피드백당 최대 1회로 제한해 별도 기록합니다.
- `--max-tokens`는 생성 응답당 기본 2500, 범위 256~4096입니다. 출력이 잘리거나 거부되면 후보로
  사용하지 않습니다. `--timeout`은 요청당 기본 60초입니다. 달러 단위의 비용 한도는 제공하지 않습니다.
- 근거는 피드백당 최대 6청크, 청크당 최대 3000자로 제한하며 인접 청크도 이 한도 안에서 사용합니다.
  잘린 본문에는 `truncated=true`를 기록합니다. 잘린 범위 밖의 문장은 인용할 수 없습니다.
- 기본 출처 도메인은 `pknu.ac.kr`와 하위 도메인입니다. 다른 공식 호스트는
  `--source-domain <도메인>`으로 추가할 수 있습니다. 도메인 검사는 문서 내용의 사실성 검증이 아닙니다.
- 기본 smoke/regression/challenge 질문·출처를 보호하고 `--holdout <평가 JSONL>`로 추가합니다.
  동일 질문·정규화 URL만 자동 비교하므로 의미가 같은 질문이나 다른 URL의 같은 문서는 사람이 확인합니다.
- 외부 요청에 포함되는 피드백에서 이메일·휴대전화·표시된 학번을 일부 마스킹합니다.
  이름·자유 서술 등 모든 개인정보를 탐지하지는 못합니다. 입력 자료를 먼저 확인하고 결과의
  `privacy_checked`는 검수 후 직접 설정합니다. 원본은 로컬 검수 파일에 남으므로 `var/feedback/`에 보관합니다.
- 원문 인용이 실제 존재하는지는 자동 검사하지만, 인용이 답변의 모든 주장을 뒷받침하는지는
  자동으로 증명하지 않습니다. 생성과 별도의 사실 검증·승인 단계는 후속 작업입니다.
- 치명적인 인증 오류, 호출 한도 소진, 근거 API 오류에는 결과 폴더를 만들지 않습니다.
  개별 생성의 형식 오류·잘못된 인용은 실패 상태로 남기고 다음 후보를 처리합니다.
  `summary.json`의 실제 생성 건수와 `events.jsonl`을 확인합니다.

JSON 모드가 스키마 일치까지 보장하지 않으므로 응답은 로컬 검증을 추가 적용합니다.
API 형식과 불완전 응답 처리는 [OpenAI 공식 문서](https://developers.openai.com/api/docs/guides/structured-outputs)를 확인했습니다.

## 사용자 실행 검증

실제 모델 호출·검색·DB 조회·테스트·빌드는 실행하지 않았습니다. 아래 테스트는 가짜 모델과
가짜 HTTP 응답으로 근거 인용, 승인 경계, 개인정보 마스킹, 요청 상한과 기존 검수 연결을 확인합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest `
  eval\tests\test_generate_feedback_candidates.py `
  eval\tests\test_layout_rag.py `
  eval\tests\test_prepare_feedback_training.py `
  eval\tests\test_feedback_training.py
```

HTML 편집기의 데이터 보존·검수·Python 변환기 호환성 검사는 Node가 설치된 환경에서 다음 명령으로
실행합니다. Node가 없으면 이 테스트들은 건너뜁니다. 브라우저 파일 선택·다운로드와 실제 화면의
동작은 별도 수동 확인이 필요합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest eval\tests\test_feedback_review_editor.py
```
