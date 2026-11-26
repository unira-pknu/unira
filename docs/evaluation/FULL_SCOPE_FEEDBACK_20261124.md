# 전체 범위 100문항 개선 기록

2026년 11월 24일 첫 평가에서 49/100문항이 통과했다. 답변 가능한 질문의 통과는 44/95였으며 미답변 40건, 부분 정답 7건, 오답 1건, 검색 오류 3건을 확인했다. 문항별 원문과 기준 답은 [평가 세트](../../eval/cases/full_scope_20261124.jsonl)에 있으며 최초 응답과 재실행 결과는 실행별 JSONL로 보존한다. 기준 근거가 최종 답변 입력에 없던 44건은 기준 문서 ID 또는 URL의 불일치를 뜻한다. DB 적재 여부를 판정하려면 원문 식별자로 별도 조회해야 한다.

| 문제 | 반영한 작업 | 재평가 기준 |
|---|---|---|
| 키워드 검색 | 포털·성적·이수학점의 일반 어휘가 구체적인 질문을 덮었던 사례에 복합어 검색을 적용했다. [022 SQL](../../supabase/migrations/022_bound_search_candidates.sql)은 빈 검색을 건너뛰고, 활성 문서 후보 중 제한된 범위를 평가한다. 제목 검색에도 인덱스를 추가했다. | 503 세 문항을 1개 동시 실행으로 재검증한다. 이어서 100문항에서 RPC 오류 문항 수, 503, 지연 시간, 정답 근거 확보를 함께 비교한다. |
| 강의계획서 | 단일 학수번호와 분반을 질문에서 읽어 전용 경로에 유지한다. 코드가 있을 때 날짜·평가 질문 문구는 과목명 일치 조건에서 제외한다. 제목·파일명·메타데이터의 학기와 분반을 읽고 평가표·주차·상담시간을 우선한다. | 12개 모두 확인한다. 특히 일반물리학실험 학점, 조선시대의역사와문화 중간고사 주차, 비교문화론 출결, 전공탐색과진로설계 P/F를 지정 분반으로 확인한다. |
| 정적 메뉴 | 본교의 공식 메뉴 제목과 소속 학과의 메뉴 제목을 사용해 좁혀 찾고 해당 문서의 본문 근거를 후보에 보존한다. | 대연·용당 오시는 길, 게시물 부착, ISIC, 증명서, 학적부 정정·재입학·국가장학 기준, 전공 연혁과 사무실 안내가 정확한 공식 메뉴를 인용하는지 확인한다. |
| 대상·시점·첨부 | 질문이 지정한 공지 연도를 제목 후보 순위에 반영한다. 정규 학점교류와 별도 사업, 하계와 동계, 명시된 규정 개정판을 구분한다. | 충남대 학생 신청 마감, 인천대 하계 수강료, 2025 졸업사정·WEST, 2026 도서관 세칙의 출처 시점과 주체를 각각 확인한다. |
| 표·조항 | 식단 질문에 명시된 날짜와 식당을 검사한다. 식당별 표 제목과 날짜 헤더를 함께 제시하고, 규정은 같은 문서 안에서 조항과 정의 본문에 해당하는 후보를 다시 뽑는다. | 라일락 화요일 점심, 목포대 학생 접수와 대학 추천기한, 학생설계전공 정의, 직장어린이집 입원 2순위의 실제 근거 문장을 확인한다. |
| 교수 상세 프로필 | 공개 프로필의 담당 교과목을 학기별 개설 강의와 구분한다. 이름이 확인된 학과 상세 페이지를 읽고, 공식 대학 도메인의 명시된 홈페이지를 답변 근거로 제공한다. | 조규영 담당 교과목, 정욱교 연구실·공개 이메일, 김보성 연구실 위치, 황재열 홈페이지를 확인한다. 동명이인 및 여러 교수의 정보를 합치지 않는지도 확인한다. |

## 재평가

저장소 루트에서 단위 테스트를 실행한다.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_syllabus.py backend/tests/test_query_plan.py backend/tests/test_faculty.py backend/tests/test_lexical_query.py backend/tests/test_title_anchor.py backend/tests/test_evidence_context.py backend/tests/test_generation.py backend/tests/test_source_scope.py backend/tests/test_retrieval_failures.py -q
```

021까지 적용된 검증 DB에 새 SQL을 적용한 뒤, 검증용 `backend/.env`가 그 DB를 가리키는 환경에서 같은 100문항을 실행한다. `DATABASE_URL`은 해당 검증 DB의 연결 문자열이고 아래 SQL 명령에는 `psql`이 필요하다.

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f supabase/migrations/022_bound_search_candidates.sql
.\.venv\Scripts\python.exe -m eval.tools.eval_local --questions eval/cases/full_scope_20261124.jsonl --case-id full_syllabus_001 --case-id full_academic_012 --case-id full_rule_002 --output eval/results/full_scope_after_022_503.jsonl --workers 1 --max-calls 12 --data-snapshot "validation-db-022"
.\.venv\Scripts\python.exe -m eval.tools.eval_local --questions eval/cases/full_scope_20261124.jsonl --output eval/results/full_scope_after_022.jsonl --workers 3 --max-calls 300 --data-snapshot "validation-db-022"
```

첫 실행과 이전 실행의 결과를 문항 ID로 대조하고 동일한 사실·출처 기준으로 검토한다. 정답, 부분 정답, 핵심 답변 누락, 오답, 처리 오류를 별도로 집계한다. 코드·모델·DB 버전이 다르면 실행 기록에 표시한다. 미통과 문항은 다음 확인 지점과 함께 남긴다.

- 기대 문서가 후보에 없으면 운영 적재·활성 상태를 원문 ID로 확인한 뒤 검색 필터와 랭킹을 점검한다.
- 기대 문서는 검색됐지만 필요한 표 행·조항이 전달되지 않았다면 원문 및 사전처리 청크를 비교한다.
- 사실이 모델에 전달된 뒤 답변에서 빠졌거나 다른 사업·연도가 인용되었다면 실제 인용문을 기준으로 답변 검증을 점검한다.
- 원래 통과한 49문항과 확인 불가 5문항을 함께 대조한다. 확인 불가 5문항은 49문항에 포함되어 있다. 문항 일부를 재실행한 결과는 전체 실행 점수에 합치지 않는다.

이번 변경과 재평가 명령을 준비했다. 단위 테스트, SQL 적용·실행 계획 확인, 실제 모델 재평가와 운영 반영은 수행하지 않았다.
