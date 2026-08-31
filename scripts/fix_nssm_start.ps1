#Requires -RunAsAdministrator
# 启动 SeatBot 服务并做启动后体检；现仅保留单一 .venv（旧 .venv2 已合并）。
param([switch]$UseVenv)

$nssm = "C:\ProgramData\chocolatey\bin\nssm.exe"
$wd = "D:\document\Projects\Library-Seat-Reservation"

# 兼容旧参数：无论是否带 -UseVenv，都确保指向唯一的 .venv
Write-Host "Ensuring SeatBot Application -> .venv ..."
& $nssm set SeatBot Application "$wd\.venv\Scripts\python.exe" 2>&1 | Write-Host

Write-Host "Starting SeatBot service..."
& $nssm start SeatBot 2>&1 | Write-Host
Start-Sleep 6

Get-Service SeatBot | Format-List Name, Status, StartType
& $nssm status SeatBot

$ok = $false
try {
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:8080/api/status" -UseBasicParsing -TimeoutSec 5
  $ok = ($r.StatusCode -eq 200)
} catch {}
Write-Host ("8080 /api/status: " + $(if ($ok) { "OK" } else { "FAIL" }))

Write-Host "--- stderr tail ---"
Get-Content "$wd\logs\seatbot_nssm_err.log" -Tail 8
if (-not $ok) {
  Write-Host "启动未成功：请把上面的 stderr tail 发给代理继续排查。"
}
