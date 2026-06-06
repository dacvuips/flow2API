@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
  echo Can chay: cai Python 3.11+ tu python.org
  exit /b 1
)

if not exist .venv (
  echo Tao virtualenv...
  py -3 -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Cai dat xong. Chay agent:
echo   run.bat
echo.
echo Admin: http://127.0.0.1:1994/admin  (mac dinh admin / admin)
echo Dashboard: http://127.0.0.1:1994/
echo.
pause
