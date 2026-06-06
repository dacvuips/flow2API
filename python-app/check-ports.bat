@echo off
setlocal
set "BLOCKED=0"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":1994 .*LISTENING"') do (
  echo [LOI] Port 1994 dang duoc dung boi PID %%P
  echo       Dong cua so CMD cu ^(Ctrl+C^) hoac: taskkill /PID %%P /F
  set "BLOCKED=1"
)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":1609 .*LISTENING"') do (
  echo [LOI] Port 1609 ^(extension WS^) dang duoc dung boi PID %%P
  set "BLOCKED=1"
)
if "%BLOCKED%"=="1" (
  echo.
  echo Chi chay MOT instance run.bat hoac run-prod.bat.
  exit /b 1
)
exit /b 0
