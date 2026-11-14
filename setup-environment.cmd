@echo off
setlocal
rem Windows PowerShell 5.1 is retired: this chain runs PowerShell 7 (pwsh) only.
rem PATH alone is not enough: the Store alias lives in %LOCALAPPDATA%\Microsoft\WindowsApps
rem and some launch contexts do not have it on PATH.
set "PWSH_EXE="
for %%P in (pwsh.exe) do if not defined PWSH_EXE set "PWSH_EXE=%%~$PATH:P"
if not defined PWSH_EXE if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PWSH_EXE if exist "%ProgramFiles%\PowerShell\7-preview\pwsh.exe" set "PWSH_EXE=%ProgramFiles%\PowerShell\7-preview\pwsh.exe"
if not defined PWSH_EXE if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe" set "PWSH_EXE=%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"
if not defined PWSH_EXE (
    echo.
    echo PowerShell 7 ^(pwsh^) was not found.
    echo Looked in PATH, "%ProgramFiles%\PowerShell\7", "%ProgramFiles%\PowerShell\7-preview"
    echo and "%LOCALAPPDATA%\Microsoft\WindowsApps".
    echo Install it with: winget install Microsoft.PowerShell
    echo Then reopen this window and try again.
    echo.
    pause
    exit /b 1
)
"%PWSH_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-environment.ps1"
set "narrative_exit_code=%errorlevel%"
if not "%narrative_exit_code%"=="0" (
    echo.
    echo Environment setup failed. See the message above.
) else (
    echo.
    echo Environment setup completed.
)
pause
exit /b %narrative_exit_code%
