@echo off
setlocal
rem Pure batch entry point. All startup logic lives in Python: narrative.launcher.
rem The only external program this chain needs is the project virtual environment
rem Python below, so there is no shell engine to find, no execution policy and no
rem alias lookup that can report "not found" on a trimmed PATH.
rem
rem Exit codes (documented in src\narrative\launcher.py):
rem   0 ok, 2 bad arguments, 3 environment incomplete, 4 Low integrity blocked,
rem   5 runtime preparation failed, 6 startup checks failed, 7 QA timed out.
set "NARRATIVE_ROOT=%~dp0"
set "NARRATIVE_PYTHON=%NARRATIVE_ROOT%.venv\Scripts\python.exe"
if not exist "%NARRATIVE_PYTHON%" (
    echo.
    echo The project virtual environment is missing:
    echo   %NARRATIVE_PYTHON%
    echo Run setup-environment.cmd first - it syncs the dependencies and prepares
    echo the local Python - then start this again.
    echo.
    pause
    exit /b 3
)
"%NARRATIVE_PYTHON%" -I -m narrative.launcher %*
set "narrative_exit_code=%errorlevel%"
if not "%narrative_exit_code%"=="0" (
    echo.
    echo Desktop startup did not complete ^(exit code %narrative_exit_code%^).
    echo See the message above and the launch log .runtime\logs\launch.log.
    pause
)
exit /b %narrative_exit_code%
