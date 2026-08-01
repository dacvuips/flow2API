@echo off
cd /d "%~dp0"

rem Double-click lai: dong CMD agent cu (theo title) + dung server, giu cua so moi
echo Dang dung instance cu (neu co)...
taskkill /F /T /FI "WINDOWTITLE eq Flow2API-Agent*" >nul 2>&1

rem PID cua CMD hien tai — stop.bat khong duoc dong cua so nay
for /f %%i in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter (\"ProcessId=$PID\")).ParentProcessId"') do set "FLOW2API_STOP_EXCLUDE_PID=%%i"
call "%~dp0stop.bat"
timeout /t 2 /nobreak >nul

rem Dat title SAU khi dong cua so cu — lan run ke tiep se tim thay
title Flow2API-Agent

if not exist .venv (
  echo Chay install.bat truoc.
  goto :end
)

call .venv\Scripts\activate.bat
set FLOW2API_FRONTEND=%~dp0..\frontend
set FLOW2API_RELOAD=1

call "%~dp0check-ports.bat"
if errorlevel 1 goto :end

echo Flow2API agent (dev/reload) - Ctrl+C de dung
echo Double-click run.bat/run-prod.bat lan nua se dong cua so nay va mo moi.
echo.

python run.py

:end
echo.
echo Nhan phim bat ky de dong cua so CMD...
pause >nul
