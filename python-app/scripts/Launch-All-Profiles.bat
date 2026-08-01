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

set "FLOW_URL=https://labs.google/fx/vi/tools/flow"

echo Opening Default
start "" "%CHROME_PATH%" --profile-directory="Default" --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
ping 127.0.0.1 -n 3 > nul

echo Opening Profile 1
start "" "%CHROME_PATH%" --profile-directory="Profile 1" --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
ping 127.0.0.1 -n 3 > nul

echo Opening Profile 20
start "" "%CHROME_PATH%" --profile-directory="Profile 20" --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
ping 127.0.0.1 -n 3 > nul

echo Opening Profile 21
start "" "%CHROME_PATH%" --profile-directory="Profile 21" --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
ping 127.0.0.1 -n 3 > nul

echo Opening Profile 23
start "" "%CHROME_PATH%" --profile-directory="Profile 23" --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
ping 127.0.0.1 -n 3 > nul

echo Opening Profile 25
start "" "%CHROME_PATH%" --profile-directory="Profile 25" --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
ping 127.0.0.1 -n 3 > nul

echo Opening Profile 5
start "" "%CHROME_PATH%" --profile-directory="Profile 5" --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
ping 127.0.0.1 -n 3 > nul

echo Done.
