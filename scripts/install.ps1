#Requires -Version 5.1
# 交互式安装脚本 — Kiro API Proxy
# 支持 Windows (任务计划程序)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── 辅助函数 ──────────────────────────────────────────────────────────────────
function Info($msg)    { Write-Host "√ $msg" -ForegroundColor Green }
function Warn($msg)    { Write-Host "! $msg" -ForegroundColor Yellow }
function Heading($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }

function Ask {
    param([string]$Prompt, [string]$Default = '')
    $display = if ($Default) { "$Prompt [$Default]" } else { $Prompt }
    while ($true) {
        $value = Read-Host "$display"
        if (-not $value) { $value = $Default }
        if ($value) { return $value }
        Warn "不能为空，请重新输入。"
    }
}

function Confirm($Prompt) {
    $ans = Read-Host "$Prompt [y/N]"
    return ($ans -eq 'y' -or $ans -eq 'Y' -or $ans -eq 'yes')
}

# ── 定位源码目录 ───────────────────────────────────────────────────────────────
$SourceDir = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path "$SourceDir\pyproject.toml")) {
    Write-Host "✘ 未找到 pyproject.toml，请在项目根目录或 scripts\ 子目录中运行此脚本。" -ForegroundColor Red
    exit 1
}

# ── 检查前置条件 ──────────────────────────────────────────────────────────────
Heading "检查前置条件"

$Python = $null
foreach ($candidate in @('python3.13', 'python3.12', 'python3.11', 'python3', 'python', 'py')) {
    try {
        $cmd = if ($candidate -eq 'py') { 'py' } else { $candidate }
        $args_ = if ($candidate -eq 'py') { @('-3', '-c', 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)') } `
                 else { @('-c', 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)') }
        & $cmd @args_ 2>$null
        if ($LASTEXITCODE -eq 0) {
            $Python = $cmd
            $ver = & $cmd -c 'import sys; print(sys.version)' 2>$null
            Info "找到 Python：$Python ($ver)"
            break
        }
    } catch { }
}

if (-not $Python) {
    Write-Host "✘ 未找到 Python 3.11+，请先从 https://www.python.org/ 安装。" -ForegroundColor Red
    exit 1
}

Info "源码目录：$SourceDir"

# ── 收集安装参数 ──────────────────────────────────────────────────────────────
Heading "安装目录"
Write-Host "建议将安装目录与源码目录分开，例如 D:\kiro-api-proxy。"
$InstallDir = Ask "安装目录" "$env:USERPROFILE\kiro-api-proxy"
$InstallDir = [IO.Path]::GetFullPath($InstallDir)

$Reinstall = $false
if (Test-Path "$InstallDir\venv") {
    Warn "检测到已有安装：$InstallDir"
    if (Confirm "覆盖现有安装？（将备份 config\ 目录）") {
        $Reinstall = $true
    } else {
        Write-Host "已取消。" -ForegroundColor Red; exit 0
    }
}

Heading "服务配置"
$Port = Ask "监听端口" "3458"
$WorkDir = Ask "Kiro 工作目录（KIRO_WORKING_DIRECTORY）" "$env:USERPROFILE\Code"
$WorkDir = [IO.Path]::GetFullPath($WorkDir)

Heading "凭据配置"
$CredsDst = "$InstallDir\config\runtime-credentials.json"
Write-Host "需要提供 runtime-credentials.json（含 refresh_token、client_id 等）。"
Write-Host "可以："
Write-Host "  1. 现在指定一个已有凭据文件的路径"
Write-Host "  2. 安装完成后手动编辑 $CredsDst"

$CredsSrc = ''
if (Confirm "现在指定已有凭据文件？") {
    while ($true) {
        $CredsSrc = Ask "凭据文件路径（JSON）" ""
        $CredsSrc = [IO.Path]::GetFullPath($CredsSrc)
        if (Test-Path $CredsSrc) { break }
        Warn "文件不存在：$CredsSrc"
    }
}

$AcctIndex = ''
if ($CredsSrc) {
    try {
        $json = Get-Content $CredsSrc -Raw | ConvertFrom-Json
        if ($json -is [array]) {
            $AcctIndex = Ask "凭据文件是账户数组，请输入要使用的零基索引" "0"
        }
    } catch { }
}

# ── 确认摘要 ──────────────────────────────────────────────────────────────────
Heading "安装摘要"
Write-Host "  平台           : Windows"
Write-Host "  源码目录        : $SourceDir"
Write-Host "  安装目录        : $InstallDir"
Write-Host "  监听地址        : 127.0.0.1:$Port"
Write-Host "  工作目录        : $WorkDir"
Write-Host "  凭据来源        : $(if ($CredsSrc) { $CredsSrc } else { '（安装后手动填写）' })"
if ($AcctIndex) { Write-Host "  账户索引        : $AcctIndex" }
Write-Host ""
if (-not (Confirm "确认以上配置并开始安装？")) {
    Write-Host "已取消。" -ForegroundColor Red; exit 0
}

# ── 备份（仅重装时）──────────────────────────────────────────────────────────
if ($Reinstall -and (Test-Path "$InstallDir\config")) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $Backup = "$InstallDir\config.bak.$stamp"
    Copy-Item -Recurse -Force "$InstallDir\config" $Backup
    Info "已备份配置到 $Backup"
}

# ── 停止旧服务 ────────────────────────────────────────────────────────────────
if ($Reinstall) {
    Heading "停止旧服务"
    try {
        Stop-ScheduledTask -TaskName 'Kiro API Proxy' -ErrorAction SilentlyContinue
        Info "已停止计划任务"
    } catch { }
}

# ── 创建目录结构 ──────────────────────────────────────────────────────────────
Heading "创建目录"
New-Item -ItemType Directory -Force "$InstallDir\config", "$InstallDir\scripts" | Out-Null
Info "目录就绪：$InstallDir"

# ── 创建 venv 并安装 ──────────────────────────────────────────────────────────
Heading "安装 Python 包"
if ($Reinstall -and (Test-Path "$InstallDir\venv")) {
    Remove-Item -Recurse -Force "$InstallDir\venv"
}

$pyArgs = if ($Python -eq 'py') { @('-3', '-m', 'venv', "$InstallDir\venv") } `
          else { @('-m', 'venv', "$InstallDir\venv") }
& $Python @pyArgs

$Pip    = "$InstallDir\venv\Scripts\pip.exe"
$PyExe  = "$InstallDir\venv\Scripts\python.exe"
& $Pip install --quiet --upgrade pip
& $Pip install --quiet $SourceDir
Info "已安装到 $InstallDir\venv"

# ── 生成 API 密钥 ─────────────────────────────────────────────────────────────
Heading "生成代理 API 密钥"
$KeyFile = "$InstallDir\config\.env.proxy-api-key"
if (-not (Test-Path $KeyFile) -or $Reinstall) {
    & $PyExe -c "import secrets,sys; open(sys.argv[1],'w').write(secrets.token_hex(32))" $KeyFile
    Info "已生成密钥：$KeyFile"
} else {
    Info "保留已有密钥：$KeyFile"
}

# ── 写入 .env ─────────────────────────────────────────────────────────────────
Heading "写入配置文件"
$EnvFile = "$InstallDir\config\.env"
# .env 路径使用正斜杠（uvicorn 在 Windows 上也支持）
$KeyFileSlash   = $KeyFile   -replace '\\', '/'
$WorkDirSlash   = $WorkDir   -replace '\\', '/'
$CredsDstSlash  = $CredsDst  -replace '\\', '/'

if (-not (Test-Path $EnvFile) -or $Reinstall) {
    @"
PROXY_API_KEY_FILE=$KeyFileSlash
DEFAULT_MODEL=auto
REQUEST_TIMEOUT_SECONDS=600
KIRO_WORKING_DIRECTORY=$WorkDirSlash
KIRO_EFFORT=
RESPONSE_LANGUAGE=简体中文

MODEL_CACHE_ENABLED=true
MODEL_CACHE_TTL_SECONDS=300
MODEL_CACHE_STALE_SECONDS=3600

INCREMENTAL_STREAMING=true

RUNTIME_CREDENTIALS_FILE=$CredsDstSlash
RUNTIME_ACCOUNT_INDEX=$AcctIndex
RUNTIME_ENDPOINT=
"@ | Set-Content -Encoding UTF8 $EnvFile
    Info "已写入 $EnvFile"
} else {
    Info "保留已有 .env，如需更新请手动编辑：$EnvFile"
}

# ── 凭据文件 ──────────────────────────────────────────────────────────────────
if ($CredsSrc) {
    Copy-Item -Force $CredsSrc $CredsDst
    Info "已复制凭据到 $CredsDst"
} else {
    if (-not (Test-Path $CredsDst)) {
        Copy-Item "$SourceDir\credentials.example.json" $CredsDst
        Warn "已复制凭据示例，请在启动服务前编辑：$CredsDst"
    } else {
        Info "保留已有凭据：$CredsDst"
    }
}

# ── 注册计划任务 ──────────────────────────────────────────────────────────────
Heading "注册计划任务"

$Arguments = "-m uvicorn --env-file `"$EnvFile`" kiro_api_proxy.main:app --host 127.0.0.1 --port $Port"
$Action    = New-ScheduledTaskAction -Execute $PyExe -Argument $Arguments -WorkingDirectory $InstallDir
$Trigger   = New-ScheduledTaskTrigger -AtLogOn
$Settings  = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
             -ExecutionTimeLimit (New-TimeSpan -Hours 0)  # 不限运行时长
Register-ScheduledTask -TaskName 'Kiro API Proxy' `
    -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName 'Kiro API Proxy'
Info "已注册并启动计划任务"

# ── 验证 ──────────────────────────────────────────────────────────────────────
Heading "验证服务"

$credsContent = Get-Content $CredsDst -Raw -ErrorAction SilentlyContinue
if ($credsContent -match 'YOUR_REFRESH_TOKEN_HERE') {
    Warn "凭据尚未填写，服务可能无法启动。"
    Warn "请编辑 $CredsDst 后运行："
    Write-Host "  Stop-ScheduledTask  -TaskName 'Kiro API Proxy'"
    Write-Host "  Start-ScheduledTask -TaskName 'Kiro API Proxy'"
} else {
    Write-Host "等待服务启动..."
    Start-Sleep -Seconds 3
    try {
        Invoke-RestMethod "http://127.0.0.1:$Port/health" | Out-Null
        Info "服务正常：http://127.0.0.1:${Port}/health"
    } catch {
        Warn "健康检查未通过，请用事件查看器或以下命令排查："
        Write-Host "  Get-ScheduledTaskInfo -TaskName 'Kiro API Proxy'"
    }
}

# ── 完成摘要 ──────────────────────────────────────────────────────────────────
Heading "安装完成"
Write-Host ""
Write-Host "  管理界面    : http://127.0.0.1:${Port}/admin/"
Write-Host "  API 文档    : http://127.0.0.1:${Port}/docs"
Write-Host "  凭据文件    : $CredsDst"
Write-Host "  配置文件    : $EnvFile"
Write-Host "  API 密钥    : $(Get-Content $KeyFile -Raw)"
Write-Host ""
Write-Host "  重启服务    : Stop-ScheduledTask -TaskName 'Kiro API Proxy'; Start-ScheduledTask -TaskName 'Kiro API Proxy'"
Write-Host "  停止服务    : Stop-ScheduledTask  -TaskName 'Kiro API Proxy'"
Write-Host ""
