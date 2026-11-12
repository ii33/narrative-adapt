@echo off
setlocal
rem Low integrity here has two causes: the interpreter files may carry a Low integrity
rem label (Windows starts any Low-labelled exe as Low, even from Explorer), and a
rem restricted terminal / sandbox / agent shell passes its Low token down. Either way
rem the app cannot write %%USERPROFILE%%\.narrative (SQLite: "readonly database").
rem start-desktop.ps1 repairs the interpreter labels, logs who started it to
rem .runtime\logs\launch.log, and blocks with a non-zero exit code if it is still Low.
rem Escape hatch for QA: start-desktop.cmd -AllowLowIntegrity
rem Windows PowerShell 5.1 is retired: this chain runs PowerShell 7 (pwsh) only.
where pwsh >nul 2>nul
if errorlevel 1 (
    echo.
    echo PowerShell 7 ^(pwsh^) was not found.
    echo This project requires PowerShell 7: winget install Microsoft.PowerShell
    echo Reopen the terminal after installing it, then try again.
    echo.
    pause
    exit /b 1
)
pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-desktop.ps1" %*
set "narrative_exit_code=%errorlevel%"
if not "%narrative_exit_code%"=="0" (
    echo.
    echo Desktop startup did not complete ^(exit code %narrative_exit_code%^).
    echo See the message above and .runtime\logs\launch.log.
    pause
)
exit /b %narrative_exit_code%
