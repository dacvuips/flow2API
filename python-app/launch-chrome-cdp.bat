@echo off

setlocal enabledelayedexpansion

cd /d "%~dp0"



echo ========================================

echo  Flow2API - Mo 1 Chrome profile (Playwright)

echo  Cau hinh profile/port tren Dashboard

echo ========================================

echo.



if not exist .venv (

  echo LOI: Chay install.bat truoc.

  exit /b 1

)

call .venv\Scripts\activate.bat

set FLOW2API_CHROME_START_MINIMIZED=1



echo [1/3] Dong Chrome cu (bat buoc)...

python -c "from flow2api.services.system_ops import ensure_chrome_fully_closed; ok=ensure_chrome_fully_closed(); print('OK' if ok else 'CANH BAO: van con chrome.exe'); raise SystemExit(0 if ok else 1)"

if errorlevel 1 (

  echo   Dong Chrome thu cong ^(Task Manager -^> chrome.exe^) roi chay lai.

  pause

  exit /b 1

)



echo [2/4] Kiem tra profile CDP (chi dong bo lan dau)...

python -c "import os; from flow2api.services.system_ops import ensure_flow_launch_script, get_playwright_flow_chrome_profile, get_playwright_flow_cdp_port, ensure_cdp_profile_ready; force=os.environ.get('FLOW2API_CDP_FORCE_SYNC','').strip().lower() in ('1','true','yes','on'); p=get_playwright_flow_chrome_profile(); c=get_playwright_flow_cdp_port(); ud, synced=ensure_cdp_profile_ready(p, force=force); print(f'Profile: {p} @ CDP {c}'); print('Da dong bo profile (lan dau)' if synced else 'Dung profile CDP da co — giu session dang nhap'); print(ensure_flow_launch_script())"

if errorlevel 1 exit /b 1



echo [3/4] Mo profile Flow...

python launch-flow-profile.py

if errorlevel 1 exit /b 1

echo.



for /f "delims=" %%C in ('python -c "from flow2api.services.system_ops import get_playwright_flow_cdp_port; print(get_playwright_flow_cdp_port())"') do set "FLOW_CDP=%%C"

echo [4/4] Cho CDP port %FLOW_CDP% ^(co the mat 30-90 giay, Chrome da mo la binh thuong^)...



set "FLOW2API_CDP_WAIT_PORT=%FLOW_CDP%"

set "FLOW2API_CDP_SKIP_RELAUNCH=1"

set "OK=0"

python wait-cdp-port.py

if errorlevel 1 (

  echo.

  echo   [LOI] CDP port %FLOW_CDP% chua san sang.

  echo   Kiem tra: http://localhost:%FLOW_CDP%/json/version

) else (

  set "OK=1"

)



echo.

if "!OK!"=="1" (

  echo Xong. CDP OK — chay run-playwright.bat

  echo Luu y: Chrome CDP la profile rieng (Chrome 136+). Dang nhap Google/Flow MOT LAN trong Chrome nay.

  echo        Lan sau giu session — khong mo Chrome icon desktop.

) else (

  echo CANH BAO: CDP port %FLOW_CDP% chua OK.

  echo KHONG mo Chrome bang icon desktop — chi dung script nay hoac Dashboard.

)

echo.

pause

