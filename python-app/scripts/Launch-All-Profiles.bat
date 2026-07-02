@echo off
setlocal enabledelayedexpansion
title Flow2API — Launch Chrome Profiles

set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_PATH%" set "CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_PATH%" set "CHROME_PATH=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_PATH%" (
  echo Chrome not found
  exit /b 1
)

set "USER_DATA=%LOCALAPPDATA%\Google\Chrome\User Data"
set "FLOW_URL=https://labs.google/fx/vi/tools/flow"
set "FLOW_PROFILE=Default"
set "FLOW_CDP=9236"
set "CDP_PORT=9236"

rem === Flow / Playwright: profile co dinh, port co dinh (mo TRUOC) ===
if exist "%USER_DATA%\%FLOW_PROFILE%\Preferences" (
  echo Opening Flow profile !FLOW_PROFILE! CDP=!FLOW_CDP!
  start "" "%CHROME_PATH%" --user-data-dir="%USER_DATA%" --profile-directory="%FLOW_PROFILE%" --remote-debugging-port=!FLOW_CDP! --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
  ping 127.0.0.1 -n 5 > nul
) else (
  echo CANH BAO: Khong tim thay profile %FLOW_PROFILE% — Playwright port %FLOW_CDP% se khong co.
)

if !FLOW_CDP! equ 9236 set /a CDP_PORT+=1

if exist "%USER_DATA%\Default\Preferences" (
  if /I not "Default"=="%FLOW_PROFILE%" (
    echo Opening Default CDP=!CDP_PORT!
    start "" "%CHROME_PATH%" --user-data-dir="%USER_DATA%" --profile-directory="Default" --remote-debugging-port=!CDP_PORT! --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
    set /a CDP_PORT+=1
    ping 127.0.0.1 -n 3 > nul
  )
)

for /D %%D in ("%USER_DATA%\Profile *") do (
  if exist "%%D\Preferences" (
    set "prof=%%~nxD"
    if /I not "!prof!"=="%FLOW_PROFILE%" (
      echo Opening !prof! CDP=!CDP_PORT!
      start "" "%CHROME_PATH%" --user-data-dir="%USER_DATA%" --profile-directory="!prof!" --remote-debugging-port=!CDP_PORT! --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
      set /a CDP_PORT+=1
      ping 127.0.0.1 -n 3 > nul
    )
  )
)
echo Done. Flow/Playwright: %FLOW_PROFILE% @ port %FLOW_CDP%
echo Kiem tra: http://127.0.0.1:%FLOW_CDP%/json/version
