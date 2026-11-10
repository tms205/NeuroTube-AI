@echo off
setlocal
cd /d "%~dp0"
set "STUDIO_PYTHON=%~dp0venv\Scripts\python.exe"
chcp 65001 >nul

if not exist "%STUDIO_PYTHON%" (
    echo Creating the project environment with Python 3.13...
    py -3.13 -m venv venv
    if errorlevel 1 (
        echo Python 3.13 could not create the environment. Run py -0p to check installed versions.
        pause
        exit /b 1
    )
)

"%STUDIO_PYTHON%" -X utf8 -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)"
if errorlevel 1 (
    echo This environment must use Python 3.13. No other version will be started.
    echo Check venv\pyvenv.cfg before recreating your environment.
    pause
    exit /b 1
)

set "VIRTUAL_ENV=%~dp0venv"
set "PATH=%VIRTUAL_ENV%\Scripts;%PATH%"
set "PYTHONUTF8=1"
"%STUDIO_PYTHON%" -c "import sys; print('Python:', sys.version.split()[0]); print('Interpreter:', sys.executable)"
if /i "%~1"=="--check" goto check_only
if /i "%~1"=="--check-only" goto check_readonly
if "%~1"=="" goto preflight
if /i "%~nx1"=="gui.py" goto preflight
if /i "%~nx1"=="pipeline.py" goto preflight
goto launch

:check_only
"%STUDIO_PYTHON%" -X utf8 startup_check.py
goto finish

:check_readonly
"%STUDIO_PYTHON%" -X utf8 startup_check.py --no-update
goto finish

:preflight
"%STUDIO_PYTHON%" -X utf8 startup_check.py
if errorlevel 2 (
    echo Studio is already running. Close the old Studio before updating.
    pause
    exit /b 0
)
if errorlevel 1 (
    echo Startup checks found an environment error. See the messages above.
    pause
    exit /b 1
)

:launch
if "%~1"=="" (
    "%STUDIO_PYTHON%" -X utf8 gui.py
) else (
    "%STUDIO_PYTHON%" -X utf8 %*
)
:finish
set "STUDIO_EXIT_CODE=%ERRORLEVEL%"
if not "%STUDIO_EXIT_CODE%"=="0" pause
exit /b %STUDIO_EXIT_CODE%
