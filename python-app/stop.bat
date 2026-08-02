@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "HTTP_PORT=1994"
set "WS_PORT=1609"
if defined FLOW2API_HTTP_PORT set "HTTP_PORT=%FLOW2API_HTTP_PORT%"
if defined FLOW2API_EXT_WS_PORT set "WS_PORT=%FLOW2API_EXT_WS_PORT%"

echo.
echo Dang dung Flow2API server...
echo.

set "KILLED=0"
set "EXCLUDE_PID="
if defined FLOW2API_STOP_EXCLUDE_PID set "EXCLUDE_PID=%FLOW2API_STOP_EXCLUDE_PID%"

call :kill_port %HTTP_PORT%
if not "%HTTP_PORT%"=="1993" call :kill_port 1993
if not "%HTTP_PORT%"=="1994" call :kill_port 1994
call :kill_port %WS_PORT%

rem Dung python run.py cua thu muc nay (timeout tranh treo WMI)
powershell -NoProfile -Command ^
  "$ErrorActionPreference='SilentlyContinue'; " ^
  "$root = (Resolve-Path '%CD%').Path; " ^
  "$job = Start-Job { param($r) " ^
  "  Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | " ^
  "  Where-Object { $_.CommandLine -and $_.CommandLine -like '*run.py*' -and $_.CommandLine -like \"*$r*\" } | " ^
  "  ForEach-Object { $_.ProcessId } " ^
  "} -ArgumentList $root; " ^
  "if (Wait-Job $job -Timeout 8) { " ^
  "  foreach ($procId in @(Receive-Job $job)) { " ^
  "    if (-not $procId) { continue } " ^
  "    Write-Host ('Dung python PID ' + $procId + ' (run.py)'); " ^
  "    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue " ^
  "  } " ^
  "} else { Write-Host 'Bo qua quet python (timeout)'; Stop-Job $job } " ^
  "Remove-Job $job -Force -ErrorAction SilentlyContinue"

rem Chi dong CMD agent cu theo title Flow2API-Agent (KHONG match run.bat — tranh tu kill cua so dang mo)
rem Bo qua title *STARTING* = cua so run.bat hien tai
powershell -NoProfile -Command ^
  "$ErrorActionPreference='SilentlyContinue'; " ^
  "$exclude = 0; [void][int]::TryParse($env:FLOW2API_STOP_EXCLUDE_PID, [ref]$exclude); " ^
  "$job = Start-Job { " ^
  "  Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | ForEach-Object { $_.ProcessId } " ^
  "}; " ^
  "if (-not (Wait-Job $job -Timeout 8)) { Write-Host 'Bo qua quet CMD (timeout)'; Stop-Job $job; Remove-Job $job -Force; exit 0 }; " ^
  "$ids = @(Receive-Job $job); Remove-Job $job -Force; " ^
  "foreach ($procId in $ids) { " ^
  "  if ($exclude -gt 0 -and $procId -eq $exclude) { continue } " ^
  "  $title = ''; try { $title = [string](Get-Process -Id $procId -ErrorAction Stop).MainWindowTitle } catch { continue } " ^
  "  if (-not $title) { continue } " ^
  "  if ($title -notlike 'Flow2API-Agent*') { continue } " ^
  "  if ($title -like '*STARTING*') { continue } " ^
  "  Write-Host ('Dong CMD PID ' + $procId + ' (' + $title + ')'); " ^
  "  Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue " ^
  "}"

if "%KILLED%"=="0" (
  echo Da gui lenh dung server / CMD run.
) else (
  echo Da dung server + dong CMD run ^(neu co^).
)

echo.
goto :end

:kill_port
set "PORT=%~1"
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr /R /C:":%PORT% .*LISTENING"') do (
  if not "%%P"=="0" (
    if not "%%P"=="%EXCLUDE_PID%" (
      echo Dung PID %%P ^(port %PORT%^)
      taskkill /PID %%P /T /F >nul 2>&1
      set "KILLED=1"
    )
  )
)
exit /b 0

:end
endlocal
