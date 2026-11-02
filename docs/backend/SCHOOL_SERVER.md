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
| 실행 스크립트 | `run_backend.ps1`, `run_ngrok.ps1` — 꺼지면 10초 뒤 다시 켜는 무한 반복, 로그는 `backend.log`·`ngrok.log` |
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
   Get-Content D:\unira-backend\backend.log -Tail 300 | Select-String "startup complete|warmup"
   ```

   오늘 시각의 `Backend startup complete.` 다음에 `warmup: search rows=…`가 보이면 새 코드로 켜진 것이다.
   밖에서는 ngrok 주소의 `/health`가 200, `/docs`가 404여야 한다.

## 되돌리기 (학교 서버가 멈췄을 때)

Vercel 프로젝트 → Settings → Environment Variables → `BACKEND_URL`을 Hugging Face Space 주소로
바꾸고 Redeploy. HF는 CPU라 답이 느리지만(약 20초) 동작한다. 복구 후 다시 ngrok 주소로 돌린다.

## 주의

- 서버를 재부팅하면 작업 스케줄러가 백엔드·ngrok을 다시 켠다(부팅 후 확인 필요).
- `backend.log`에는 학생 질문 원문이 남는다. 공용 서버이므로 로그 보관 기간을 정해 지운다.
- 관리자 PowerShell 창이나 원격 접속을 닫아도 백엔드는 계속 돈다(작업 스케줄러가 실행).
