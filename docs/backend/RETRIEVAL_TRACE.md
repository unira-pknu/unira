# 출처 식별과 검색 단계 추적

일반 응답의 출처 식별자와 평가 요청의 단계별 후보 기록을 연결합니다.
`include_evidence=true`인 JSON 요청에서만 전체 추적을 수집합니다. 기본 화면 요청·스트리밍·워밍업은
전체 후보를 기록하지 않습니다. 검색·재정렬 점수와 선택 정책은 변경하지 않습니다.

## 출처 식별자

`sources[]`에 다음 필드를 추가합니다. 출처 카드 발췌 후에도 유지하며 프런트엔드의 출처 변환·대화 저장에도 보존합니다.

| 필드 | 의미 |
| --- | --- |
| `source_id` | DB 원문 식별자 |
| `source_ids` | 교수별 강의 목록처럼 여러 원문을 합친 경우 실제 구성 원문 ID 목록 |
| `chunk_id` | 저장된 원래 청크 식별자. 정수/문자열을 유지하고 0도 유효한 값으로 보존 |
| `chunk_row_id` | 청크 테이블의 `id`. `chunk_id`와 구분 |
| `chunk_index` | 원문 내 청크 순서. `chunk_id`에서 추정하지 않음 |
| `source_dataset` | 저장된 데이터셋. 없으면 알려진 RPC의 데이터셋 계열 |
| `ingestion_dataset` | 저장된 적재 데이터셋 식별자 |
| `department_id` | 출처 metadata/등록된 출처 사이트에서 확인한 학과 ID. 질문의 학과를 복사하지 않음 |
| `rpc_name` | 해당 출처의 검색 RPC 계열 |

기존 데이터에 식별자가 없으면 미확인 상태로 두며 원문 ID나 청크 ID를 만들어 넣지 않습니다.
공지 목록의 원문 행 ID나 교수별 목록의 합성 표시 ID를 DB 청크 행 ID로 표시하지 않습니다.
인용 번호 `[1]`은 여전히 응답 출처 배열의 1번째 항목이며 DB 식별자와 별개입니다.
같은 게시물 URL을 공유하는 첨부 파일은 `source_id`로 구분합니다.

## 기록 위치와 단계

`evaluation_evidence.retrieval.trace` 아래에 다음을 저장합니다.

- `tasks[]`: 하위 질문별 검색 경로와 `trace`. 일반 혼합 검색은 RPC 후보부터 최종 선택까지 기록합니다.
- `request`: 하위 질문 결과의 최종 병합과 인접 근거 보충 기록입니다.

| 단계 | 확인할 내용 |
| --- | --- |
| `vector_returned`, `lexical_returned` | RPC가 실제 반환한 후보, 원래 순위, 채널 상태 |
| `channel_merged` | 적용 대상·잡음·데이터셋 검사를 통과한 데이터셋별 후보와 초기 점수 |
| `candidate_pool` | 중복·후보 수·출처당 제한을 거쳐 재정렬 후보 풀에 들어간 자료 |
| `expanded_candidates`, `department_focus`, `applicability_checked` | 제목/일정 등 보충, 학과별 정리, 적용 대상 재검사 전후 |
| `rerank_input`, `rerank_ordered` | 재정렬 입력과 전체 정렬 결과. 초기 점수·재정렬 점수·최종 결합 점수 |
| `after_source_caps`, `task_selected` | 출처당 제한과 top-k 선택을 구분 |
| `task_scope_checked`, `request_merge_input`, `request_selected` | 하위 질문의 최종 범위 검사와 전체 응답 병합 전후 |
| `context_components`, `generation_sources` | 인접 청크 구성과 최종 생성 근거 |

`decisions`에는 초기/확장 적용 대상 검사, 잡음·데이터셋 제외, 마지막 출처당 제한의 사유를 남깁니다.
초기 후보 풀의 슬롯·중복·출처당 제한은 전후 집합과 설정으로 확인하며 모든 슬롯 시도의 개별 사유를 기록하지는 않습니다.
`reranker.status`는 `applied`, `disabled`, `fallback`, `no_candidates`, `empty_question`, `score_count_mismatch`를 구분합니다.
재정렬 실패의 초기 점수 정렬을 정상 재정렬 결과라고 표시하지 않습니다.

후보 기록은 ID·순위·허용된 점수·제목·본문 해시/길이만 포함합니다. 임베딩, 후보 본문 전체,
임의 metadata, 인증정보나 예외 메시지를 복사하지 않습니다. 최종 근거 원문은 기존처럼 `sources[].content`에 보존합니다.
각 검색 및 요청 기록기는 최대 10,000개 후보/제외 기록을 남기며 초과 시 실제 단계별 개수는 유지하고
`complete=false`로 표시합니다. 기록이 불완전하면 후보가 없었다고 확정할 수 없습니다.

단계 연결용 `key`는 RPC 계열과 DB 행 ID 또는 원문·청크 위치를 결합합니다. ID가 없는 자료에는
`unidentified:` 해시 키를 사용하지만 이것은 DB 식별자가 아닙니다. 본문이 정리되거나 인접 근거가
보충될 때도 기록 연결을 유지하며, 구성 청크의 개별 ID·해시는 `context_chunks`에서 확인합니다.

## 생성 단계

최종 출처의 `key`와 `index`, `shown_content`, `cited_in_answer`를 연결합니다.
`generation`에는 최종 답변 해시, 인용 번호, 유효 범위를 벗어난 번호와 직접 응답/생성 여부를 남깁니다.
번호 추출은 실제 최종 답변을 기준으로 하며, 다른 문장으로 인용을 상속하거나 추정하지 않습니다.

검색 불가 HTTP 503에서도 평가 요청은 오류 본문의 `evaluation_evidence`에 확보한 추적을 보존합니다.
기존 평가 실행기는 이를 `error.evaluation_evidence`에 남기며 API 실패 상태를 유지합니다.

## 저장 결과로 진단하기

새 백엔드로 [평가 실행 순서](../evaluation/BASELINES.md#실행-순서)에 따라 새 결과 파일을 수집합니다.
기존 `eval_ask`는 이미 `include_evidence=true`를 사용하므로 별도 플래그가 필요하지 않습니다.
과거 결과·기준점에는 없던 후보 기록을 소급해서 채우지 않습니다.

프로젝트 루트에서 다음 명령으로 특정 출처/청크를 확인합니다. DB·검색·LLM을 호출하지 않는 오프라인 도구입니다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m eval.tools.inspect_retrieval_trace --results eval/results/trace_run01.jsonl --case-id rules_001 --source-id "정답 원문의 source_id" --chunk-row-id "정답 청크의 DB id" --quote "정답 근거 원문 구절"
```

원문 전체의 청크를 보려면 `--chunk-row-id`를 생략합니다. `--chunk-id`, `--chunk-index`, `--dataset`,
`--department-id`도 필터로 사용할 수 있습니다. 청크 순번만으로는 원문을 식별할 수 없으므로
`--source-id` 또는 전역 청크 행 ID인 `--chunk-row-id`가 필요합니다.
정답 라벨에 ID가 없으면 검수한 정답 문서와 실제 적재 자료의 ID를 먼저 연결해야 합니다. URL만으로 서로 다른 첨부를 합치지 않습니다.

주요 진단 결과:

| 결과 | 해석 |
| --- | --- |
| `not_observed_in_candidates` | 정상·완전한 기록에서 후보로 관측되지 않음. DB 미적재인지 RPC 조건/상위 후보 수 때문인지는 별도 확인 필요 |
| `filtered_before_candidate_pool`, `not_admitted_to_candidate_pool` | 초기 검사 또는 후보 풀 진입 단계에서 빠짐 |
| `not_selected_after_rerank`, `excluded_by_source_cap` | 재정렬 후 순위 또는 출처당 제한에서 탈락 |
| `removed_during_request_merge` | 하위 질문에서 선택됐지만 전체 질문의 최종 병합에서 빠짐 |
| `evidence_not_shown` | 지정한 구절은 최종 원문에 있으나 실제 모델 전달 구간에 없음 |
| `shown_but_not_cited` | 지정한 구절을 모델에 전달했으나 해당 출처 번호를 답변이 인용하지 않음 |
| `shown_and_cited` | 지정 구절을 전달했고 해당 출처를 인용함. 사실 충족을 보장하지 않음 |
| `inconclusive_*`, `trace_not_captured` | 채널 장애·기록 한도·재정렬 이상 또는 구버전 결과로 판단 불가 |

`--quote`는 공백 차이만 정규화한 원문 구절 비교입니다. 인용이 없더라도 바르게 바꾸어 썼을 수 있고,
인용이 있어도 다른 사실을 답했을 수 있습니다. 실제 생성 중 사실 누락은 기존 사실 체크리스트와 답변을 함께 검토해야 합니다.
직접 공지 목록/강의계획서 전용 경로는 반환 결과와 최종 인용을 기록하며 해당 전용 조회 내부의 모든 후보를 추적하지는 않습니다.

## 검증 상태와 명령

사용자의 실행 원칙에 따라 테스트·서버 실행·DB 조회·실제 검색·LLM·품질 재측정·프런트엔드 빌드를 실행하지 않았습니다.
애플리케이션을 import하지 않는 AST 문법 검사와 `git diff --check`만 수행했습니다.
다음 명령은 사용자가 프로젝트 루트에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_retrieval_trace.py backend/tests/test_retrieval_parallel.py backend/tests/test_retrieval_failures.py backend/tests/test_ask_sources.py backend/tests/test_evaluation_evidence.py backend/tests/test_evidence_context.py backend/tests/test_question_flow.py backend/tests/test_syllabus.py backend/tests/test_notice_listing.py eval/tests/test_inspect_retrieval_trace.py eval/tests/test_eval_ask.py
npm --prefix frontend run lint -- app/lib/api.ts
npm --prefix frontend run build
```
