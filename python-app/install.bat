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
python -m playwright install chrome

echo.
echo Kiem tra FFmpeg ^(watermark video gateway^)...
python -c "from flow2api.services.watermark_engine import ffmpeg_executable; p=ffmpeg_executable(); print('FFmpeg OK:', p)"
if errorlevel 1 (
  echo [CANH BAO] FFmpeg chua san sang. Cai lai: pip install imageio-ffmpeg
  echo            hoac dat FLOW2API_FFMPEG=duong\dan\ffmpeg.exe
) else (
  echo FFmpeg da san sang ^(imageio-ffmpeg trong venv hoac PATH^).
)

echo.
echo Cai dat xong. Chay agent:
echo   run.bat
echo.
echo Admin: http://127.0.0.1:1994/admin  (mac dinh admin / admin)
echo Dashboard: http://127.0.0.1:1994/
echo.
pause
