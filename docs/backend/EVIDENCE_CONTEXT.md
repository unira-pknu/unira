# 검색 근거 보존

일반 문서를 청크 앞 1000자에서 자르던 경로를 질문에 맞춘 구간 선택으로 변경합니다.
화면의 짧은 출처 카드 발췌와 별개로, 답변 모델에 전달할 근거를 선택합니다.

## 구간 선택

- 사용자 질문과 검색 계획의 구체화된 질문을 기준으로 관련 문장·줄을 선택합니다. 질문 자체의 값은 근거 본문에 추가하지 않습니다.
- 길이가 목표를 넘는 일반 청크에서는 전처리가 붙인 `문서제목`·분류·파일명 머리글을 본문 구간 평가에서 제외합니다. 인접 청크의 반복 머리글도 제거해 제목의 검색어 일치만으로 뒤쪽 실제 답변이 밀리지 않도록 합니다. 출처 제목·URL은 프롬프트의 별도 필드에 유지하고, 평가 캡처의 전체 원문은 변경하지 않습니다.
- 관련 구간의 주변 제목·예외·제외 조건·제출 서류를 함께 선택하고, 선택한 구간은 원문 순서로 표시합니다.
- `MAX_CHARS_PER_CHUNK` 기본값 1000은 목표 길이입니다. 완전한 문장·조건·표 행을 유지할 때 출처당 `max(4000, 설정값)`자까지 허용합니다. 생략한 앞·중간·뒷부분은 표시합니다.
- Markdown 표와 열 이름을 확인할 수 있는 줄바꿈 기반 파이프/탭 표는 선택한 행에 제목·열 이름을 반복합니다. 서로 다른 표의 열 이름을 섞지 않습니다.
- 스프레드시트는 전처리에서 보존한 시트·원본 행·열 이름과 실제 본문 값을 유지합니다. 본문에 없는 셀 값을 metadata에서 추정해 채우지 않습니다. 길이 한도를 넘는 표 행은 셀 중간을 자르지 않고 원문 확인 필요를 표시합니다.
- 강의계획서는 이미 학기·분반을 확정하고 구간을 선택하는 기존 전용 경로를 유지합니다.

전체 문서가 아닌 제한된 검색 근거입니다. 질문 관련성은 어휘 일치 기반이므로 모든 동의어나 먼 위치의 예외를 회수한다고 보장하지 않습니다.
기존 DB 본문에서 이미 사라진 행/열 경계, OCR이 잃은 셀, 멀리 떨어진 표 머리글을 복원하지 않습니다.
그러한 자료는 원본과 전처리 결과를 대조한 뒤 별도 재처리·적재가 필요합니다. 이 변경에서 DB를 수정하거나 재적재하지 않습니다.

## 인접 청크 보충

최종 검색 결과의 순위와 인용 번호를 정한 다음, 다음 조건을 모두 만족하는 앞뒤 한 청크씩을 보충합니다.

1. RPC와 연결된 청크 테이블, `source_id`, 정수 `chunk_index`가 확인되어야 합니다. 같은 URL이나 첨부 게시물 주소만으로 서로 다른 파일을 합치지 않습니다.
2. 조회 결과의 원문 ID와 정확한 인접 순서를 다시 확인합니다. 학교·학과·학생 유형·학기 등 기존 적용 대상 검사를 추가 근거에도 적용합니다.
3. 스프레드시트는 시트와 원본 행 번호까지 일치해야 합니다. 다음 행의 다른 과목·교수·학기 값을 한 인용으로 합치지 않습니다.

`RAG_CONTEXT_NEIGHBOR_SOURCES=4`는 요청당 추가 조회 횟수의 상한입니다. 각 조회는 같은 원문의 `chunk_index - 1`, `chunk_index + 1`만 읽고, 임베딩은 받지 않습니다.
상위 자료부터 보충하며 동일 요청 안에서 조회 결과를 재사용합니다. 조회 실패나 식별자 누락 시 원래 검색 근거로 계속 진행합니다.
0으로 설정하면 인접 조회만 비활성화하고 질문별 구간 선택은 유지합니다. 최신 공지의 직접 목록 응답과 강의계획서 전용 응답은 기존 경로를 유지합니다.

보충한 청크는 원래 인용 번호의 근거 범위에 포함됩니다. 원래 검색 점수와 인용 순서를 바꾸지 않습니다.
UI 출처 카드도 보충된 본문에서 발췌하므로 답변에 사용한 인접 조건을 확인할 수 있습니다.

## 생성·검증·평가의 일치

일반 JSON 응답과 스트리밍 생성, 숫자 검사, 수정 답변 재검사, 평가용 `shown_content`와 `prompt_source_block`은
같은 질문·설정·선택 함수를 사용합니다. 선택되지 않은 다른 구간이나 사용자 질문의 숫자로 숫자 검사를 통과시키지 않습니다.

`include_evidence=true` 응답에서:

- `sources[].content`: 인용별 검색 청크와 보충한 인접 청크를 원문 순서로 합친 본문
- `sources[].shown_content`: 실제 모델에 전달한 선택 구간
- `sources[].context_chunks`: 구성 청크 각각의 ID, `source_id`, `chunk_index`, 본문과 SHA-256, 허용된 metadata
- `sources[].metadata.request_coverage`: 하위 질문별 `candidates_selected`, `no_candidates`, `not_selected`, `search_unavailable` 및 범위 검사 후 후보 수. 선택된 후보의 의미상 정답 여부를 나타내는 지표는 아닙니다.
- `sources[].metadata.comparison_targets`: 등록된 이름으로 확인한 비교 대상. 한쪽 근거 누락을 전체 자료 부재나 두 대상의 동일성으로 바꾸지 않도록 생성 프롬프트에도 전달합니다.
- `settings.rag_context_neighbor_sources`: 추가 조회 상한

기존 평가 파일과 2026-11-10 기준점은 변경하지 않습니다. 변경 후 품질 차이는 백엔드 재시작 후 새 이름의 전체 실행으로 측정해야 합니다.
전달 본문은 기존보다 길어질 수 있고 추가 조회가 발생하므로 응답 시간·토큰 사용량도 함께 비교해야 합니다.

## 검증

사용자의 실행 원칙에 따라 이번 구현에서는 테스트·서버·DB 조회·외부 API·전체 평가를 실행하지 않았습니다.
애플리케이션을 import하지 않는 AST 문법 검사와 `git diff --check`만 통과했습니다.
2026-11-11 대상·부분 답변 보강분은 코드 대조와 `git diff --check`만 수행했습니다. 위 AST 검사 기록은 이전 변경의 기록입니다.
추가한 회귀 사례는 뒤쪽 신청 근거와 예외/서류, 표 제목/열/행, 인접 표 머리글, 동일 행 분할,
다른 원문·대상 배제, 조회 실패/횟수 제한, 숫자 검사·JSON/스트리밍·평가 캡처의 일치를 다룹니다.

프로젝트 루트에서 다음 명령으로 확인합니다. DB와 모델 호출은 테스트용 객체로 대체됩니다.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_evidence_context.py backend/tests/test_grounding.py backend/tests/test_generation.py backend/tests/test_evaluation_evidence.py backend/tests/test_source_scope.py backend/tests/test_question_flow.py backend/tests/test_ask_json.py backend/tests/test_ask_sources.py
```

실제 검색과 품질 비교는 [평가 실행 순서](../evaluation/BASELINES.md#실행-순서)를 사용합니다.
새 백엔드 버전·새 실행 이름을 사용하고, 실제 DB 스냅샷이 없다면 미확인 라벨이라는 한계를 유지해야 합니다.

## 대상 보존·부분 답변 보강 검증

등록된 학과·전공명과 명시적 대학명·인용 제목을 비교해 누락된 검색 계획을 원문으로 되돌립니다.
최대 3개 등록 전공의 비교는 각 대상에 공통 비교 항목을 붙여 검색합니다. 서로 다른 연도 조건이 있는데
모델이 한쪽을 누락한 경우에는 조건을 임의로 배분하지 않고 원문 전체를 유지합니다. 비교 대상이 여러 개인
전체 질문을 한 학과·한 입학연도로 좁히지 않습니다. 보존 검사는 일반적인 모든 고유명사를 인식하는 기능은 아닙니다.

검색된 내용이 교육과정 비교인데 개별 강의계획서만 있거나, 같은 데이터셋의 다른 전공 교육과정 제목만
일치하는 경우는 적용 대상 검사에서 제외합니다. 일반 안내·공통 규정의 기존 범위 검사는 유지합니다.
빈 결과나 병합 탈락이 있는 하위 질문은 그 상태를 생성 단계에 넘깁니다. 실제 부분 답변과 확인 질문의
적절성은 모델 출력과 직접 근거를 함께 검토해야 합니다. 문구 기반 `answerable` 판정은 의미 채점을 대체하지 않습니다.

다음 명령은 프로젝트 루트에서 사용자가 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_query_plan.py backend/tests/test_source_scope.py backend/tests/test_evidence_context.py backend/tests/test_generation.py backend/tests/test_question_flow.py backend/tests/test_ask_json.py backend/tests/test_retrieval_failures.py backend/tests/test_evaluation_evidence.py backend/tests/test_retrieval_trace.py
```

새 코드로 백엔드를 재시작하고, `ASK_API_BASE_URL`과 유효한 로그인 토큰 `ASK_API_TOKEN`을 설정한 뒤
새 출력 이름으로 100문항을 실행합니다. 아래 평가와 채점은 실제 API 호출을 수행합니다.

```powershell
.\.venv\Scripts\python.exe -m eval.tools.eval_ask --questions eval/cases/regression.jsonl --output eval/results/subject_evidence_partial_v1.jsonl
.\.venv\Scripts\python.exe -m eval.tools.grade_eval_results --results eval/results/subject_evidence_partial_v1.jsonl
```

고유명사의 최종 검색어 유지, 비교 양쪽의 출처, `content`와 `shown_content`의 핵심 구절,
요청별 미확인 부분과 확인 질문을 대조합니다. 이전 100문항 원본·라벨·혼합 채점 기준점은 덮어쓰지 않습니다.
