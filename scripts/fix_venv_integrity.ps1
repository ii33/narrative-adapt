#requires -Version 7.0
<#
.SYNOPSIS
    Raise virtual environments to Medium integrity on Windows.

.DESCRIPTION
    A workspace created at Low integrity makes CPython's venv launcher fail
    with "failed to open pyvenv.cfg" (exit 107), and uv trampolines fail with
    "permission denied". Medium integrity on the venv directory fixes both.
    Windows starts any Low-labelled exe as Low no matter who launched it, so a
    Low-labelled .venv\Scripts\pythonw.exe also makes the desktop app write to
    %USERPROFILE%\.narrative fail (SQLite reports "attempt to write a readonly
    database"), even when the user double-clicks start-desktop.cmd.
    This changes ACL integrity labels only; it does not change file ownership
    or read/write permissions. start-desktop.ps1 runs the same repair
    automatically through scripts/check_runtime.py --fix-integrity-labels.
#>
param(
    [string[]]$Paths = @(".venv", ".venv-graphrag", ".runtime\python")
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ($env:OS -ne "Windows_NT") {
    Write-Host "Not Windows; nothing to do."
    exit 0
}

foreach ($path in $Paths) {
    if (-not (Test-Path $path)) {
        Write-Host "Skip missing: $path"
        continue
    }
    Write-Host "Setting Medium integrity: $path"
    icacls $path /setintegritylevel "(OI)(CI)Medium" /T | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed for $path"
    }
}
Write-Host "Done. Verify with: .venv\Scripts\python.exe -V"
