# Writes piped lines to <Dir>\<Name>_yyyy-MM-dd.log and, whenever the day changes,
# deletes this name's files older than KeepDays. Each line opens and closes the file,
# so old days can be removed while the backend keeps running.
# Used by the school server loop scripts (docs/backend/SCHOOL_SERVER.md):
#   ... 2>&1 | & D:\unira-backend\write_daily_log.ps1 -Name backend
[CmdletBinding()]
param(
    [Parameter(ValueFromPipeline = $true)]$Line,
    [Parameter(Mandatory = $true)][string]$Name,
    [string]$Dir = 'D:\unira-backend\logs',
    [int]$KeepDays = 30
)
begin {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $lastDay = ''
}
process {
    $day = Get-Date -Format 'yyyy-MM-dd'
    if ($day -ne $lastDay) {
        $lastDay = $day
        $cutoff = (Get-Date).AddDays(-$KeepDays)
        Get-ChildItem -Path $Dir -Filter "$($Name)_*.log" |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            Remove-Item -ErrorAction SilentlyContinue
    }
    Add-Content -LiteralPath (Join-Path $Dir "$($Name)_$day.log") -Value "$Line" -Encoding UTF8
}
