@echo off
rem ytbulk - command-line mode. Example:  ytbulk.bat -i videos.txt -o D:\Videos --codec h264
setlocal EnableExtensions
set "DIR=%~dp0"
if not exist "%DIR%.venv\Scripts\python.exe" (
  echo Not set up yet - double-click setup.bat first. 1>&2
  exit /b 2
)
set "PYTHONUTF8=1"
set "PYTHONPATH=%DIR%;%PYTHONPATH%"
"%DIR%.venv\Scripts\python.exe" -m ytbulk %*
exit /b %errorlevel%
