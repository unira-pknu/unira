# 학교 서버 운영 (운영 백엔드)

2026-10-27부터 운영 백엔드는 학교 GPU 서버(RTX 3080, Windows)에서 돈다. 화면(Vercel)의
`BACKEND_URL`이 이 서버의 ngrok 고정 주소를 가리킨다. Hugging Face Space 백엔드는 예비로 남아 있고
`main`에 백엔드 변경이 머지되면 자동 배포된다(`.github/workflows/deploy-backend-hf.yml`).
**학교 서버는 자동 배포되지 않는다** — 아래 순서로 직접 반영한다.

서버 주소·원격 접속 정보·`.env` 값은 공개 저장소에 적지 않는다(팀 내부 메모 참고).

## 구성

| 항목 | 위치·이름 |
| --- | --- |
| 백엔드 코드 | `D:\unira-backend` (운영 전용 clone, `main`) |
| 가상환경 | `D:\unira-backend\backend\.venv` (Python 3.12, torch CUDA) |
| 설정 | `D:\unira-backend\backend\.env` (`EMBEDDING_DEVICE=cuda`, `RERANKER_DEVICE=cuda`) |
| 실행 스크립트 | `run_backend.ps1`, `run_ngrok.ps1` — 꺼지면 10초 뒤 다시 켜는 무한 반복 |
| 로그 | `D:\unira-backend\logs\backend_날짜.log`·`ngrok_날짜.log` — 하루 한 파일, 30일 지난 파일은 자동 삭제(`write_daily_log.ps1`, 원본 `backend/deploy/`) |
| 자동 시작 | 작업 스케줄러 `unira-backend`, `unira-ngrok` (부팅 시, 관리자 권한) |

크롤링 작업 폴더(`D:\Github\campus-rag\campus-rag`)와는 별개다. 백엔드 반영 때 그 폴더와
크롤링 창은 건드리지 않는다.

## 코드 반영 순서

1. **새 코드 받기** — 일반 PowerShell

   ```powershell
   cd D:\unira-backend
   git pull
   ```

   `backend/requirements.txt`가 바뀌었으면 패키지도 설치한다.

   ```powershell
   .\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
   ```

2. **백엔드 다시 켜기** — **관리자** PowerShell (일반 창에서는 관리자 권한으로 돈 프로세스가 보이지 않아 꺼지지 않는다)

   ```powershell
   # 끌 대상 확인: uvicorn app.main:app 두 줄(venv 실행기 + 본체)만 나와야 한다
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*uvicorn*" } | Select-Object ProcessId, CommandLine

   # 끄기 → 실행 스크립트가 10초 뒤 새 코드로 다시 켠다
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*uvicorn*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "꺼짐: $($_.ProcessId)" }
   ```

   `uvicorn`이 들어간 python만 끄므로 크롤링 python은 영향이 없다. 다시 켜지는 1분 정도는 질문이 실패할 수 있다.

3. **확인** (1분 뒤)

   ```powershell
   Get-Content "D:\unira-backend\logs\backend_$(Get-Date -Format yyyy-MM-dd).log" -Tail 300 | Select-String "startup complete|warmup"
   ```

   오늘 시각의 `Backend startup complete.` 다음에 `warmup: search rows=…`가 보이면 새 코드로 켜진 것이다.
   밖에서는 ngrok 주소의 `/health`가 200, `/docs`가 404여야 한다.

## 작업 폴더의 미커밋 수정 반영

2026-11-09 복구 작업의 검색 확인에는 scoped lexical 필터 전달과 모호한 질문 확인 경로가
반영된 코드가 필요하다. 아직 커밋하지 않은 작업 폴더 수정은 운영 clone의 `git pull`에
포함되지 않는다. 먼저 프로젝트 루트에서 아래 테스트를 직접 실행한다.

```powershell
.\.venv\Scripts\python.exe -m pytest scripts/tests backend/tests -q
if ($LASTEXITCODE -ne 0) { throw "테스트 실패: 운영 반영을 중단합니다." }
```

이번 변경 파일을 검토한 뒤 아래 명령으로 이전 운영 파일을 백업하고 복사한다.
이 명령은 환경설정·가상환경을 복사하지 않으며 프로세스를 재시작하지 않는다.

```powershell
$projectRoot = (Get-Location).Path
$installedBackend = "D:\unira-backend\backend"
$backendFiles = @("generation.py", "query_plan.py", "retrieval.py", "routers\ask.py", "source_scope.py", "syllabus.py")
$backendBackup = Join-Path $projectRoot ("var\recovery\backend_copy_" + (Get-Date -Format "yyyyMMdd_HHmmss"))

foreach ($file in $backendFiles) {
    $source = Join-Path $projectRoot ("backend\app\" + $file)
    $target = Join-Path $installedBackend ("app\" + $file)
    if (!(Test-Path -LiteralPath $source -PathType Leaf) -or !(Test-Path -LiteralPath $target -PathType Leaf)) {
        throw "작업/운영 파일을 찾지 못했습니다: $file"
    }
    $backup = Join-Path $backendBackup $file
    New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
    Copy-Item -LiteralPath $target -Destination $backup -ErrorAction Stop
}
foreach ($file in $backendFiles) {
    $source = Join-Path $projectRoot ("backend\app\" + $file)
    $target = Join-Path $installedBackend ("app\" + $file)
    Copy-Item -LiteralPath $source -Destination $target -ErrorAction Stop
    if ((Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $target).Hash) {
        throw "운영 복사 해시 불일치: $file"
    }
}
Write-Host "이전 운영 파일 백업: $backendBackup"
```

이후 위의 **백엔드 다시 켜기**와 **확인** 절차를 실행한다. HTTP 서비스는 재시작 전까지
이전 코드를 사용할 수 있다. 파일 복사 성공을 운영 요청의 검증 성공으로 표시하지 않는다.
`019_scoped_lexical_search.sql`의 3인자 RPC도 필요하며, 기존 적용 이력과 실제 오류를 보고
판단한다. 기존 2인자 RPC를 삭제하거나 DB 시간 제한을 일괄 늘리지 않는다.
적재·검색 재개 명령은 [복구 작업 실행 순서](../../scripts/vectorization/README.md#중단된-복구-작업의-실행-순서)에 있다.

### 2026-11-09 검색 복구 변경

`020_scoped_lexical_source_bitmap.sql`은 학과 RAG 검색에서 대상 출처 ID와
본문 FTS 인덱스를 함께 사용할 수 있게 한다. 기존 2인자 RPC, 데이터, 인덱스,
시간 제한을 변경하지 않는다. 다른 계열에는 기존 계획을 유지한다. 전체 계열의
출처 ID 비트맵을 먼저 만드는 방식은 대표 홈페이지 검색을 느리게 할 수 있다.
정확한 문서 식별자인 `original_source_slug`가 있으면 `ingestion_dataset`과 함께
출처 후보 단계에 적용한다. 교수·학기 등 청크별로 달라질 수 있는 조건은 기존처럼
출처·청크 메타데이터 병합 후 검사한다.

검색 코드는 `2026학년도`의 접미사를 보존하고, 교류 질문의 학교·학년도 조건을
유지한다. 재순위화 전에 키워드 후보에 제한된 자리를 배정하며, 학과 제목 조회에도
출처 메타데이터 조건을 전달한다. 같은 공지 URL에 딸린 첨부파일·ZIP 멤버는
출처 ID별로 중복 제거와 청크 수 제한을 적용한다. 질문에 길이가 충분한 첨부파일의
전체 이름이 명시되면 해당 문서 후보를 먼저 선택하고 내부 청크를 재순위화한다.

운영 파일 교체·재시작 후 `/health`와 시작 로그를 확인한다. `/health` 성공이나
워밍업 결과 수만으로 모든 검색 RPC의 성공을 판정하지 않는다. 복구 검증은 내부
검색 실패 경고도 실패로 처리하며, 시간 초과가 있으면 원인을 확인하고 재검증한다.

## 학습 후보 다운로드에서 Not Found가 뜰 때

Vercel 화면이 갱신되어도 학교 서버의 백엔드는 자동 갱신되지 않습니다.
다운로드 버튼은 `/api/admin/feedback/training-candidates`를 거쳐 학교 서버의
`/admin/feedback/training-candidates`를 호출합니다. 서버의 `app/routers/admin.py`에
해당 라우트가 없으면 404가 발생합니다. `app/feedback_training.py`도 함께 필요합니다.
프록시는 이 404를 서버 업데이트 안내와 HTTP 503으로 변환하며 기존 관리자 인증은 유지합니다.

2026-11-11 최초 점검에서는 관리자 라우트 미반영, `feedback_training.py` 부재를 확인했습니다.
이후 사용자의 명시적 실행 요청으로 아래 두 파일 반영과 재시작을 완료했습니다. 실행 결과는 이 절 마지막에 있습니다.

프로젝트 루트(`D:\Github\campus-rag\campus-rag`)에서 먼저 테스트합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_admin.py eval/tests/test_feedback_training.py
if ($LASTEXITCODE -ne 0) { throw "테스트 실패: 운영 반영을 중단합니다." }
```

학습 후보 기능에 필요한 두 파일만 백업 후 반영합니다. 앞선 검색 품질 변경 파일이나
`.env`·가상환경·DB는 이 복사에 포함하지 않습니다. 새 파일은 기존 운영 파일이 없어도 복사합니다.

```powershell
$feedbackProject = 'D:\Github\campus-rag\campus-rag'
$feedbackInstalledApp = 'D:\unira-backend\backend\app'
$feedbackFiles = @('routers\admin.py', 'feedback_training.py')
$feedbackBackup = Join-Path $feedbackProject ('var\recovery\feedback_backend_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))

if (!(Test-Path -LiteralPath (Join-Path $feedbackInstalledApp 'main.py') -PathType Leaf)) {
    throw '운영 백엔드 app 폴더를 찾지 못했습니다.'
}
foreach ($relative in $feedbackFiles) {
    $source = Join-Path $feedbackProject ('backend\app\' + $relative)
    if (!(Test-Path -LiteralPath $source -PathType Leaf)) { throw "작업 파일 없음: $relative" }
}
foreach ($relative in $feedbackFiles) {
    $target = Join-Path $feedbackInstalledApp $relative
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        $backup = Join-Path $feedbackBackup $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
        Copy-Item -LiteralPath $target -Destination $backup -ErrorAction Stop
    }
}
# 새 라우트를 읽기 전에 그 라우트가 import하는 모듈을 먼저 반영합니다.
foreach ($relative in @('feedback_training.py', 'routers\admin.py')) {
    $source = Join-Path $feedbackProject ('backend\app\' + $relative)
    $target = Join-Path $feedbackInstalledApp $relative
    Copy-Item -LiteralPath $source -Destination $target -ErrorAction Stop
    if ((Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $target).Hash) {
        throw "운영 복사 해시 불일치: $relative"
    }
}
Write-Host "운영 파일 반영 완료. 이전 파일 백업: $feedbackBackup"
```

이후 위의 **백엔드 다시 켜기** 절차를 관리자 PowerShell에서 실행하고, 새 시작 로그를 확인합니다.
Vercel의 `BACKEND_URL`이 이 학교 서버를 가리키는지도 확인합니다. Hugging Face 예비 서버를
가리킨다면 학교 서버 파일 복사는 그 예비 서버를 갱신하지 않으므로 해당 배포를 따로 반영해야 합니다.

브라우저에서 학습 후보 다운로드를 다시 시도합니다. 정상 로그인한 관리자에게 200과 후보 목록이
반환되는지 확인합니다. 토큰 없이 URL만 열었을 때 401이 나오면 라우트는 존재하는 것이며,
404이면 연결한 서버 또는 실행 중인 코드가 아직 다릅니다. 피드백이 없으면 빈 목록이 정상입니다.

프록시 메시지 변경을 Vercel에 반영하기 전에는 아래 검증을 사용자가 실행합니다.

```powershell
npm --prefix frontend run lint -- app/api/admin/feedback/training-candidates/route.ts
npm --prefix frontend run build
```

이 기능 반영에는 추가 DB 마이그레이션이 필요하지 않습니다.

2026-11-11 사용자 실행 승인 후 다음 작업을 완료했습니다.

- 관리자·학습 데이터 테스트: **37개 통과** (`backend/tests/test_admin.py`, `eval/tests/test_feedback_training.py`).
- 운영 `feedback_training.py`, `routers/admin.py` 복사와 두 파일의 작업/운영 SHA-256 일치 확인.
  기존 관리자 파일 백업: `var/recovery/feedback_backend_20261111_151423_403/routers/admin.py`.
- 경로를 확인한 작업 스케줄러 `unira-backend` 중지·시작, 새 `Backend startup complete.` 확인.
- 로컬 운영 `/health` **200**, 인증 없는 `/admin/feedback/training-candidates?limit=1` **401** 확인.
  이는 새 경로 등록과 인증 요구 확인이며, 관리자 권한으로 DB 데이터를 다운로드한 검증은 아닙니다.
- 프론트엔드 관련 3개 파일 ESLint: 오류 0, 기존 불필요한 eslint-disable 경고 1.
- 프론트엔드 프로덕션 빌드 성공. 빌드 라우트에 `/api/admin/feedback/training-candidates` 포함 확인.

시스템에 Node.js가 없어 공식 Node.js 22 LTS(v22.23.4)를 SHA-256 검증 후
`var/tools/node-v22.23.4-win-x64`에 준비했습니다. `npm ci`로 잠금 파일 그대로 설치했으며
`package.json`, `package-lock.json`, 시스템 PATH는 변경하지 않았습니다. 이 Node.js를 다시 사용할 때는
현재 PowerShell 세션의 PATH 앞에 해당 폴더를 추가합니다.

검색 품질 변경 파일의 운영 복사, DB 변경, Vercel 배포, 로그인한 관리자의 실제 다운로드는 수행하지 않았습니다.

## 되돌리기 (학교 서버가 멈췄을 때)

Vercel 프로젝트 → Settings → Environment Variables → `BACKEND_URL`을 Hugging Face Space 주소로
바꾸고 Redeploy. HF는 CPU라 답이 느리지만(약 20초) 동작한다. 복구 후 다시 ngrok 주소로 돌린다.

## 주의

- 서버를 재부팅하면 작업 스케줄러가 백엔드·ngrok을 다시 켠다(부팅 후 확인 필요).
- 백엔드 로그에는 학생 질문 원문이 남는다. 공용 서버라 30일만 보관한다(2026-11-03 결정).
- 관리자 PowerShell 창이나 원격 접속을 닫아도 백엔드는 계속 돈다(작업 스케줄러가 실행).
