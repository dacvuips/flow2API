@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0\.."

rem ============================================================
rem  Flow2API — build portable release + Windows Setup.exe
rem  1) (optional) obfuscate with PyArmor  — build_release.bat --obfuscate
rem  2) PyInstaller onedir → Flow2API-Agent.exe (no .py source shipped)
rem  3) Stage frontend + extension + launcher
rem  4) Inno Setup → Flow2API-Setup-x.y.z.exe (wizard Next Next)
rem ============================================================

set "APP_DIR=%cd%"
set "BUILD_DIR=%APP_DIR%\build"
set "DIST_DIR=%APP_DIR%\dist"
set "PYI_WORK=%DIST_DIR%\pyi-work"
set "PYI_DIST=%DIST_DIR%\pyi-out"
set "STAGE=%DIST_DIR%\Flow2API-Release"
set "REPO=%APP_DIR%\.."
set "USE_PYARMOR=0"
set "OBF_SWAP=0"
if /I "%~1"=="--obfuscate" set "USE_PYARMOR=1"
if /I "%FLOW2API_OBFUSCATE%"=="1" set "USE_PYARMOR=1"

echo.
echo [1/6] Check Python / venv...
if not exist "%APP_DIR%\.venv\Scripts\python.exe" (
  echo Chay install.bat truoc de tao .venv va cai dependencies.
  exit /b 1
)
call "%APP_DIR%\.venv\Scripts\activate.bat"
python -c "import sys; print(sys.version)"

echo.
echo [2/6] Install packagers...
python -m pip install -q --upgrade pip
python -m pip install -q "pyinstaller>=6.0"
if "%USE_PYARMOR%"=="1" (
  python -m pip install -q "pyarmor>=8.0"
)

if "%USE_PYARMOR%"=="1" (
  echo.
  echo [2b] Obfuscate package with PyArmor...
  set "OBF_OUT=%DIST_DIR%\obf"
  if exist "%OBF_OUT%" rmdir /s /q "%OBF_OUT%"
  pyarmor gen -O "%OBF_OUT%" -r -i "%APP_DIR%\flow2api"
  if errorlevel 1 (
    echo [CANH BAO] PyArmor that bai — dong goi bytecode thuong.
    set "USE_PYARMOR=0"
  ) else if not exist "%OBF_OUT%\flow2api" (
    echo [CANH BAO] PyArmor output thieu flow2api — skip.
    set "USE_PYARMOR=0"
  ) else (
    if exist "%APP_DIR%\flow2api.srcbak" rmdir /s /q "%APP_DIR%\flow2api.srcbak"
    move "%APP_DIR%\flow2api" "%APP_DIR%\flow2api.srcbak" >nul
    if errorlevel 1 (
      echo [CANH BAO] Khong doi duoc flow2api de obfuscate — skip.
      set "USE_PYARMOR=0"
    ) else (
      xcopy /E /I /Y /Q "%OBF_OUT%\flow2api" "%APP_DIR%\flow2api\"
      for /d %%D in ("%OBF_OUT%\pyarmor_runtime*") do xcopy /E /I /Y /Q "%%D" "%APP_DIR%\%%~nxD\"
      set "OBF_SWAP=1"
      echo PyArmor OK — building from obfuscated tree.
    )
  )
)

echo.
echo [3/6] PyInstaller freeze...
if exist "%PYI_WORK%" rmdir /s /q "%PYI_WORK%"
if exist "%PYI_DIST%" rmdir /s /q "%PYI_DIST%"

pyinstaller --noconfirm --clean ^
  --workpath "%PYI_WORK%" ^
  --distpath "%PYI_DIST%" ^
  "%BUILD_DIR%\Flow2API-Agent.spec"
set "PYI_EC=!ERRORLEVEL!"

if "%OBF_SWAP%"=="1" (
  echo Khoi phuc source tree sau build...
  rmdir /s /q "%APP_DIR%\flow2api" 2>nul
  move "%APP_DIR%\flow2api.srcbak" "%APP_DIR%\flow2api" >nul
  for /d %%D in ("%APP_DIR%\pyarmor_runtime*") do rmdir /s /q "%%D" 2>nul
)

if not "%PYI_EC%"=="0" (
  echo PyInstaller FAILED.
  exit /b 1
)

echo.
echo [4/6] Stage release folder...
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%"

xcopy /E /I /Y /Q "%PYI_DIST%\Flow2API-Agent\*" "%STAGE%\"
if errorlevel 1 (
  echo Khong copy duoc output PyInstaller.
  exit /b 1
)

if not exist "%REPO%\frontend\dashboard.html" (
  echo [LOI] Thieu frontend\dashboard.html
  exit /b 1
)
xcopy /E /I /Y /Q "%REPO%\frontend" "%STAGE%\frontend\"

if not exist "%REPO%\extension\manifest.json" (
  echo [LOI] Thieu extension\manifest.json
  exit /b 1
)
xcopy /E /I /Y /Q "%REPO%\extension" "%STAGE%\extension\"
if exist "%STAGE%\extension\_metadata" rmdir /s /q "%STAGE%\extension\_metadata"

copy /Y "%BUILD_DIR%\Run-Flow2API.bat" "%STAGE%\Run Flow2API.bat" >nul
copy /Y "%BUILD_DIR%\AFTER_INSTALL.txt" "%STAGE%\AFTER_INSTALL.txt" >nul

echo.
echo [5/6] Sanity check stage...
if not exist "%STAGE%\Flow2API-Agent.exe" (
  echo [LOI] Thieu Flow2API-Agent.exe trong stage.
  exit /b 1
)
if not exist "%STAGE%\frontend\dashboard.html" (
  echo [LOI] Thieu frontend trong stage.
  exit /b 1
)

echo Portable: %STAGE%
echo Chay thu: "%STAGE%\Run Flow2API.bat"

echo.
echo [6/6] Inno Setup installer...
set "ISCC="
where iscc >nul 2>&1 && for /f "delims=" %%I in ('where iscc') do set "ISCC=%%I"
if not defined ISCC if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not defined ISCC (
  echo [CANH BAO] Chua cai Inno Setup 6 — chi co ban portable.
  echo Cai: https://jrsoftware.org/isinfo.php
  echo Roi: iscc "%BUILD_DIR%\installer.iss"
  goto :done
)

"%ISCC%" "%BUILD_DIR%\installer.iss"
if errorlevel 1 (
  echo Inno Setup compile FAILED.
  exit /b 1
)

echo.
echo Installer:
for %%F in ("%DIST_DIR%\Flow2API-Setup-*.exe") do echo   %%~fF

:done
echo.
echo ===== XONG =====
echo Nguoi dung cuoi chi can:
echo   1. Chay Flow2API-Setup-*.exe ^(Next Next^)
echo   2. Shortcut "Flow2API" / Run Flow2API.bat
echo   3. Lan dau: Chrome - Load unpacked thu muc extension
if "%USE_PYARMOR%"=="1" (
  echo Code: PyArmor obfuscate + PyInstaller bytecode
) else (
  echo Code: PyInstaller bytecode ^(them --obfuscate de bat PyArmor^)
)
echo.
exit /b 0
