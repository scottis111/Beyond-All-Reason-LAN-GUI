@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "SCRIPT=%~dp0awesome_bar_lan_gui.py"

if not exist "%SCRIPT%" (
    echo ERROR: awesome_bar_lan_gui.py was not found in this folder.
    echo Keep this batch file in the same folder as the Python program.
    pause
    exit /b 2
)

set "PYTHON_CMD="
where py >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo ERROR: Python 3 was not found.
    echo Install 64-bit Python 3 from https://www.python.org/downloads/windows/
    echo During installation, enable the option to add Python to PATH.
    pause
    exit /b 3
)

%PYTHON_CMD% -c "import sys; print('Using Python', sys.version.split()[0])"
if errorlevel 1 (
    echo ERROR: Python could not start.
    pause
    exit /b 4
)

%PYTHON_CMD% -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
    echo PyQt6 is required by the lobby application.
    choice /C YN /M "Install PyQt6 now using pip? Internet access is required"
    if errorlevel 2 goto no_install
    %PYTHON_CMD% -m pip install --upgrade PyQt6
    if errorlevel 1 (
        echo ERROR: PyQt6 installation failed. Try running this command in Command Prompt:
        echo %PYTHON_CMD% -m pip install --upgrade PyQt6
        pause
        exit /b 5
    )
)

%PYTHON_CMD% "%SCRIPT%"
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
    echo.
    echo The application exited with code %APP_EXIT%.
)
pause
exit /b %APP_EXIT%

:no_install
echo PyQt6 was not installed. The lobby cannot start without it.
pause
exit /b 6
