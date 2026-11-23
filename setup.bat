@echo off
rem ytbulk - one-time setup for Windows. Double-click this file.
setlocal EnableExtensions
cd /d "%~dp0"
title ytbulk setup
set "NOPAUSE="
if /i "%~1"=="--no-pause" set "NOPAUSE=1"

echo === ytbulk setup ===
echo.

rem --- 1. Find Python 3.10+ (py launcher first, then python on PATH) ---
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if defined PY goto :have_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if defined PY goto :have_python

echo Python 3.10 or newer was not found.
where winget >nul 2>nul || goto :manual_python
choice /C YN /M "Install Python 3.12 now using winget (no admin needed)"
if errorlevel 2 goto :manual_python
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
echo.
echo Python is installed. Close this window and double-click setup.bat again.
if not defined NOPAUSE pause
exit /b 1

:manual_python
echo Install Python from https://www.python.org/downloads/
echo During install, tick "Add python.exe to PATH". Then run setup.bat again.
if not defined NOPAUSE pause
exit /b 1

:have_python
echo Using Python: %PY%

rem --- 2. Virtual environment (a .venv copied from Linux/macOS is replaced) ---
if exist ".venv\bin\" if not exist ".venv\Scripts\" (
  echo Removing a .venv that was created on another operating system...
  rmdir /s /q ".venv"
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv || goto :fail
)

rem --- 3. Dependencies: yt-dlp, deno, rich, bundled ffmpeg ---
echo Installing dependencies - the first run downloads about 150 MB, please wait...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip || goto :fail
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade -r requirements.txt || goto :fail
echo Installing the Deno JavaScript runtime - optional, YouTube needs it when Node.js is absent...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade deno || echo   Deno is not available for this PC - install Node.js 20+ from https://nodejs.org instead.
if not exist "videos.txt" copy /y "videos.example.txt" "videos.txt" >nul

rem --- 4. Check everything ---
echo.
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" -m ytbulk --check
if errorlevel 1 goto :checkfail
echo Setup complete.
echo   Double-click  ytbulk-ui.bat  to open the downloader in your browser.
echo.
if not defined NOPAUSE pause
exit /b 0

:checkfail
echo Setup finished, but some checks FAILED - see the lines marked FAIL above.
if not defined NOPAUSE pause
exit /b 1

:fail
echo.
echo Setup failed - see the messages above.
if not defined NOPAUSE pause
exit /b 1
