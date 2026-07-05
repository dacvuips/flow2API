@echo off

cd /d "%~dp0"



if not exist .venv (

  echo Chay install.bat truoc.

  goto :end

)



call .venv\Scripts\activate.bat

set FLOW2API_FRONTEND=%~dp0..\frontend
set FLOW2API_RELOAD=0
rem Local dev: link media/video http://127.0.0.1:1994 (tunnel van override khi co request qua domain)
set FLOW2API_PUBLIC_BASE_URL_DEFAULT=http://127.0.0.1:1994

set FLOW2API_PLAYWRIGHT_ENABLED=1

set FLOW2API_UI_AUTOMATION=1

set FLOW2API_UI_PREP_ONLY=1

set FLOW2API_UI_ACTION_DELAY_MIN_S=0

set FLOW2API_UI_ACTION_DELAY_MAX_S=1.5

set FLOW2API_CHROME_START_MINIMIZED=1



if not defined FLOW2API_CDP_BASE_PORT set FLOW2API_CDP_BASE_PORT=9236

rem Profile/port lay tu Dashboard (storage/system_config.json), khong ep Default o day.



call "%~dp0check-ports.bat"

if errorlevel 1 goto :end



echo Flow2API + Playwright UI - Ctrl+C de dung
for /f "delims=" %%P in ('python -c "from flow2api.services.system_ops import get_playwright_flow_chrome_profile,get_playwright_flow_email,get_playwright_flow_cdp_port; print(get_playwright_flow_chrome_profile()+'|'+(get_playwright_flow_email() or '-')+'|'+str(get_playwright_flow_cdp_port()))"') do set "PW_INFO=%%P"
for /f "tokens=1,2,3 delims=|" %%a in ("%PW_INFO%") do (
  echo   FLOW profile=%%a email=%%b CDP=%%c
  set "FLOW2API_PLAYWRIGHT_FLOW_CDP_PORT=%%c"
)
echo.

powershell -NoProfile -Command "$p=$env:FLOW2API_PLAYWRIGHT_FLOW_CDP_PORT; $ok=$false; foreach($h in @('localhost','127.0.0.1')) { try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 (\"http://${h}:${p}/json/version\")).StatusCode | Out-Null; $ok=$true; break } catch {} }; if(-not $ok){ exit 1 }" >nul 2>&1

if errorlevel 1 (

  echo [CANH BAO] CDP port %FLOW2API_PLAYWRIGHT_FLOW_CDP_PORT% CHUA mo! Chay launch-chrome-cdp.bat truoc.

  echo   http://localhost:%FLOW2API_PLAYWRIGHT_FLOW_CDP_PORT%/json/version

  echo.

) else (

  echo [OK] CDP port %FLOW2API_PLAYWRIGHT_FLOW_CDP_PORT% san sang.

  echo.

)



python run.py



:end

echo.

pause >nul

