param(
    [string]$Project = '',
    [double]$QaExitAfterSeconds = 0,
    [string]$RenderLog = '',
    [switch]$PrepareOnly,
    # 逃生口：受限终端/沙箱里做 QA 自动化时显式放行低完整性启动。
    # 用了它，界面会常驻「改动不会写进用户目录」的提示（bridge.integrityWarning）。
    [switch]$AllowLowIntegrity
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runtime = Join-Path $projectRoot '.runtime\python'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$config = Join-Path $projectRoot '.venv\pyvenv.cfg'
if (-not (Test-Path -LiteralPath $pythonw) -or
    -not (Test-Path -LiteralPath $python) -or
    -not (Test-Path -LiteralPath $config)) {
    throw 'Project virtual environment is incomplete. Run uv sync --locked --all-extras before starting.'
}

# --- 启动日志 ------------------------------------------------------------------
# 每次启动都留一行证据：低完整性出问题时，靠它回答「谁启动的、父进程什么完整性、
# 标签修过什么」，而不是靠猜。工作区是 Low 标签，低完整性进程也能写这里；
# 不要写 %TEMP%（本机受限会话里建临时目录会挂住）。
$logsDir = Join-Path $projectRoot '.runtime\logs'
if (-not (Test-Path -LiteralPath $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
}
$launchLog = Join-Path $logsDir 'launch.log'

function Write-LaunchLog([string]$text) {
    try {
        $stamp = Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz'
        Add-Content -LiteralPath $launchLog -Value ($stamp + ' ' + $text) -Encoding UTF8
    } catch { }
}

Write-LaunchLog ('launch_begin pid=' + $PID + ' script="' + $PSCommandPath + '"')

# --- 完整性标签自检与自动修复 --------------------------------------------------
# 真因在这里：解释器树（.venv / .runtime\python）如果带着 Low 完整性标签，
# Windows 就会**一律以 Low 启动这些 exe**——父进程是不是 Medium 都没用，
# 资源管理器双击也一样。标签是低完整性进程（代理会话里的 uv sync /
# setup-environment / localize_python）创建这些文件时打上的。
# 判定与 icacls 只写在 scripts/check_runtime.py（--fix-integrity-labels）里，
# 这里只负责调用与记录，避免两处各写一套。
function Repair-IntegrityLabels([string]$stage) {
    # 修不动是「要说出来的失败」，不是「中止启动」：icacls 出错时别被
    # $ErrorActionPreference = 'Stop' 变成异常把整次启动打断。
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $python -I (Join-Path $projectRoot 'scripts\check_runtime.py') --fix-integrity-labels 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    foreach ($line in @($output)) {
        if ("$line" -match 'integrity_label ') {
            Write-LaunchLog ("label_stage=$stage " + "$line")
        }
    }
    if ($code -ne 0) {
        Write-LaunchLog ("label_stage=$stage result=failed exit_code=$code")
        Write-Output 'Warning: could not raise the interpreter integrity label to Medium.'
        Write-Output 'A Low-labelled interpreter cannot be fixed from a Low process.'
        Write-Output 'Run scripts\fix_venv_integrity.ps1 from a normal (Medium) terminal, then start again.'
    }
}

Repair-IntegrityLabels 'pre'
if (-not (Test-Path -LiteralPath $runtime)) {
    Write-Output 'Preparing the project-local Python runtime for first launch.'
    # uv launchers may embed the original interpreter and remain locked while
    # running. Query it first, then prepare from a separate base interpreter.
    $sourceJson = & $python -I -c 'import json,sys; print(json.dumps(sys.base_prefix))'
    if ($LASTEXITCODE -ne 0) { throw 'Could not locate the virtual environment base Python.' }
    $source = $sourceJson | ConvertFrom-Json
    $sourcePython = Join-Path $source 'python.exe'
    & $sourcePython -I (Join-Path $projectRoot 'scripts\localize_python.py') --source $source
    if ($LASTEXITCODE -ne 0) {
        throw 'Project runtime preparation failed. See the diagnostic above.'
    }
}
if (
    -not (Test-Path -LiteralPath (Join-Path $runtime 'python.exe')) -or
    -not (Test-Path -LiteralPath (Join-Path $runtime 'Lib\encodings'))) {
    throw 'Project runtime is incomplete. Inspect .runtime\python; if it is a failed new installation, move that directory aside and rerun setup-environment.cmd.'
}
# Existing local runtimes may accompany a newly recreated uv environment.
# Never mix redirectors for a different interpreter version.
$runtimePython = Join-Path $runtime 'python.exe'
$runtimeVersion = & $runtimePython -I -c 'import sys; print(sys.version_info.major,sys.version_info.minor,sep=chr(46))'
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the project-local Python runtime.' }
$versionLine = Get-Content -LiteralPath $config | Where-Object { $_ -match '^\s*(version_info|version)\s*=\s*(\d+)\.(\d+)' } | Select-Object -First 1
if (-not $versionLine -or $versionLine -notmatch '^\s*(version_info|version)\s*=\s*(\d+)\.(\d+)' -or "$($Matches[2]).$($Matches[3])" -ne $runtimeVersion) {
    throw 'The virtual environment and local Python runtime versions do not match. Inspect them before rebuilding the runtime.'
}
$needsRepair = $false
$hasher = [Security.Cryptography.SHA256]::Create()
foreach ($pair in @(@('venvlauncher.exe', 'python.exe'), @('venvwlauncher.exe', 'pythonw.exe'))) {
    $redirector = Join-Path $runtime ("Lib\venv\scripts\nt\" + $pair[0])
    $installed = Join-Path $projectRoot (".venv\Scripts\" + $pair[1])
    if (-not (Test-Path -LiteralPath $redirector)) { throw "Missing CPython redirector: $redirector" }
    $expectedHash = [Convert]::ToBase64String($hasher.ComputeHash([IO.File]::ReadAllBytes($redirector)))
    $actualHash = [Convert]::ToBase64String($hasher.ComputeHash([IO.File]::ReadAllBytes($installed)))
    if ($expectedHash -ne $actualHash) { $needsRepair = $true }
}
$hasher.Dispose()
if ($needsRepair) {
    $identityCode = "import json,sys,struct,platform,sysconfig; print(json.dumps([list(sys.version_info[:3]),sys.implementation.cache_tag,struct.calcsize(chr(80)),platform.machine(),sysconfig.get_config_var('Py_GIL_DISABLED')]))"
    $runtimeIdentity = & $runtimePython -I -c $identityCode
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the local Python runtime identity.' }
    try {
        $venvIdentity = & $python -I -c $identityCode 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Interpreter could not run.' }
    } catch {
        throw 'Cannot verify the existing virtual environment interpreter. Recreate .venv with uv and rerun environment setup.'
    }
    if (($venvIdentity -join '') -ne ($runtimeIdentity -join '')) {
        throw 'The virtual environment and local runtime differ in Python version, ABI, or architecture. Inspect the existing runtime before rebuilding it.'
    }
    Write-Output 'Repairing virtual environment launchers for the project-local Python runtime.'
    & $runtimePython -I (Join-Path $projectRoot 'scripts\localize_python.py') --source $runtime
    if ($LASTEXITCODE -ne 0) { throw 'Project runtime preparation failed. See the diagnostic above.' }
}
# A moved project must point its venv at the copy beside it, never the old host.
$configLines = @(Get-Content -LiteralPath $config | Where-Object { $_ -notmatch '^(home|executable|command)\s*=' })
$localConfig = @('home = ' + $runtime) + $configLines
$configText = ($localConfig -join "`n") + "`n"
if ([IO.File]::ReadAllText($config) -ne $configText) {
    [IO.File]::WriteAllText($config, $configText, (New-Object Text.UTF8Encoding($false)))
}
& $python -I (Join-Path $projectRoot 'scripts\check_runtime.py')
if ($LASTEXITCODE -ne 0) {
    throw 'Project startup checks failed. See the diagnostic above.'
}
if ($PrepareOnly) {
    Write-Output 'Project environment is ready.'
    return
}
# --- 启动取证 + 低完整性处置 ---------------------------------------------------
# 标签已经在上面自动修过；但当前进程的完整性是启动时就定下来的，改标签救不了它。
# 所以这里仍然要认人：自己是 Low 就硬拦（或走显式逃生口），绝不悄悄把用户带进
# 一个存不下东西的窗口。
function Read-IntegrityLevel([int]$targetPid) {
    # 完整性读取只在 python 侧实现一次；查不到就写 unknown，不猜。
    $text = & $python -I -c "from narrative.runtime_security import process_integrity_level as f; v=f($targetPid); print('unknown' if v is None else v)" 2>$null
    $value = [string](@($text) | Select-Object -First 1)
    if ($value -match '^\d+$') { return [int]$value }
    return $null
}

# 父进程是谁：低完整性确实是「谁启动的」问题的一半答案，另一半看上面的标签日志。
$parentProcessId = 0
$parentImage = 'unknown'
$parentCommandLine = 'unknown'
$ownCommandLine = $PSCommandPath
try {
    $selfProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop
    if ($selfProcess) {
        $parentProcessId = [int]$selfProcess.ParentProcessId
        if ($selfProcess.CommandLine) { $ownCommandLine = [string]$selfProcess.CommandLine }
    }
    if ($parentProcessId -gt 0) {
        $parentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$parentProcessId" -ErrorAction Stop
        if ($parentProcess) {
            if ($parentProcess.ExecutablePath) { $parentImage = [string]$parentProcess.ExecutablePath }
            elseif ($parentProcess.Name) { $parentImage = [string]$parentProcess.Name }
            if ($parentProcess.CommandLine) { $parentCommandLine = [string]$parentProcess.CommandLine }
        }
    }
} catch {
    $ownCommandLine = [string]$MyInvocation.Line
}
$selfIntegrity = Read-IntegrityLevel $PID
$parentIntegrity = Read-IntegrityLevel $parentProcessId
$selfIntegrityText = 'unknown'
if ($null -ne $selfIntegrity) { $selfIntegrityText = ('0x{0:x4}' -f $selfIntegrity) }
$parentIntegrityText = 'unknown'
if ($null -ne $parentIntegrity) { $parentIntegrityText = ('0x{0:x4}' -f $parentIntegrity) }
$lowIntegrity = ($null -ne $selfIntegrity) -and ($selfIntegrity -lt 0x2000)

$allowLowIntegrity = $AllowLowIntegrity.IsPresent
if ($env:NARRATIVE_ALLOW_LOW_INTEGRITY -match '^(?i)(1|true|yes|on)$') { $allowLowIntegrity = $true }

$logParts = @(
    ('pid=' + $PID),
    ('self_integrity=' + $selfIntegrityText),
    ('parent_pid=' + $parentProcessId),
    ('parent_integrity=' + $parentIntegrityText),
    ('parent_image="' + $parentImage + '"'),
    ('parent_cmd="' + $parentCommandLine + '"'),
    ('allow_low_integrity=' + $allowLowIntegrity),
    ('cmdline="' + $ownCommandLine + '"')
)
$logPrefix = ($logParts -join ' ')

if ($lowIntegrity -and -not $allowLowIntegrity) {
    Write-LaunchLog ($logPrefix + ' decision=blocked reason=low_integrity')
    Write-Output '=============================================================='
    Write-Output 'BLOCKED: this process is Low integrity, so the app could not save anything.'
    Write-Output ''
    Write-Output ('  integrity:            ' + $selfIntegrityText + ' (Low; Medium is required)')
    Write-Output ('  parent process:       ' + $parentImage + ' (pid ' + $parentProcessId + ', integrity ' + $parentIntegrityText + ')')
    Write-Output ('  parent command line:  ' + $parentCommandLine)
    Write-Output ''
    Write-Output 'Why it happens (two separate causes, both real):'
    Write-Output '  a) the interpreter files carry a Low integrity label - Windows starts any'
    Write-Output '     Low-labelled exe as Low, even from a Medium parent (Explorer included).'
    Write-Output '     Such a process cannot write %USERPROFILE%\.narrative, so recent projects'
    Write-Output '     and model settings silently fail (SQLite: "attempt to write a readonly'
    Write-Output '     database").'
    Write-Output '  b) this window itself was started from a restricted terminal / sandbox /'
    Write-Output '     agent shell, which passes its Low token down to cmd -> powershell.'
    Write-Output ''
    Write-Output 'What was already tried automatically:'
    Write-Output '  the interpreter label fix ran before this point - see the integrity_label'
    Write-Output ('  lines in ' + $launchLog)
    Write-Output '  Relaunching through explorer.exe is NOT attempted: it does not help when the'
    Write-Output '  exe itself carries the Low label, it only starts the app twice.'
    Write-Output ''
    Write-Output 'What to do:'
    Write-Output '  1. Close this window and double-click start-desktop.cmd in Explorer (or use'
    Write-Output '     the Start menu / desktop shortcut). If the labels above were still Low,'
    Write-Output '     run scripts\fix_venv_integrity.ps1 once from a normal shell first.'
    Write-Output '  2. Debugging from a restricted terminal on purpose? Pass -AllowLowIntegrity'
    Write-Output '     to start-desktop.ps1 (or set NARRATIVE_ALLOW_LOW_INTEGRITY=1). The app'
    Write-Output '     will then keep a permanent warning in its status bar.'
    Write-Output ''
    Write-Output ('  launch log: ' + $launchLog)
    Write-Output '=============================================================='
    exit 4
}

if ($lowIntegrity) {
    # 逃生口：允许跑，但必须让降级可见（界面状态栏常驻提示，见 bridge.integrityWarning）。
    $env:NARRATIVE_ALLOW_LOW_INTEGRITY = '1'
    Write-LaunchLog ($logPrefix + ' decision=allow_low_integrity reason=explicit_override')
    Write-Output 'Warning: starting with Low integrity on purpose (-AllowLowIntegrity).'
    Write-Output 'The app will keep showing that nothing is written to the user directory.'
} else {
    Write-LaunchLog ($logPrefix + ' decision=normal')
}
$arguments = @('-I', '-m', 'narrative.native_app')
if ($Project) {
    $arguments += @('--project', ('"' + $Project + '"'))
}
if ($QaExitAfterSeconds -gt 0) {
    $arguments += @('--qa-exit-after-seconds', $QaExitAfterSeconds.ToString([cultureinfo]::InvariantCulture))
}
if ($RenderLog) {
    $renderLogPath = [IO.Path]::GetFullPath((Join-Path $projectRoot $RenderLog))
    $arguments += @('--render-log', ('"' + $renderLogPath + '"'))
}
# pythonw hosts the requested visible interactive Qt window without a console.
# 启动前再修一次标签：上面的 localize_python / 重定向器修复会在低完整性进程里
# 重写可执行文件，新写出来的 exe 又会被打回 Low 标签——这一遍是最后一道保险。
Repair-IntegrityLabels 'prelaunch'
Write-Output "Starting native desktop from $projectRoot."
$desktopProcess = Start-Process -FilePath $pythonw -WorkingDirectory $projectRoot -ArgumentList $arguments -NoNewWindow -PassThru
# 先抓一次句柄：应用可能立刻就退出（例如低完整性被应用侧拦下），那时懒取的
# ExitCode 会读不到，QA 报告里就只剩一句「code unknown」。
if ($QaExitAfterSeconds -gt 0) { $null = $desktopProcess.Handle }
Write-Output "Native desktop started (PID $($desktopProcess.Id))."
if ($QaExitAfterSeconds -gt 0) {
    $waitMilliseconds = [int](($QaExitAfterSeconds + 15) * 1000)
    if (-not $desktopProcess.WaitForExit($waitMilliseconds)) {
        throw 'Timed desktop QA did not exit after the requested duration.'
    }
    $desktopProcess.Refresh()
    $qaCode = 'unknown'
    if ($null -ne $desktopProcess.ExitCode) { $qaCode = $desktopProcess.ExitCode }
    Write-Output "Native desktop QA exited (code $qaCode)."
    Write-LaunchLog ($logPrefix + ' decision=qa_exit qa_exit_code=' + $qaCode)
    # QA 模式下把应用的退出码原样交回去：应用侧拦下低完整性时是 3，
    # 外部脚本据此判断「这次启动到底成没成」，不用去猜。
    if ("$qaCode" -match '^\d+$' -and [int]$qaCode -ne 0) { exit ([int]$qaCode) }
}
