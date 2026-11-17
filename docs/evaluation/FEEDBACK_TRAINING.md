# 싫어요 피드백으로 학습 데이터 만들기

기존 `message_feedback`의 싫어요를 **검토 후보 → 출처 확인·정답 작성 → 학습/평가 데이터**로
연결합니다. 싫어요는 실패 신호이며 정답 라벨이 아닙니다. 사용자의 의견이나 기존 나쁜 답변을
그대로 정답으로 사용하지 않습니다. 이 기능은 데이터 준비까지 제공하며 모델 학습·업로드·배포는
수행하지 않습니다. 실행 중인 RAG가 피드백만으로 자동 변경되지는 않습니다.

수정 답변과 관련 질문의 초안 작성을 자동화하려면
[피드백 기반 AI 후보 생성](FEEDBACK_AI_CANDIDATES.md)을 사용합니다. 결과는 검수 대기 상태로 저장합니다.
실제 피드백과 합성 질문을 함께 검수·변환하거나, 충돌 항목을 보고서에 보류하면서 정상 항목을
변환하려면 [② 검수·데이터 변환 자동화](FEEDBACK_REVIEW_AUTOMATION.md)를 사용합니다.

## 명령어로 수집하고 학습용 파일 준비하기

관리자 화면을 열지 않고 기존 관리자 API를 호출하는 `prepare_feedback_training`을 제공합니다.
새 패키지 설치나 DB 마이그레이션 없이 프로젝트 루트에서 실행합니다. API를 호출하는 계정은
`ADMIN_EMAILS`에 등록되어 있어야 하며, Supabase 로그인 세션의 `access_token`을 사용합니다.
Supabase anon key나 service-role key를 토큰으로 넣지 않습니다.

### 처음 수집

```powershell
# 실행 중인 로컬 백엔드 예시. 토큰은 실행 중 숨김 입력으로 받습니다.
.\.venv\Scripts\python.exe -m eval.tools.prepare_feedback_training `
  --base-url http://localhost:8000 `
  --output-dir var\feedback\round1
```

운영 백엔드라면 `--base-url https://<백엔드 도메인>`을 사용합니다. 프론트엔드 프록시를
통하려면 `--base-url https://<프론트엔드 도메인>/api`로 지정합니다. 원격 연결은 HTTPS를
사용하고, HTTP만 열려 있는 학교 서버는 localhost SSH 터널을 사용합니다. 리다이렉트는
따라가지 않으므로 실제 API 주소를 지정합니다. 토큰이 필요한 반복 실행에는
`backend/.env`에 아래 값을 저장할 수 있습니다. CLI가 토큰을 읽기 전에 해당 파일을 자동으로
로드하며, 이미 설정한 프로세스 환경 변수는 덮어쓰지 않습니다.

```dotenv
# Supabase 관리자 로그인 세션의 access_token만 입력 (Bearer 접두어 제외)
FEEDBACK_API_TOKEN=<관리자 access_token>
```

이 값 하나로 수집과 AI 후보 생성을 모두 실행할 수 있습니다. AI 후보 생성에 별도 계정을 쓰려면
`ASK_API_TOKEN`을 추가합니다. `backend/.env`는 Git에서 제외되며 토큰은 결과 파일에 저장하지
않습니다. 로그인 토큰은 영구 키가 아니므로 만료되어 HTTP 401이 발생하면 새 값으로 교체합니다.
저장된 값이 없으면 기존처럼 숨김 입력을 받습니다. anon key나 service-role key를 대신 넣지 않습니다.

현재 내려받은 파일도 사용할 수 있습니다. 다음 명령은 네트워크 요청 없이 검수 사본을 만듭니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.prepare_feedback_training `
  --input "$env:USERPROFILE\Downloads\feedback-review-2026-11-11T06-18-15-803Z.jsonl" `
  --output-dir var\feedback\round1
```

두 예시는 같은 출력 폴더를 사용하므로 하나를 선택합니다. 생성한
`var/feedback/round1/review.jsonl`에서 아래 **정답 검수** 절차에 따라 정답·출처·승인 상태를
작성합니다. 후보 수집만으로는 `train.jsonl`을 만들지 않습니다.

### 검수 후 한 명령으로 재조회·대조·변환

```powershell
.\.venv\Scripts\python.exe -m eval.tools.prepare_feedback_training `
  --base-url http://localhost:8000 `
  --reviewed var\feedback\round1\review.jsonl `
  --output-dir var\feedback\dataset_v1
```

이미 검수한 다운로드 파일이 있다면 `--reviewed`에 그 경로를 바로 지정할 수 있습니다.
이 명령은 현재 싫어요 전체를 다시 수집한 뒤 승인한 항목을 ID로 대조하고, 기존 변환기의
검수·중복·평가 데이터 분리 검사를 통과한 데이터만 저장합니다. 새로 생긴 피드백은 `pending`으로
포함합니다. 승인한 항목이 취소·좋아요 전환·삭제로 사라졌거나, 시각·기존 답변·출처·사유·의견·
원래 질문 키 등이 바뀌었으면 전체 변환을 중단합니다. 새 수집본을 기준으로 다시 검수해야 합니다.
변경된 미승인 항목은 최신 후보로 되돌리고, 사라진 미승인 항목은 제외합니다.

검수할 때는 `question`과 `review`만 수정하고 나머지 원본 필드는 보존합니다.
질문에는 개인정보 제거와 맥락 보강을 허용합니다. 원본 질문은 `original_question_key`로 대조합니다.
개인정보가 있는 원본 답변은 로컬 검수 파일에만 남으며 학습 입력에는 검수한 `corrected_answer`만
들어갑니다. 원본 파일과 검수 파일은 Git에서 제외되는 `var/feedback/` 아래에 보관합니다.

| 생성 파일 | 용도 |
| --- | --- |
| `candidates.jsonl` | 이번에 수집한 원본 후보. 변경하지 않고 보관 |
| `review.jsonl` | 검수 결과와 새 후보를 합친 파일. 다음 검수의 입력 |
| `train.jsonl` | 승인한 `split=train`의 질문·수정 정답을 담은 `messages` JSONL |
| `evaluation.jsonl` | 승인한 `split=evaluation`의 기존 평가 도구 호환 JSONL |
| `summary.json` | 수집 시각, 입력 종류, 최신 조회 대조 여부, 후보·학습·평가·제외 건수 |

`train.jsonl`과 `evaluation.jsonl`은 `--reviewed`를 지정했을 때만 만듭니다. 한쪽 split만
승인했다면 다른 파일은 비어 있습니다. 승인 사례가 하나도 없으면 변환을 중단합니다.
페이지 조회 실패·잘못된 응답·검수 오류·파일 쓰기 실패 시 불완전한 결과 폴더를 게시하지 않습니다.
기존 출력 폴더는 덮어쓰지 않으므로 매번 새 이름을 지정합니다.

추가 평가 파일은 `--holdout <평가 JSONL>`로 보호할 수 있습니다. 기본
`smoke/regression/challenge`는 항상 보호합니다. `--page-size`는 1~50(기본 20), `--timeout`은
페이지당 대기 시간(기본 60초)입니다. 401이면 로그인 토큰을 갱신하고, 403이면 관리자 등록을,
404 또는 503이면 API 주소와 운영 백엔드 반영 여부를 확인합니다.

오프라인 변환은 `--base-url` 대신 `--input var\feedback\round1\candidates.jsonl`을 지정합니다.
이 경우 입력 파일과 검수 파일만 대조하며 현재 서버에서 취소·변경된 피드백은 확인하지 못합니다.
`summary.json`의 `freshness_checked`는 API 재조회와 `--reviewed`를 함께 사용한 경우에만
`true`입니다. API도 페이지 전체의 단일 DB 스냅샷은 아니므로 조회 중 변경은 다음 실행에서
반영될 수 있습니다.

## 1. 후보 내려받기

백엔드와 프론트엔드에 변경 코드를 반영한 뒤 `ADMIN_EMAILS`에 등록된 계정으로 `/admin`을 엽니다.
피드백 로그의 **학습 후보 내려받기 (JSONL)**를 누릅니다. 기존 015 피드백 마이그레이션이
적용된 DB라면 추가 마이그레이션은 필요하지 않습니다.

Vercel에서 `Not Found`가 뜨면 운영 백엔드에 기능이 반영됐는지 확인합니다.
학교 서버의 별도 운영 폴더는 자동 갱신되지 않으며 `routers/admin.py`와
`feedback_training.py`가 함께 필요합니다. [두 파일 반영·재시작 절차](../backend/SCHOOL_SERVER.md#학습-후보-다운로드에서-not-found가-뜰-때)를 따릅니다.

- 현재 `rating=down`인 피드백을 페이지당 20건씩 조회합니다. 화면의 최근 500건 제한과 독립적입니다.
- 같은 채팅에서 답변 시각 이전의 가장 최근 사용자 메시지를 질문으로 연결합니다.
  연결 실패 또는 동일 시각으로 순서를 판단할 수 없는 항목은 `pairing_status`로 표시합니다.
- 질문·기존 답변·저장된 화면용 출처·싫어요 사유·의견과 빈 검수 필드를 담습니다.
  계정 ID와 채팅 ID는 내보내지 않지만 본문에는 개인정보가 남을 수 있습니다.
- 모든 항목은 `review.status=pending`이며, 조회 중 실패하면 부분 파일을 다운로드하지 않습니다.
- 여러 페이지 조회는 DB의 단일 트랜잭션 스냅샷이 아닙니다. 도중 변경된 피드백은 다음 다운로드에서
  다시 확인하세요. 취소·좋아요 전환·채팅 삭제는 다음 조회부터 반영되며 이미 받은 파일은 별도로
  정리해야 합니다. 검수·학습 직전에 다시 조회해 ID와 `feedback_updated_at`을 대조하세요.

다운로드 파일은 Git에서 제외되는 `var/feedback/`에 보관하고 검수용 사본을
`var/feedback/reviewed.jsonl`로 저장합니다. 각 줄이 JSON 객체 하나인 JSONL 형식을 유지합니다.
서버에 검수 내용을 저장하거나 다른 관리자의 검수와 병합하는 기능은 포함하지 않습니다.

API는 `GET /admin/feedback/training-candidates?limit=20&after_id=<이전 nextCursor>`입니다.
`items`와 `nextCursor`를 반환하고 마지막 페이지는 `nextCursor=null`입니다.
프론트엔드는 `/api/admin/feedback/training-candidates`로 프록시합니다.
두 경로 모두 응답 캐시를 금지하며, 백엔드는 기존 관리자 인증을 적용합니다.

## 2. 정답 검수

`pairing_status=matched`인 항목만 승인할 수 있습니다. 다음 순서로 검토합니다.

1. 학습에 넣을 `question`·`review.corrected_answer`에서 개인정보를 제거하고,
   이전 대화가 필요한 질문은 `question`에 학과·입학연도 등
   필요한 맥락을 보강합니다. `original_question_key`는 원래 질문의 중복 검사 키이므로 유지합니다.
2. 실패 원인을 검토합니다. 자료가 없다면 수집 대상을 보강하고, 관련 자료를 못 찾았다면 검색을
   개선하며, 자료를 잘못 해석했다면 생성 지침이나 학습 후보로 다룹니다.
3. 공식 출처의 대상·조건·시행 시점을 확인해 `corrected_answer`를 작성합니다. 기존 답변의
   `[1]` 같은 번호를 근거 없이 옮기지 말고, 독립적으로 이해되는 정답을 작성합니다.
4. 실제 확인한 문서 URL을 `verified_sources`에 넣습니다. `answer_sources`는 과거 화면용
   발췌이므로 검증된 근거로 자동 인정하지 않습니다. 출처를 확인할 수 없으면 보류합니다.
5. 같은 원문 문서와 관련 질문은 한쪽에만 배정합니다. 반복된 질문은 대표 교정본 하나만 승인하고,
   나머지는 보류하거나 제외합니다. `split`은 `train` 또는 `evaluation`으로 직접 정합니다.
6. 검수자와 시간대가 있는 검수 시각을 기록하고 개인정보·맥락 확인을 `true`로 바꾼 뒤 승인합니다.

아래는 `review` 객체만의 형식 예시입니다. URL과 정답은 실제 확인한 값으로 바꿉니다.

```json
{
  "status": "approved",
  "split": "train",
  "corrected_answer": "공식 문서에서 확인한 조건과 적용 범위를 반영한 정답",
  "verified_sources": ["https://example.edu/official-document"],
  "required_facts": [],
  "forbidden_claims": [],
  "reviewed_by": "검수자 식별명",
  "reviewed_at": "2026-11-11T18:00:00+09:00",
  "privacy_checked": true,
  "context_checked": true
}
```

평가용은 `split=evaluation`으로 지정하고 `required_facts`에 정답의 필수 사실을 문자열 목록으로
작성합니다. `forbidden_claims`에는 금지할 오답을 작성합니다. 현재 변환기는 **출처를 확인한
답변 가능 질문**을 지원합니다. 답변 불가·확인 질문 요구 사례는 기존 평가 라벨 절차로 다룹니다.
제외할 후보는 `status=rejected`, 보류할 후보는 `pending`을 유지합니다.

## 3. 학습/평가 JSONL 생성

프로젝트 루트에서 사용자가 실행합니다. 매번 새 출력 폴더를 지정합니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.build_feedback_training `
  --input var\feedback\reviewed.jsonl `
  --output-dir var\feedback\dataset_v1
```

생성물은 다음 두 파일입니다.

| 파일 | 내용 |
| --- | --- |
| `train.jsonl` | `messages=[{role:user, content:질문}, {role:assistant, content:검수한 정답}]` |
| `evaluation.jsonl` | 기존 `eval_ask`와 채점기가 읽는 구조화 평가 케이스·필수 사실·검증 URL |

후보 파일의 나쁜 답변·사유·사용자 의견·검수자 정보는 학습 입력에 넣지 않습니다.
`approved`가 아닌 항목은 건너뛰며, 승인했지만 필수 검수를 누락한 항목은 전체 변환을 중단합니다.
기존 출력 폴더를 덮어쓰지 않습니다. 승인된 항목이 하나도 없으면 파일을 만들지 않습니다.

변환기는 같은 정규화 질문과 원래 질문 키의 중복, 학습/평가 간 동일 질문·문서 URL 중복을
검사합니다. URL의 호스트 대소문자·끝 슬래시·fragment 차이를 정리해 비교합니다.
기본 `smoke`, `regression`, `challenge`에 있는 질문과 명시된 출처 URL도 사용하지 않습니다.
이전 회차 평가 파일도 보호하려면 추가로 지정합니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.build_feedback_training `
  --input var\feedback\reviewed.jsonl `
  --output-dir var\feedback\dataset_v2 `
  --holdout var\feedback\dataset_v1\evaluation.jsonl
```

이는 의미상 같은 질문이나 서로 다른 URL의 동일 문서까지 판별하는 검사는 아닙니다.
기존 평가 라벨에 URL이 없거나 source ID만 있으면 출처 중복을 자동 판정할 수 없습니다.
원문 문서·시점·학과를 기준으로 검수자가 먼저 분리해야 합니다. 검수 표시는 사람이 했다는
기록이며, 도구가 개인정보 제거와 정답의 사실성을 자동 검증하지는 않습니다.

## 4. 개선 후 평가

데이터 생성 자체는 품질 개선이나 모델 학습 완료를 뜻하지 않습니다. 자료·검색·프롬프트 개선,
또는 별도 학습 절차를 거친 뒤 분리한 평가 질문으로 개선 여부를 확인합니다.
학습 모델을 선택하거나 학습 서비스를 호출하는 기능은 이 도구에 없습니다.

```powershell
# 평가용 항목이 있는 경우, 실행 중인 백엔드와 유효한 로그인 토큰으로 평가
$env:ASK_API_TOKEN = "<Supabase 로그인 세션의 access_token>"
.\.venv\Scripts\python.exe -m eval.tools.eval_ask `
  --questions var\feedback\dataset_v1\evaluation.jsonl `
  --output eval\results\feedback_v1.jsonl

# 실제 모델 API를 호출하는 별도 채점 단계
.\.venv\Scripts\python.exe -m eval.tools.grade_eval_results `
  --results eval\results\feedback_v1.jsonl
```

인증·기준점 비교 절차는 [기준점 관리](BASELINES.md)를 따릅니다. 평가용으로 정한 사례는
프롬프트 예시나 학습 데이터로 사용하지 않습니다. 기존 100문항 기준점도 함께 비교합니다.

## 사용자 실행 검증

③ 단계의 학습 제출·상태 확인·동일 검색 자료 비교는
[학습·비교 평가 자동화](FEEDBACK_TRAINING_AUTOMATION.md)를 따릅니다.
최소 학습 건수와 제공자 계정 제약을 먼저 확인하며, 현재 3건으로는 학습 제출이 보류됩니다.

아래 테스트·빌드·실제 DB 조회·다운로드·데이터 변환·평가 실행은 구현 과정에서 실행하지 않았습니다.

```powershell
# 프로젝트 루트: 실 DB와 모델 호출 없는 권한·페어링·검수·중복·CLI 수집 경계 테스트
.\.venv\Scripts\python.exe -m pytest backend\tests\test_admin.py eval\tests\test_feedback_training.py eval\tests\test_prepare_feedback_training.py

# 프론트엔드 의존성이 설치된 상태에서
npm --prefix frontend run lint
npm --prefix frontend run build
```

실제 화면에서는 관리자만 다운로드할 수 있는지, 싫어요 취소/좋아요 전환 후 다시 받은 파일에서
빠지는지, 여러 페이지를 내려받는지 확인합니다. 연결이 불확실한 후보는 승인하지 않습니다.
