$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot

# --- 低完整性警告 --------------------------------------------------------------
# 在受限终端/沙箱/代理里做环境准备，它创建的解释器文件会被打上 Low 完整性标签；
# 之后 Windows 对带 Low 标签的 exe **一律以 Low 启动**（父进程 Medium 也救不回来），
# 应用就写不了 %USERPROFILE%\.narrative，表现为 SQLite「attempt to write a readonly
# database」——最近工程和模型设置静默失败。这条坑曾经耗掉一整个通宵，所以这里先喊。
$groups = & whoami /groups 2>$null
if ("$groups" -match 'S-1-16-4096') {
    Write-Output '=============================================================='
    Write-Output 'WARNING: this session is Low integrity (restricted terminal / sandbox).'
    Write-Output 'Everything created here - the interpreter included - gets a Low integrity'
    Write-Output 'label, and Windows then always starts those exes as Low, no matter who'
    Write-Output 'launches them. The desktop app would not be able to write'
    Write-Output '%USERPROFILE%\.narrative (SQLite: "attempt to write a readonly database").'
    Write-Output 'Run setup from a normal (Medium) terminal. If it already happened, repair'
    Write-Output 'the labels with scripts\fix_venv_integrity.ps1 from a normal terminal.'
    Write-Output '=============================================================='
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv was not found. Install uv and reopen the terminal before running setup.'
}
Push-Location -LiteralPath $projectRoot
try {
    & uv sync --locked --all-packages --all-extras --all-groups
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit code $LASTEXITCODE)." }
    & (Join-Path $projectRoot 'start-desktop.ps1') -PrepareOnly
    # 收尾自愈：uv sync 刚建出来的解释器就是会被打 Low 标签的那批。修不动会如实报错
    # （低完整性进程抬不高别人/自己的标签），此时只能按上面的提示到正常终端修。
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $python) {
        & $python -I (Join-Path $projectRoot 'scripts\check_runtime.py') --fix-integrity-labels
    }
} finally {
    Pop-Location
}
