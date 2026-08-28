#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
$py = "D:\document\Projects\Library-Seat-Reservation\.venv\Scripts\python.exe"
$wd = "D:\document\Projects\Library-Seat-Reservation"
$nssm = "C:\ProgramData\chocolatey\bin\nssm.exe"

if (-not (Test-Path $py)) { Write-Error "python not found: $py"; exit 1 }
if (-not (Test-Path $nssm)) { Write-Error "nssm not found: $nssm"; exit 1 }

# 已存在则先卸载
$svc = Get-Service -Name SeatBot -ErrorAction SilentlyContinue
if ($svc) {
  Write-Host "Stopping existing SeatBot service..."
  & $nssm stop SeatBot 2>&1 | Write-Host
  Start-Sleep 2
  & $nssm remove SeatBot confirm 2>&1 | Write-Host
  Start-Sleep 2
}

Write-Host "Installing SeatBot service..."
& $nssm install SeatBot $py "-m seatbot --config config.yaml run"
if ($LASTEXITCODE -ne 0) { Write-Error "nssm install failed $LASTEXITCODE"; exit 1 }

& $nssm set SeatBot AppDirectory $wd
& $nssm set SeatBot DisplayName "SeatBot - Library Seat Reservation"
& $nssm set SeatBot Description "超星图书馆座位自动预约守护系统 - 14:00自动预约/每分钟tick签到签退"
& $nssm set SeatBot Start SERVICE_AUTO_START
& $nssm set SeatBot AppStdout "$wd\logs\seatbot_nssm.log"
& $nssm set SeatBot AppStderr "$wd\logs\seatbot_nssm_err.log"
& $nssm set SeatBot AppRotateFiles 1
& $nssm set SeatBot AppRotateOnline 1
& $nssm set SeatBot AppRotateSeconds 86400
& $nssm set SeatBot AppRotateBytes 10485760
& $nssm set SeatBot AppRestartDelay 10000

Write-Host "Starting SeatBot service..."
& $nssm start SeatBot
Start-Sleep 3
Get-Service SeatBot | Format-List Name,Status,StartType
& $nssm status SeatBot
Write-Host "Done. Logs: $wd\logs\seatbot_nssm.log"
