#Requires -RunAsAdministrator
$nssm = "C:\ProgramData\chocolatey\bin\nssm.exe"
Write-Host "Setting PLAYWRIGHT_BROWSERS_PATH..."
& $nssm set SeatBot AppEnvironmentExtra "PLAYWRIGHT_BROWSERS_PATH=C:\Users\missk\AppData\Local\ms-playwright"
& $nssm get SeatBot AppEnvironmentExtra
Write-Host "Restarting SeatBot..."
& $nssm restart SeatBot
Start-Sleep 4
Get-Service SeatBot | Format-List Name,Status,StartType
& $nssm status SeatBot
Write-Host "Tail log:"
Get-Content "D:\document\Projects\Library-Seat-Reservation\logs\seatbot_nssm.log" -Tail 20
