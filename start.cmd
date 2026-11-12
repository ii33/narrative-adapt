@echo off
setlocal
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
pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "narrative_exit_code=%errorlevel%"
if not "%narrative_exit_code%"=="0" (
    echo.
    echo Startup failed. See the message above.
    pause
)
exit /b %narrative_exit_code%
