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

rem Dung python run.py cua thu muc nay
powershell -NoProfile -Command ^
  "$root = (Resolve-Path '%CD%').Path; " ^
  "$exclude = 0; [void][int]::TryParse($env:FLOW2API_STOP_EXCLUDE_PID, [ref]$exclude); " ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | " ^
  "Where-Object { $_.CommandLine -and $_.CommandLine -like '*run.py*' -and $_.CommandLine -like \"*$root*\" } | " ^
  "ForEach-Object { Write-Host ('Dung python PID ' + $_.ProcessId + ' (run.py)'); " ^
  "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

rem Dong cua so CMD agent cu: title Flow2API-Agent, hoac CommandLine run.bat (tru CMD hien tai)
powershell -NoProfile -Command ^
  "$root = (Resolve-Path '%CD%').Path; " ^
  "$exclude = 0; [void][int]::TryParse($env:FLOW2API_STOP_EXCLUDE_PID, [ref]$exclude); " ^
  "Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" -ErrorAction SilentlyContinue | " ^
  "ForEach-Object { " ^
  "  if ($exclude -gt 0 -and $_.ProcessId -eq $exclude) { return } " ^
  "  $cmd = [string]$_.CommandLine; " ^
  "  $title = ''; try { $title = [string](Get-Process -Id $_.ProcessId -ErrorAction Stop).MainWindowTitle } catch {} " ^
  "  $byTitle = $title -like 'Flow2API-Agent*'; " ^
  "  $byCmd = $cmd -and ($cmd -like '*run.bat*' -or $cmd -like '*run-prod.bat*') -and $cmd -notlike '*stop.bat*' -and ($cmd -like ('*{0}*' -f $root) -or $cmd -like '*\\python-app\\*' -or $cmd -like '*/python-app/*'); " ^
  "  if (-not ($byTitle -or $byCmd)) { return } " ^
  "  Write-Host ('Dong CMD PID ' + $_.ProcessId + ' (run)'); " ^
  "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue " ^
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
