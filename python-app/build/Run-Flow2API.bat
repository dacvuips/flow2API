@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem Portable / after-install launcher.
rem Storage path: storage_path.txt (set by installer) or folder picker on first run.
rem Re-pick: Run Flow2API.bat --pick-storage

title Flow2API-Agent

set "FLOW2API_ROOT=%~dp0"
if "%FLOW2API_ROOT:~-1%"=="\" set "FLOW2API_ROOT=%FLOW2API_ROOT:~0,-1%"

set "FLOW2API_FRONTEND=%FLOW2API_ROOT%\frontend"
set "FLOW2API_EXTENSION=%FLOW2API_ROOT%\extension"
set "FLOW2API_RELOAD=0"
set "STORAGE_CFG=%FLOW2API_ROOT%\storage_path.txt"
set "FORCE_PICK=0"
if /I "%~1"=="--pick-storage" set "FORCE_PICK=1"
if /I "%~1"=="pick" set "FORCE_PICK=1"

if not exist "%~dp0Flow2API-Agent.exe" (
  echo [LOI] Khong tim thay Flow2API-Agent.exe canh file nay.
  pause
  exit /b 1
)

set "FLOW2API_STORAGE="
if "%FORCE_PICK%"=="0" if exist "%STORAGE_CFG%" (
  for /f "usebackq delims=" %%A in ("%STORAGE_CFG%") do (
    set "LINE=%%A"
    goto :have_line
  )
)
goto :need_pick

:have_line
rem strip quotes / spaces
set "LINE=!LINE:"=!"
for /f "tokens=* delims= " %%B in ("!LINE!") do set "LINE=%%B"
if defined LINE if not "!LINE:~0,1!"=="#" set "FLOW2API_STORAGE=!LINE!"
if defined FLOW2API_STORAGE goto :storage_ready

:need_pick
echo.
echo Chon thu muc luu du lieu ^(database, video, output^)...
echo.
set "DEFAULT_STORAGE=%LOCALAPPDATA%\Flow2API\storage"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "Add-Type -AssemblyName System.Windows.Forms;" ^
  "$d = New-Object System.Windows.Forms.FolderBrowserDialog;" ^
  "$d.Description = 'Flow2API — chon thu muc luu storage (DB, video, output)';" ^
  "$d.ShowNewFolderButton = $true;" ^
  "$def = $env:LOCALAPPDATA + '\Flow2API\storage';" ^
  "if (-not (Test-Path -LiteralPath $def)) { New-Item -ItemType Directory -Path $def -Force | Out-Null };" ^
  "$d.SelectedPath = $def;" ^
  "if ($d.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 2 };" ^
  "Write-Output $d.SelectedPath"`) do set "FLOW2API_STORAGE=%%P"

if not defined FLOW2API_STORAGE (
  echo [LOI] Ban da huy chon thu muc storage. Thoat.
  pause
  exit /b 1
)

(
  echo !FLOW2API_STORAGE!
) > "%STORAGE_CFG%"
echo Da luu vi tri storage vao:
echo   %STORAGE_CFG%
echo.

:storage_ready
rem Normalize trailing spaces
for /f "tokens=* delims= " %%S in ("%FLOW2API_STORAGE%") do set "FLOW2API_STORAGE=%%S"
set "FLOW2API_DB=%FLOW2API_STORAGE%\flow2api.db"

if not exist "%FLOW2API_STORAGE%" (
  mkdir "%FLOW2API_STORAGE%" 2>nul
)
if not exist "%FLOW2API_STORAGE%" (
  echo [LOI] Khong tao duoc thu muc storage:
  echo   %FLOW2API_STORAGE%
  echo Chay lai voi: "%~nx0" --pick-storage
  pause
  exit /b 1
)

echo ========================================
echo  Flow2API Agent
echo  Dashboard: http://127.0.0.1:1994/
echo  Admin:     http://127.0.0.1:1994/admin
echo  ^(mac dinh admin / admin^)
echo  Storage:   %FLOW2API_STORAGE%
echo ========================================
echo.
echo Doi storage: "%~nx0" --pick-storage
echo Dang chay... dong cua so nay se dung server.
echo.

"%~dp0Flow2API-Agent.exe"
set "EC=%ERRORLEVEL%"
echo.
if not "%EC%"=="0" echo Agent thoat voi ma loi %EC%.
echo Nhan phim bat ky de dong...
pause >nul
exit /b %EC%
