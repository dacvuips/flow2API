@echo off
cd /d "%~dp0"

if not exist .venv (
  echo Chay install.bat truoc.
  goto :end
)

call .venv\Scripts\activate.bat
set FLOW2API_FRONTEND=%~dp0..\frontend
set FLOW2API_RELOAD=0
rem Queue song song (mac dinh 1) va cach giua task (giay):
rem set FLOW2API_MAX_CONCURRENT=2
rem set FLOW2API_TASK_STAGGER_S=3

call "%~dp0check-ports.bat"
if errorlevel 1 goto :end

echo Flow2API agent - Ctrl+C de dung server
echo.

python run.py

:end
echo.
echo Nhan phim bat ky de dong cua so CMD...
pause >nul
