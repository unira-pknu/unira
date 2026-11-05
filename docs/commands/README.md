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
