@echo off
cd /d "%~dp0"

rem Double-click lai: dong CMD agent cu (theo title) + dung server, giu cua so moi
echo Dang dung instance cu (neu co)...
rem KHONG dung /T — tranh kill Chrome CDP ChatGPT/Flow (con cua python).
taskkill /F /FI "WINDOWTITLE eq Flow2API-Agent" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Flow2API-Agent*" >nul 2>&1

rem Danh dau cua so nay TRUOC stop — stop.bat se khong kill title *STARTING*
title Flow2API-Agent-STARTING

rem PID cua CMD hien tai — stop.bat khong duoc dong cua so nay
set "FLOW2API_STOP_EXCLUDE_PID="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "try { (Get-CimInstance Win32_Process -Filter ('ProcessId='+$PID)).ParentProcessId } catch { '' }"`) do set "FLOW2API_STOP_EXCLUDE_PID=%%i"
call "%~dp0stop.bat"
timeout /t 2 /nobreak >nul

rem Dat title chinh thuc — lan run ke tiep se tim thay va dong
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
