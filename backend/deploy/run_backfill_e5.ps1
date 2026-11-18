# 학교 서버 작업 스케줄러(unira-backfill-e5)가 4시간마다 실행한다.
# 새로 적재됐거나 글이 바뀐 조각의 e5 벡터만 채운다(scripts/vectorization/backfill_e5.py).
# 채울 것이 없으면 모델을 불러오지 않고 바로 끝나고, 앞 실행이 아직 돌고 있으면 그냥 끝난다.
# 로그: D:\unira-backend\logs\backfill_e5_날짜.log (30일 보관)
$root = 'D:\unira-backend'
Set-Location $root
# 작업 스케줄러가 SYSTEM 계정으로 돌려도, 손으로 돌릴 때 받아 둔 모델(약 1GB)을 같이 쓴다.
$env:HF_HOME = 'C:\Users\Server\.cache\huggingface'
& "$root\backend\.venv\Scripts\python.exe" -m scripts.vectorization.backfill_e5 2>&1 |
    ForEach-Object { "$_" } |
    & "$root\write_daily_log.ps1" -Name backfill_e5
