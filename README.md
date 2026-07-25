# Kiro API Proxy

通过 Kiro Runtime 将 Kiro 包装为 OpenAI 与 Anthropic 兼容 API。
直连 CodeWhisperer 数据面（OIDC Bearer 认证），是唯一的上游路径。

> 本项目是非官方社区项目，与 Kiro 官方无隶属或背书关系。使用时需遵守
> Kiro 的服务条款、订阅限制与模型权限。

## 快速开始

前置条件：

- Python 3.11 或更高版本。
- 一份有效的 Kiro OIDC 凭据（`refresh_token`、`client_id`、`client_secret`、
  `auth_region`、`profile_arn`），或 Kiro Account Manager 的账户文件。

```bash
git clone https://github.com/fun90/kiro-api-proxy.git
cd kiro-api-proxy
python -m venv .venv
./.venv/bin/pip install -e .
cp .env.example .env
```

在 `.env` 中设置 `PROXY_API_KEY`（或将 `PROXY_API_KEY_FILE` 指向权限为
`0600` 的密钥文件），并配置 `RUNTIME_CREDENTIALS_FILE` 指向凭据文件，
然后启动：

```bash
./.venv/bin/uvicorn --env-file .env kiro_api_proxy.main:app \
  --host 127.0.0.1 --port 3458
```

服务启动后可访问 `http://127.0.0.1:3458/docs` 查看接口文档。未配置
`RUNTIME_CREDENTIALS_FILE` 时服务启动即失败，不再回退到本地 kiro-cli。

## 安装

建议将代码目录、安装目录和运行配置分开。安装目录结构如下：

```text
<安装目录>/
├── config/
│   ├── .env
│   ├── .env.proxy-api-key
│   └── runtime-credentials.json
├── scripts/
└── venv/
```

`venv` 使用非 editable 方式安装，修改源码不会直接影响正在运行的服务。
配置文件和凭据统一放在 `config/`，不要提交到 Git。

### Windows

以下命令在 PowerShell 中执行。先将 `$InstallDir` 改为实际安装目录：

```powershell
$SourceDir = (Get-Location).Path
$InstallDir = "D:\kiro-api-proxy"

New-Item -ItemType Directory -Force `
  "$InstallDir\config", "$InstallDir\scripts" | Out-Null
py -3.11 -m venv "$InstallDir\venv"
& "$InstallDir\venv\Scripts\python.exe" -m pip install $SourceDir
```

创建配置和密钥：

```powershell
Copy-Item "$SourceDir\.env.example" "$InstallDir\config\.env"
Copy-Item "$SourceDir\credentials.example.json" `
  "$InstallDir\config\runtime-credentials.json"
& "$InstallDir\venv\Scripts\python.exe" -c `
  "import secrets,sys; open(sys.argv[1],'w').write(secrets.token_hex(32))" `
  "$InstallDir\config\.env.proxy-api-key"
```

修改 `$InstallDir\config\.env`，路径建议使用正斜杠：

```dotenv
PROXY_API_KEY_FILE=D:/kiro-api-proxy/config/.env.proxy-api-key
KIRO_WORKING_DIRECTORY=D:/Code
RUNTIME_CREDENTIALS_FILE=D:/kiro-api-proxy/config/runtime-credentials.json
RUNTIME_ACCOUNT_INDEX=
```

填好 `runtime-credentials.json` 后，注册当前用户登录时启动的计划任务：

```powershell
$Python = "$InstallDir\venv\Scripts\python.exe"
$EnvFile = "$InstallDir\config\.env"
$Arguments = "-m uvicorn --env-file `"$EnvFile`" " +
  "kiro_api_proxy.main:app --host 127.0.0.1 --port 3458"
$Action = New-ScheduledTaskAction -Execute $Python `
  -Argument $Arguments -WorkingDirectory $InstallDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Kiro API Proxy" `
  -Action $Action -Trigger $Trigger -Settings $Settings -Force
Start-ScheduledTask -TaskName "Kiro API Proxy"
```

验证：

```powershell
Invoke-RestMethod http://127.0.0.1:3458/health
Start-Process http://127.0.0.1:3458/admin/
```

### macOS

在源码根目录执行，并将 `INSTALL_DIR` 改为实际安装目录：

```bash
SOURCE_DIR="$(pwd)"
INSTALL_DIR="/绝对路径/kiro-api-proxy"

mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/scripts"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install "$SOURCE_DIR"
install -m 600 "$SOURCE_DIR/.env.example" "$INSTALL_DIR/config/.env"
install -m 600 "$SOURCE_DIR/credentials.example.json" \
  "$INSTALL_DIR/config/runtime-credentials.json"
openssl rand -hex 32 > "$INSTALL_DIR/config/.env.proxy-api-key"
chmod 600 "$INSTALL_DIR/config/.env.proxy-api-key"
```

修改 `$INSTALL_DIR/config/.env`：

```dotenv
PROXY_API_KEY_FILE=/绝对路径/kiro-api-proxy/config/.env.proxy-api-key
KIRO_WORKING_DIRECTORY=/绝对路径/Code
RUNTIME_CREDENTIALS_FILE=/绝对路径/kiro-api-proxy/config/runtime-credentials.json
RUNTIME_ACCOUNT_INDEX=
```

填好 `runtime-credentials.json`，然后创建日志目录：

```bash
mkdir -p "$HOME/Library/LaunchAgents" \
  "$HOME/Library/Logs/kiro-api-proxy"
```

创建 `~/Library/LaunchAgents/com.fun90.kiro-api-proxy.plist`。launchd 不会展开
环境变量或 `~`，以下 `<安装目录>` 和 `<用户目录>` 必须替换为绝对路径：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.fun90.kiro-api-proxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>&lt;安装目录&gt;/venv/bin/uvicorn</string>
    <string>--env-file</string>
    <string>&lt;安装目录&gt;/config/.env</string>
    <string>kiro_api_proxy.main:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>3458</string>
  </array>
  <key>WorkingDirectory</key>
  <string>&lt;安装目录&gt;</string>
  <key>StandardOutPath</key>
  <string>&lt;用户目录&gt;/Library/Logs/kiro-api-proxy/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>&lt;用户目录&gt;/Library/Logs/kiro-api-proxy/stderr.log</string>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>5</integer>
</dict>
</plist>
```

加载服务并验证：

```bash
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.fun90.kiro-api-proxy.plist"
curl -fsS http://127.0.0.1:3458/health
open http://127.0.0.1:3458/admin/
```

重启或停止服务：

```bash
launchctl kickstart -k "gui/$(id -u)/com.fun90.kiro-api-proxy"
launchctl bootout "gui/$(id -u)/com.fun90.kiro-api-proxy"
```

### Linux

在源码根目录执行，并将 `INSTALL_DIR` 改为实际安装目录：

```bash
SOURCE_DIR="$(pwd)"
INSTALL_DIR="/绝对路径/kiro-api-proxy"

mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/scripts"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install "$SOURCE_DIR"
install -m 600 "$SOURCE_DIR/.env.example" "$INSTALL_DIR/config/.env"
install -m 600 "$SOURCE_DIR/credentials.example.json" \
  "$INSTALL_DIR/config/runtime-credentials.json"
openssl rand -hex 32 > "$INSTALL_DIR/config/.env.proxy-api-key"
chmod 600 "$INSTALL_DIR/config/.env.proxy-api-key"
```

修改 `$INSTALL_DIR/config/.env`：

```dotenv
PROXY_API_KEY_FILE=/绝对路径/kiro-api-proxy/config/.env.proxy-api-key
KIRO_WORKING_DIRECTORY=/绝对路径/Code
RUNTIME_CREDENTIALS_FILE=/绝对路径/kiro-api-proxy/config/runtime-credentials.json
RUNTIME_ACCOUNT_INDEX=
```

填好 `runtime-credentials.json`，然后创建
`~/.config/systemd/user/kiro-api-proxy.service`。systemd 单元中的
`<安装目录>` 必须替换为绝对路径：

```ini
[Unit]
Description=Kiro OpenAI 与 Anthropic 兼容 API 代理
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=<安装目录>/config/.env
WorkingDirectory=<安装目录>
ExecStart=<安装目录>/venv/bin/uvicorn kiro_api_proxy.main:app --host 127.0.0.1 --port 3458
Restart=on-failure
RestartSec=2
KillMode=control-group
TimeoutStopSec=10

[Install]
WantedBy=default.target
```

加载服务并验证：

```bash
systemctl --user daemon-reload
systemctl --user enable --now kiro-api-proxy.service
systemctl --user status kiro-api-proxy.service --no-pager
curl -fsS http://127.0.0.1:3458/health
```

管理端地址为 `http://127.0.0.1:3458/admin/`。如需在用户未登录时也运行，
可由管理员执行 `sudo loginctl enable-linger "$USER"`。

### 更新与回滚

更新前应完整备份安装目录。Windows PowerShell：

```powershell
$BackupDir = "$InstallDir-backups\$(Get-Date -Format yyyyMMdd-HHmmss)"
Copy-Item -Recurse -Force $InstallDir $BackupDir
```

macOS 或 Linux：

```bash
BACKUP_ROOT="/绝对路径/kiro-api-proxy-backups"
BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_ROOT"
cp -R -p "$INSTALL_DIR" "$BACKUP_DIR"
```

更新源码后，使用安装目录中的 Python 重新安装，再通过对应平台的任务管理器、
launchd 或 systemd 重启服务：

```bash
git -C "$SOURCE_DIR" pull --ff-only
"$INSTALL_DIR/venv/bin/pip" install --upgrade --force-reinstall "$SOURCE_DIR"
```

Windows 使用等价的 PowerShell 命令：

```powershell
git -C $SourceDir pull --ff-only
& "$InstallDir\venv\Scripts\python.exe" -m pip install `
  --upgrade --force-reinstall $SourceDir
Stop-ScheduledTask -TaskName "Kiro API Proxy"
Start-ScheduledTask -TaskName "Kiro API Proxy"
```

如果新版验证失败，停止服务，将当前安装目录移走，把对应备份恢复为原安装目录，
再重新启动服务。

## 接口

- `GET /`
- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`（Anthropic Claude API）
- `POST /v1/messages/count_tokens`
- `POST /admin/models/refresh`

## 已实现的能力

- 模型列表 TTL 缓存、single-flight 刷新和 stale-if-error。
- OpenAI Chat、Responses 与 Anthropic Messages 实时增量 SSE。
- OIDC Access Token 过期检测、单航班刷新与文件回写；首次 401 自动刷新
  重放一次。
- request ID、传输、首字时间和总时长结构化日志。

## 兼容边界

- 支持原生工具调用（function calling）：解析 Anthropic `tools` 与 OpenAI
  `tools`，将客户端工具定义转换为 Kiro `toolSpecification`，并把上游
  `toolUseEvent` 映射为原生 `tool_use`（Anthropic，`stop_reason: tool_use`）
  与 `tool_calls`（OpenAI，`finish_reason: tool_calls`），流式与非流式均支持。
  客户端回填的 `tool_result`/`role:tool` 结果按工具调用 ID 关联为 Kiro
  `toolResults`，支撑 Claude Code 等 Agent 的多步工具回路。工具由客户端执行，
  代理只透传调用与结果。
- Claude API 支持文本消息、system、Thinking 映射、原生工具调用及 SSE。
- 流式与非流式响应优先采用上游上报的真实 token 用量：`message_delta`／
  `usage` 回传 `input_tokens`、缓存与 `output_tokens`，供客户端准确计算
  会话上下文占用；上游未提供时优先采用其上报的上下文用量（`used`），
  再退回按 Prompt 与输出文本估算。`/v1/messages/count_tokens`
  为请求前预估接口，无上游调用，始终使用字符级估算。
- OpenAI Chat Completions 在 `stream_options.include_usage=true` 时于
  `[DONE]` 前返回最终用量 chunk；Responses 流的 `response.completed`
  也包含输入、输出、缓存、推理和总 token 用量。
- Kiro 尚未公开直接 Runtime API 契约；`RuntimeTransport` 基于逆向
  观察实现。端点或协议变化时可能需要调整实现。

## 配置

服务从环境变量读取配置：

- `PROXY_API_KEY`：代理接口 Bearer 密钥。也可用 `PROXY_API_KEY_FILE`
  指向密钥文件（建议权限 `0600`）。
- `DEFAULT_MODEL`：默认 `auto`。
- `REQUEST_TIMEOUT_SECONDS`：请求绝对总超时，默认 `600`。
- `KIRO_WORKING_DIRECTORY`：允许解析的工作目录根。Claude Code
  请求会通过会话 ID 解析本地 transcript 中的 `cwd`；其他客户端的系统
  提示中存在 `Working directory` 时也可解析。代理校验目录位于根目录内，
  再作为 prompt 前缀提示上游。
- `KIRO_EFFORT`：可选 reasoning effort。
- `RESPONSE_LANGUAGE`：面向用户内容的语言，默认 `简体中文`。
- `MODEL_CACHE_ENABLED`：模型缓存，默认 `true`。
- `MODEL_CACHE_TTL_SECONDS`：新鲜快照时间，默认 `300`。
- `MODEL_CACHE_STALE_SECONDS`：上游失败时最大陈旧时间，默认 `3600`。
- `INCREMENTAL_STREAMING`：标准增量 SSE，默认 `true`。
- `RUNTIME_CREDENTIALS_FILE`：Runtime 凭据 JSON 文件路径（含
  `refresh_token`、`client_id`、`client_secret`、`auth_region`、
  `profile_arn`）。也可直接指向 Kiro Account Manager 的账户数组文件。
  格式参见 `credentials.example.json`。未配置时服务启动即失败。
- `RUNTIME_ACCOUNT_INDEX`：凭据文件为账户数组时，指定要使用的零基账号
  索引；普通凭据对象留空。
- `RUNTIME_ENDPOINT`：可选端点覆盖；留空时从 `profile_arn` 区域自动
  构造 `https://codewhisperer.<region>.amazonaws.com`。
- `DEFAULT_CONTEXT_WINDOW`：模型未上报上下文窗口时的兜底值，默认
  `200000`。

会话 ID 按以下优先级提取：
`X-Claude-Code-Session-Id`、`X-OpenCode-Session-Id`、`X-Session-Id`、
`OpenAI-Conversation-Id`。响应通过 `X-Kiro-Session-Id` 和
`X-Claude-Code-Session-Id` 回传。

## 使用

```bash
curl http://127.0.0.1:3458/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "只回复 OK"}]
  }'
```
