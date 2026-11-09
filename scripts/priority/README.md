# 문서 우선순위

수집기 이동 후 우선순위 코드만 남아 있던 `scripts/ce`, `scripts/rule`, `scripts/rag`를 이 패키지로 모았습니다.
학과 공통 정책의 이름은 `department`입니다.

| 패키지 | 용도 | 실행 모듈 |
|---|---|---|
| `department` | 학과 문서와 규정·본교 자료의 관련성을 이용한 우선순위 | `scripts.priority.department.update_priorities` |
| `rule` | 규정의 권위·학생 관련성·개정 시점·출처 품질에 따른 우선순위 | `scripts.priority.rule.update_priorities` |
| `rag` | 본교 공지·일반 자료·강의계획서의 우선순위 | `scripts.priority.rag.update_main_priorities` |

각 `priority.py`는 계산 정책을, `update_priorities.py` 또는 `update_main_priorities.py`는 DB 조회·반영과
로컬 인덱스 미리보기를 담당합니다. 모듈을 import하는 것만으로 DB 갱신을 수행하지 않습니다.

프로젝트 루트의 Python 3.12 환경에서 실행합니다. 옵션 확인:

```powershell
.\.venv\Scripts\python.exe -m scripts.priority.department.update_priorities --help
.\.venv\Scripts\python.exe -m scripts.priority.rule.update_priorities --help
.\.venv\Scripts\python.exe -m scripts.priority.rag.update_main_priorities --help
```

DB 조회 후 미리보기는 `--dry-run`을 사용합니다. 옵션 없이 실행하면 실제 DB 우선순위를 갱신합니다.
학과 DB 명령은 활성 `rag_sources` 전체를 대상으로 합니다. 특정 학과의 로컬 벡터만 미리 보려면 인덱스 경로를 지정합니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.priority.department.update_priorities --from-index --dry-run --index-path vectorized/ce/index.jsonl
.\.venv\Scripts\python.exe -m scripts.priority.rag.update_main_priorities --dataset all --dry-run
```

`ce` 등 수집 데이터셋 별칭은 원본·벡터·DB 문서 ID에 쓰이므로 유지합니다. 학과 미리보기 기본 인덱스도 기존
`vectorized/ce/index.jsonl`을 사용합니다. 기존 우선순위 상세 기록과의 호환을 위해 계산 결과의 `latest_ce_date` 키와
기존 계산 정책 식별자는 유지하며, 함수명은 `calculate_department_priority`와 `update_department_priorities`를 사용합니다.

저장소 내부 import, 적재 후 우선순위 갱신, 복구 작업, 파이프라인, CI와 문서 명령은 새 경로를 사용합니다.
이전 패키지 경로의 호환 파일은 두지 않습니다.

변경 검증은 사용자가 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest scripts\tests backend\tests eval\tests -q -p no:cacheprovider
```
