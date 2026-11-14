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
rem Do not rely on PATH alone. The Store alias lives in %LOCALAPPDATA%\Microsoft\WindowsApps
rem and is missing from some launch contexts (trimmed PATH from a sandbox, task or agent),
rem which used to print "pwsh was not found" even though it is installed.
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
"%PWSH_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-desktop.ps1" %*
set "narrative_exit_code=%errorlevel%"
if not "%narrative_exit_code%"=="0" (
    echo.
    echo Desktop startup did not complete ^(exit code %narrative_exit_code%^).
    echo See the message above and .runtime\logs\launch.log.
    pause
)
exit /b %narrative_exit_code%
