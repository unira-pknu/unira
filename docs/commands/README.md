# Commands

프로젝트 루트에서 실행하는 것을 기준으로 합니다.
프로젝트 가상환경의 Python을 직접 사용합니다.

```powershell
.\.venv\Scripts\python.exe --version
```

## 명령 문서

- [크롤러](CRAWLERS.md): 문서 마이그레이션, 본교·학과·규정 크롤링, 학과 registry 검증과 discovery
- [전체 전처리](../preprocessing/PREPROCESSING.md): 전체 수집 자료 점검·실행·재처리
- [개별 전처리 옵션](PREPROCESSING.md): 학과·공통 첨부파일·규정 전처리
- [벡터 DB와 RAG](VECTOR_DATABASE.md): 벡터화, Supabase 적재·검색, 우선순위, 파이프라인
- [평가](EVALUATION.md): 질문 세트를 이용한 API 평가
- [피드백 학습 데이터](../evaluation/FEEDBACK_TRAINING.md): 관리자 피드백 CLI 수집, 검수 결과 대조와 학습·평가 JSONL 생성
- [피드백 기반 AI 후보 생성](../evaluation/FEEDBACK_AI_CANDIDATES.md): 검색 원문에 근거한 수정 답변·관련 질문 초안 생성
- [로컬 HTML 검수 편집기](../../eval/tools/feedback_review_editor.html): 질문·수정 답변·근거·검수 상태 편집과 JSONL 저장
- [② 검수·데이터 변환 자동화](../evaluation/FEEDBACK_REVIEW_AUTOMATION.md): 실제·합성 질문 통합 검수, 자동 검사·보류 보고서와 승인 데이터 변환
- [③ 학습·비교 평가 자동화](../evaluation/FEEDBACK_TRAINING_AUTOMATION.md): 학습 제출 조건 검사, 작업 재개, 동일 자료의 두 모델 비교와 보고서
- [이후 학습 진행 계획](../evaluation/FEEDBACK_TRAINING_NEXT_STEPS.md): 추가 데이터·근거 확보부터 시범 학습, 비용 확인과 전체 평가까지의 순서

## 빠른 실행 흐름

### CE 전체 파이프라인

```powershell
.\.venv\Scripts\python.exe scripts\vectorization\pipelining.py --once
```

### 크롤러 설정 검증

```powershell
.\.venv\Scripts\python.exe scripts\crawlers\departments\cli.py validate
```

각 명령의 옵션, 출력 경로 및 운영 주의사항은 위 주제별 문서를 참고하세요.
