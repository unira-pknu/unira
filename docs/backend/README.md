# UNIRA 백엔드

백엔드 코드는 [`backend/`](../../backend)에 있습니다. 아래 설치 명령은 프로젝트 루트,
서버 실행 명령은 본문의 안내에 따라 `backend/`에서 실행합니다.

FastAPI 백엔드. 단일 엔드포인트 `POST /ask` 를 제공한다.

```
질문 → (sentence-transformers 임베딩) → Supabase RPC `match_rag_documents`
     → (top-k 청크) → OpenAI gpt-4o-mini → 답변 + 출처
```

## 폴더 구조

```
backend/
├── app/
│   ├── main.py            FastAPI 앱 + CORS + lifespan(모델 로드)
│   ├── config.py          pydantic-settings 기반 환경변수
│   ├── schemas.py         AskRequest / AskResponse / Source
│   ├── deps.py            싱글턴 AppState 주입
│   ├── embeddings.py      질문 임베딩용 sentence-transformers 래퍼
│   ├── retrieval.py       Supabase RPC 호출
│   ├── generation.py      OpenAI 프롬프트 + 답변 생성
│   └── routers/ask.py     POST /ask 핸들러
├── prompts/system.txt     LLM 시스템 프롬프트 (한국어, 출처 강제)
├── requirements.txt
└── .env.example
```

질문 임베딩 래퍼는 [`backend/app/embeddings.py`](../../backend/app/embeddings.py)에서
관리합니다. 수집 문서의 배치 벡터화 도구는 `scripts/vectorization/`에 있습니다.

## 사전 조건

- Python 3.12 (프로젝트 기본 가상환경 `.venv`, 현재 3.12.10)
- Supabase 프로젝트에 다음이 이미 존재:
  - `rag_chunks` (또는 호환 테이블) — `embedding extensions.vector(384)` 컬럼
  - RPC `match_rag_documents(query_embedding extensions.vector(384), match_count int, min_similarity float, metadata_filter jsonb)`
  - 반환 컬럼: `uri, content, chunk_id, chunk_index, metadata, similarity`
- 임베딩은 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384차원)으로 적재되어 있어야 함. **다른 모델로 적재됐다면 검색 정확도가 의미 없는 수준으로 떨어짐.**

## 설치

프로젝트 루트의 Python 3.12 가상환경 `.venv`를 사용합니다. 새로 준비할 때는 다음 명령을 실행합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
```

PowerShell에서 활성화 스크립트가 차단되면 현재 터미널에만 실행을 허용한 뒤 활성화합니다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

`sentence-transformers` 첫 import 시 모델을 HuggingFace 에서 캐시 디렉터리(`%USERPROFILE%\.cache\huggingface`)로 받는다 (약 470MB).

## 환경변수 설정

1. `backend\.env.example` 을 `backend\.env` 로 복사한다.
2. 다음 키를 채운다.

| 키 | 설명 |
|---|---|
| `SUPABASE_URL` | `https://<project>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | service_role key (anon key 로는 RLS 정책에 따라 RPC 호출이 거부될 수 있음) |
| `OPENAI_API_KEY` | OpenAI API 키 |
| `ADMIN_EMAILS` | 관리자 대시보드(`/admin`)를 볼 수 있는 계정 이메일, 콤마 구분. 비워 두면 아무도 접근 불가(기본 거부). 대시보드는 service_role 로 전체 사용자의 질문·피드백을 읽으므로 이 목록이 유일한 접근 제어다 |

기타 키 기본값은 `.env.example` 참고. `EXPECTED_DIMENSIONS` 와 `EMBEDDING_MODEL` 의 차원이 DB 컬럼과 일치해야 한다.

> 보안: `.env` 는 루트 `.gitignore` 에 포함되어 있다. `.env.example` 도 현재 .gitignore 패턴에 잡혀 있어서 git 에 안 올라간다. 팀과 공유할 거면 `.gitignore` 에서 `.env.example` 줄을 제거해야 한다.

## 실행

`backend/` 폴더에서 실행하는 것을 기준으로 한다 (`.env` 가 cwd 에서 로드됨).

```powershell
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

첫 부팅 시 `sentence-transformers` 모델을 로드하느라 1~5초 정도 걸린다. `Backend startup complete.` 로그가 보이면 ready.

## 사용 예시

```powershell
curl -X POST http://localhost:8000/ask `
  -H "Content-Type: application/json" `
  -d '{"question":"졸업 요건이 어떻게 되나요?"}'
```

응답:

```json
{
  "answer": "졸업 요건은 ... [1]. 또한 ... [2].",
  "sources": [
    {
      "title": "졸업요건 안내",
      "uri": "https://ce.pknu.ac.kr/...",
      "content": "...",
      "similarity": 0.7821,
      "attachments": [
        {"name": "졸업요건 안내.pdf", "url": "https://ce.pknu.ac.kr/.../download"}
      ]
    }
  ]
}
```

검색 결과가 0건이거나 LLM 이 자료에서 답을 만들 수 없다고 판단하면:

```json
{ "answer": "관련 정보를 찾을 수 없습니다.", "sources": [] }
```

## 주요 동작 규칙

- **한국어 강제**: 시스템 프롬프트에서 한국어 외 답변 금지
- **출처 인라인**: `[1]`, `[2]` 형식으로 답변 안에 표기, 자료 목록 순서와 일치
- **자료 외 추측 금지**: 자료에 없는 내용은 "관련 정보를 찾을 수 없습니다." 로 대답
- **수치 근거 검사**: 사용자 질문·이전 대화의 숫자는 근거로 인정하지 않고, 모델에 전달한 참고 자료 본문만으로 검사합니다. 인용한 출처에 없는 수치를 다른 자료에서 가져왔는지도 확인합니다. 일반 응답과 스트리밍 모두 한 번 수정한 뒤 같은 검사를 다시 수행하며, 수정 답변도 실패하면 "관련 정보를 찾을 수 없습니다."로 대체합니다. 스트리밍에서는 교체 이벤트(`replace`)와 최종 응답(`done`)에 같은 검증 결과를 전달합니다.
- **컨텍스트 길이 제한**: GPT에 전달하는 일반 청크는 `MAX_CHARS_PER_CHUNK=1000`자로 제한하고, 강의계획서의 근거 구간은 최대 4000자로 전달합니다. 수치 검사에도 같은 길이 제한을 적용합니다.
- **출처 원문 발췌**: 답변의 `[1]`, `[2]` 인용 문장과 질문을 기준으로 관련 구간을 골라 `SOURCE_EXCERPT_MAX_CHARS=320`자 이내로 반환
- **로깅**: 질문은 INFO 레벨로 남김. 답변/응답 본문은 로깅하지 않음
- **에러 응답**: 임베딩/검색/생성 단계별로 500/502 분기

수치 검사의 회귀 테스트는 프로젝트 루트에서 다음 명령으로 실행합니다. OpenAI 호출은 테스트용 응답으로 대체됩니다.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_grounding.py backend/tests/test_generation.py backend/tests/test_ask_json.py
```

현재 수치 검사는 학점·금액·평점 등 지원하는 패턴의 숫자 존재 여부를 검사합니다. 숫자의 대상·단위·조건이 의미상 같은지나 날짜·연락처 등 모든 사실의 정확성까지 보장하지는 않습니다.

## 알려진 제약 / 다음 단계

- 회수된 청크가 0건이면 즉시 fallback. 청크가 있으면 LLM은 관련 근거가 있는 범위까지 답하고, 질문의 핵심에 대한 근거가 전혀 없을 때만 "관련 정보를 찾을 수 없습니다."라고 답함. 검색 임계값은 `RAG_MIN_SIMILARITY`로 조정.
- 스트리밍 응답, 대화 히스토리, 피드백 엔드포인트는 미구현. 추후 `routers/` 에 추가하면 됨.
- 모델 자동 워밍업 외 헬스체크는 `/health` (단순 ok) 만 제공.
# 데이터셋별 검색 테이블

`018_split_dataset_tables.sql` 적용 이후 기본 검색은 다섯 독립 테이블 계열을 사용합니다.
`RPC_NAMES`를 환경 변수로 지정했다면 기본값보다 우선하므로 다음 목록으로 변경하고 백엔드를 재시작·재배포합니다.

```dotenv
RPC_NAMES=match_rule_documents,match_pknu_notice_documents,match_pknu_main_documents,match_rag_documents,match_pknu_syllabus_documents
```

각 RPC에는 같은 이름의 `_lexical` 검색 함수도 있습니다. 학과 검색 응답의 metadata에는
`department_id`, `department_name`, `college_name`, `department_homepage` 및 전공 정보가 포함됩니다.
`match_rag_documents`의 `metadata_filter`에 `department_id`를 지정하면 학과를 제한할 수 있습니다.
기존 `match_pknu_student_life_documents`와 `_lexical` RPC는 배포 전환 기간에도 호출할 수 있도록
본교 공지 외 테이블을 조회합니다. `pknu_student_life_*` 물리 테이블은 제거되었습니다.
질문 임베딩 모델과 384차원 입력 형식은 유지합니다.
