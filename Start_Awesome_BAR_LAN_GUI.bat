@echo off
setlocal
set "SCRIPT=%~dp0Awesome_BAR_LAN_GUI.ps1"
if not exist "%SCRIPT%" (
  echo ERROR: GUI script not found: "%SCRIPT%"
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
if errorlevel 1 (
  echo.
  echo The GUI exited with an error. If a red error message appeared above, take a screenshot of it.
  pause
)
