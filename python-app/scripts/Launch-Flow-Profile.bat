@echo off
setlocal
title Flow2API — Launch Flow Profile

set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_PATH%" set "CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_PATH%" set "CHROME_PATH=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_PATH%" (
  echo Chrome not found
  exit /b 1
)

set "USER_DATA=D:\PhanMem\tool\Flow 2\flow2API\python-app\storage\chrome-cdp-user-data"
set "FLOW_URL=https://labs.google/fx/vi/tools/flow"
set "FLOW_PROFILE=Default"
set "FLOW_CDP=9236"

if not exist "%USER_DATA%\%FLOW_PROFILE%\Preferences" (
  echo Profile %FLOW_PROFILE% not found in CDP user-data — chay launch-chrome-cdp.bat de dong bo
  exit /b 1
)

echo Opening Flow profile %FLOW_PROFILE% CDP=%FLOW_CDP% (CDP user-data)
start /min "" "%CHROME_PATH%" --user-data-dir="%USER_DATA%" --profile-directory="%FLOW_PROFILE%" --remote-debugging-port=%FLOW_CDP% --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --hide-crash-restore-bubble --disable-session-crashed-bubble --disable-restore-session-state --no-first-run --no-default-browser-check --start-minimized "%FLOW_URL%"
echo Done. Kiem tra: http://localhost:%FLOW_CDP%/json/version
