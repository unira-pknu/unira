# 검색 채널 장애 처리

벡터 RPC 실패 뒤의 `continue`가 같은 데이터셋의 키워드 결과까지 버리던 경로를 제거했습니다.
두 채널은 같은 적용 대상 검사·후보 처리·중복 제거를 독립적으로 거치며, 기존 RRF 기반 점수와
데이터셋별 후보 확보·재정렬 정책을 유지합니다.

## 응답 기준

| 검색 상태 | 최종 근거/답변 | 처리 |
| --- | --- | --- |
| 시도한 모든 채널 정상 | 근거 있음 | 정상 답변, `retrieval_status=complete` |
| 시도한 모든 채널 정상 | 최종 근거 없음 | 정상적인 답변 불가 응답, HTTP 200. DB 전체에 자료가 없다는 보장은 아님 |
| 일부 채널 오류 | 남은 근거로 답변 가능 | 성공한 근거 유지, `retrieval_status=partial` |
| 일부 또는 모든 채널 오류 | 최종 근거 없음 | HTTP 503; "관련 정보를 찾을 수 없습니다"로 성공 처리하지 않음 |
| 일부 채널 오류 | 생성 결과가 전면 거절 | HTTP 503; 검색 범위가 불완전한 상태에서 자료 부재를 확정하지 않음 |
| RPC 설정이 비어 있음 | 검색 불가 | HTTP 503 |

벡터가 실패해도 같은 데이터셋의 키워드 결과를 사용합니다. 반대 경우와 다른 데이터셋의 성공 결과도 유지합니다.
키워드 검색어가 없어 호출하지 않은 채널은 실패로 계산하지 않습니다. 잘못된 RPC 응답 형식과
병렬 작업의 클라이언트 초기화 실패도 해당 채널의 오류로 격리합니다.

복합 질문에서는 검색 불가인 하위 질문 때문에 다른 하위 질문의 성공 근거를 버리지 않습니다.
미확인 하위 질문을 생성 모델에 명시하고, 확인하지 못한 요청을 자료 부재로 단정하지 않도록 합니다.
적용 대상/잡음 검사·재정렬 후에도 근거가 남지 않으면 장애 상태를 유지합니다.

## API·화면·평가 기록

JSON의 검색 불가 응답은 HTTP 503과 사용자용 안내 문자열을 반환합니다. 스트리밍은 이미 HTTP 200으로
연결되므로 `type=error`, `status_code=503`으로 끝나며 `done` 성공 이벤트를 보내지 않습니다.
숫자 검증과 마찬가지로 스트리밍 토큰은 최종 검증 전 임시 표시일 수 있습니다.
화면은 503 안내를 표시하며, 실패 응답을 정상적인 자료 없음 답변으로 저장하지 않습니다.

일부 장애가 있어도 유효한 답변은 유지합니다. API는 `retrieval_status=partial`을 제공하고,
모델에 확인된 자료 범위라는 제한을 전달합니다. 화면에도 확인된 자료로 답했다는 짧은 안내를 표시합니다.
일반 화면에 RPC명이나 내부 예외 메시지를 노출하지 않습니다.

`include_evidence=true`의 `evaluation_evidence.retrieval`에는 다음을 남깁니다.

- `status`: `complete` 또는 `partial`
- `channels`: RPC/채널 이름, 성공/오류, 원래 반환 수, 소요 시간, 오류 클래스 이름, 하위 질문 인덱스
- `failed_queries`: 검색 불가로 남은 하위 질문

오류 객체·예외 메시지·DB 연결 주소·인증정보는 이 기록에 넣지 않습니다. 기존 서버 로그의 예외 로그와는 구분됩니다.
평가 실행기도 응답의 `retrieval_status`를 보존합니다. HTTP 503은 API 실패로 남으며 실제 품질 점수의
전체 분모에서 제거하지 않습니다. 과거 원본·기준점 파일은 변경하지 않습니다.

이 상태는 기본 벡터/키워드 RPC의 가용성입니다. 제목 검색·인접 청크 보충 등 선택적인 추가 조회와
강의계획서/공지 목록 전용 조회의 모든 세부 장애를 통합한 상태는 아닙니다. 예상치 못한 오류와
전용 조회의 예외는 기존 502 경로로 전달되며 자료 없음으로 바꾸지 않습니다.

## 검증

사용자의 실행 원칙에 따라 테스트·실제 검색·서버·DB·외부 API·프런트엔드 빌드를 실행하지 않았습니다.
애플리케이션을 import하지 않는 Python AST 문법 검사와 `git diff --check`만 수행했습니다.
프로젝트 루트에서 다음 명령으로 확인합니다. 백엔드 회귀 사례는 DB와 모델을 모의 처리합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_retrieval_failures.py backend/tests/test_retrieval_parallel.py backend/tests/test_ask_json.py backend/tests/test_question_flow.py backend/tests/test_evaluation_evidence.py backend/tests/test_evidence_context.py eval/tests/test_eval_ask.py
npm --prefix frontend run lint -- app/lib/api.ts app/chat/ChatContent.tsx
npm --prefix frontend run build
```

실제 채널 장애 주입·운영 DB 검색·품질 재측정도 실행하지 않았습니다. 새 백엔드로 재측정할 때는
[평가 실행 순서](../evaluation/BASELINES.md#실행-순서)의 새 실행 이름을 사용하세요.
