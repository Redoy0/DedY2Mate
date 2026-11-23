@echo off
rem ytbulk - double-click to open the downloader in your browser.
setlocal EnableExtensions
cd /d "%~dp0"
title ytbulk
if not exist ".venv\Scripts\python.exe" (
  echo First run - setting things up...
  call setup.bat --no-pause || goto :fail
)
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" -m ytbulk --ui %*
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo ytbulk stopped with an error - see the messages above.
pause
exit /b 1
