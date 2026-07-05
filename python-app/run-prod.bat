@echo off
cd /d "%~dp0"

if not exist .venv (
  echo Chay install.bat truoc.
  goto :end
)

call .venv\Scripts\activate.bat
set FLOW2API_FRONTEND=%~dp0..\frontend
set FLOW2API_RELOAD=0
rem Production / Cloudflare Tunnel: link public co dinh (tunnel header van uu tien hon neu khac)
set FLOW2API_PUBLIC_BASE_URL=https://flow2.viettheo.site

call "%~dp0check-ports.bat"
if errorlevel 1 goto :end

echo Flow2API agent (production) - Ctrl+C de dung
echo.

python run.py

:end
echo.
echo Nhan phim bat ky de dong cua so CMD...
pause >nul
