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

call :kill_port %HTTP_PORT%
if not "%HTTP_PORT%"=="1993" call :kill_port 1993
if not "%HTTP_PORT%"=="1994" call :kill_port 1994
call :kill_port %WS_PORT%

powershell -NoProfile -Command ^
  "$root = (Resolve-Path '%CD%').Path; " ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | " ^
  "Where-Object { $_.CommandLine -and $_.CommandLine -like '*run.py*' -and $_.CommandLine -like \"*$root*\" } | " ^
  "ForEach-Object { Write-Host ('Dung python PID ' + $_.ProcessId + ' (run.py)'); " ^
  "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

if "%KILLED%"=="0" (
  echo Khong tim thay server Flow2API dang chay tren port %HTTP_PORT% / %WS_PORT%.
) else (
  echo Da dung server.
)

echo.
goto :end

:kill_port
set "PORT=%~1"
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr /R /C:":%PORT% .*LISTENING"') do (
  if not "%%P"=="0" (
    echo Dung PID %%P ^(port %PORT%^)
    taskkill /PID %%P /F >nul 2>&1
    set "KILLED=1"
  )
)
exit /b 0

:end
endlocal
