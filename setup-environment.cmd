@echo off
setlocal
set "NARRATIVE_ROOT=%~dp0"
cd /d "%NARRATIVE_ROOT%"

rem A Low integrity session stamps a Low integrity label on everything it creates,
rem the interpreter included, and Windows then always starts those exes as Low.
rem The desktop app would not be able to write the user profile directory
rem (recent projects and model settings fail with "attempt to write a readonly
rem database"). Say it before spending ten minutes on a broken environment.
whoami /groups 2>nul | findstr /C:"S-1-16-4096" >nul 2>nul
if not errorlevel 1 (
    echo ==============================================================
    echo WARNING: this session is Low integrity ^(restricted terminal or sandbox^).
    echo Everything created here gets a Low integrity label, the interpreter
    echo included, and Windows then always starts those exes as Low. Run
    echo setup from a normal ^(Medium^) terminal instead.
    echo ==============================================================
)

where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo uv was not found on PATH.
    echo Install uv, reopen this terminal, then run this again.
    echo.
    pause
    exit /b 1
)

uv sync --locked --all-packages --all-extras --all-groups
if errorlevel 1 goto :failed

rem Preparation lives in the launcher, so there is exactly one implementation:
rem -PrepareOnly runs scripts\localize_python.py from the base interpreter,
rem repoints pyvenv.cfg and runs scripts\check_runtime.py - all of it in Python.
"%NARRATIVE_ROOT%.venv\Scripts\python.exe" -I -m narrative.launcher -PrepareOnly
if errorlevel 1 goto :failed

rem uv sync may have recreated the interpreter files, so repair the labels once more.
"%NARRATIVE_ROOT%.venv\Scripts\python.exe" -I "%NARRATIVE_ROOT%scripts\check_runtime.py" --fix-integrity-labels
if errorlevel 1 goto :failed

echo.
echo Environment setup completed.
pause
exit /b 0

:failed
echo.
echo Environment setup failed ^(exit code %errorlevel%^). See the message above.
pause
exit /b 1
