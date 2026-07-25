#Requires -Version 5.1
# 无交互安装脚本 — Kiro API Proxy
# 支持 Windows (任务计划程序)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── 辅助函数 ──────────────────────────────────────────────────────────────────
function Info($msg)    { Write-Host "√ $msg" -ForegroundColor Green }
function Warn($msg)    { Write-Host "! $msg" -ForegroundColor Yellow }
function Heading($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }

# 本脚本全程无交互，所有安装参数取固定默认值（见下方“确定安装参数”）。

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

# ── 确定安装参数（无交互，全部固定默认值）─────────────────────────────────────
# 安装目录固定为当前目录；配置目录、凭据文件均在其下的 .config\。
$InstallDir = (Get-Location).Path
$Port = "3458"
# 工作目录根放开为系统盘（该值仅作为可传给上游的路径白名单，非真实文件权限）。
$WorkDir = "C:\"
# 不在安装期指定凭据，安装后经 /admin 登录或手动编辑 runtime-credentials.json。
$CredsSrc = ''
$AcctIndex = ''
$CredsDst = "$InstallDir\.config\runtime-credentials.json"

# 已有安装则自动备份 .config\ 后覆盖（不再询问）。
$Reinstall = $false
if (Test-Path "$InstallDir\venv") {
    Warn "检测到已有安装，将备份 .config\ 后覆盖：$InstallDir"
    $Reinstall = $true
}

Heading "安装摘要"
Write-Host "  平台           : Windows"
Write-Host "  源码目录        : $SourceDir"
Write-Host "  安装目录        : $InstallDir"
Write-Host "  监听地址        : 127.0.0.1:$Port"
Write-Host "  工作目录        : $WorkDir"
Write-Host "  凭据来源        : （安装后手动填写或经 /admin 登录）"
Write-Host ""

# ── 备份（仅重装时）──────────────────────────────────────────────────────────
if ($Reinstall -and (Test-Path "$InstallDir\.config")) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $Backup = "$InstallDir\.config.bak.$stamp"
    Copy-Item -Recurse -Force "$InstallDir\.config" $Backup
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

# ── 写入配置文件 ──────────────────────────────────────────────────────────────
Heading "写入配置文件"
$ConfigFile = "$InstallDir\.config\config.json"

# 仅当 config.json 不存在或是重装时写入（重装时已备份）。api_key 默认为空、
# 不再预生成，安装后到 /admin 生成并保存；配置目录与工作目录通过计划任务启动
# 命令内联设置的环境变量传入，无需 .env。
if (-not (Test-Path $ConfigFile) -or $Reinstall) {
    $pyGenConfig = @'
import json, sys
from pathlib import Path

# 凭据路径固定为 config.json 同目录下的 runtime-credentials.json，不写入配置。
# argv[3]（账户索引）可能缺省：用 len 守卫，规避 PowerShell 5.1 丢弃空参时越界。
path, port = sys.argv[1], int(sys.argv[2])
acct = sys.argv[3] if len(sys.argv) > 3 else ""
data = {
    "api_host": "127.0.0.1",
    "api_port": port,
}
if acct.strip():
    data["runtime_account_index"] = int(acct)
p = Path(path)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
'@
    $pyGenConfig | & $PyExe - $ConfigFile $Port $AcctIndex
    Info "已写入 $ConfigFile"
} else {
    Info "保留已有配置：$ConfigFile"
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

# 计划任务无法直接给子进程注入环境变量，改由 powershell.exe 在启动命令内联
# 设置 $env: 后再拉起 uvicorn——进程创建时即生效，且不污染用户全局环境。
$PwShExe   = (Get-Command powershell.exe).Source
$InnerCmd  = "`$env:KIRO_PROXY_CONFIG_DIRECTORY='$InstallDir\.config'; " +
             "`$env:KIRO_WORKING_DIRECTORY='$WorkDir'; " +
             "& '$PyExe' -m uvicorn kiro_api_proxy.main:app --host 127.0.0.1 --port $Port"
$Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"$InnerCmd`""
$Action    = New-ScheduledTaskAction -Execute $PwShExe -Argument $Arguments -WorkingDirectory $InstallDir
$Trigger   = New-ScheduledTaskTrigger -AtLogOn
$Settings  = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
             -ExecutionTimeLimit (New-TimeSpan -Hours 0)  # 不限运行时长
Register-ScheduledTask -TaskName 'Kiro API Proxy' `
    -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName 'Kiro API Proxy'
Info "已注册并启动计划任务"

# ── 完成摘要 ──────────────────────────────────────────────────────────────────
Heading "安装完成"
Write-Host ""
Write-Host "  管理界面    : http://127.0.0.1:${Port}/admin/"
Write-Host "  API 文档    : http://127.0.0.1:${Port}/docs"
Write-Host "  凭据文件    : $CredsDst"
Write-Host "  配置文件    : $ConfigFile"
Write-Host "  API 密钥    : 未设置，请访问管理界面在“设置”页生成并保存"
Write-Host ""
Write-Host "  重启服务    : Stop-ScheduledTask -TaskName 'Kiro API Proxy'; Start-ScheduledTask -TaskName 'Kiro API Proxy'"
Write-Host "  停止服务    : Stop-ScheduledTask  -TaskName 'Kiro API Proxy'"
Write-Host ""
