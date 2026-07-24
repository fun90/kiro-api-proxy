# Kiro API Proxy

通过官方 Kiro CLI/ACP 将 Kiro 包装为 OpenAI 与 Anthropic 兼容 API。
热路径使用常驻 ACP worker，异常时自动降级到每请求 CLI。

> 本项目是非官方社区项目，与 Kiro 官方无隶属或背书关系。使用时需遵守
> Kiro 的服务条款、订阅限制与模型权限。

## 快速开始

前置条件：

- Python 3.11 或更高版本。
- 已安装并登录官方 `kiro-cli`。

```bash
git clone https://github.com/fun90/kiro-api-proxy.git
cd kiro-api-proxy
python -m venv .venv
./.venv/bin/pip install -e .
cp .env.example .env
```

在 `.env` 中设置 `PROXY_API_KEY`，或将
`PROXY_API_KEY_FILE` 指向权限为 `0600` 的密钥文件，然后启动：

```bash
./.venv/bin/uvicorn --env-file .env kiro_api_proxy.main:app \
  --host 127.0.0.1 --port 3458
```

服务启动后可访问 `http://127.0.0.1:3458/docs` 查看接口文档。

## 本机部署（systemd 用户服务）

以下步骤适用于使用 systemd 的 Linux 桌面或服务器。服务安装到用户目录，
无需 root 权限，默认只监听 `127.0.0.1:3458`。

### 1. 安装程序

```bash
mkdir -p "$HOME/.local/share/kiro-api-proxy"
git clone https://github.com/fun90/kiro-api-proxy.git \
  "$HOME/.local/share/kiro-api-proxy/app"
cd "$HOME/.local/share/kiro-api-proxy/app"

python -m venv "$HOME/.local/share/kiro-api-proxy/venv"
"$HOME/.local/share/kiro-api-proxy/venv/bin/pip" install -e .
```

### 2. 创建代理密钥

```bash
mkdir -p "$HOME/.config/kiro-api-proxy"
openssl rand -hex 32 > "$HOME/.config/kiro-api-proxy/api-key"
chmod 600 "$HOME/.config/kiro-api-proxy/api-key"
```

复制环境配置：

```bash
cp .env.example "$HOME/.config/kiro-api-proxy/proxy.env"
```

至少修改以下配置：

```dotenv
PROXY_API_KEY_FILE=/home/你的用户名/.config/kiro-api-proxy/api-key
KIRO_WORKING_DIRECTORY=/home/你的用户名
```

systemd 的 `EnvironmentFile` 不会展开 `$HOME` 或 `~`，因此
`proxy.env` 中的文件路径必须填写绝对路径。

### 3. 配置直连 Runtime

Runtime 支持两种凭据来源。

方式一：使用独立凭据文件。复制示例并填入自己的 Kiro OIDC 凭据：

```bash
cp credentials.example.json \
  "$HOME/.config/kiro-api-proxy/runtime-credentials.json"
chmod 600 "$HOME/.config/kiro-api-proxy/runtime-credentials.json"
```

然后在 `proxy.env` 中配置：

```dotenv
RUNTIME_ENABLED=true
RUNTIME_CREDENTIALS_FILE=/home/你的用户名/.config/kiro-api-proxy/runtime-credentials.json
RUNTIME_ACCOUNT_INDEX=
TRANSPORT_PRIORITY=runtime,acp,cli
```

方式二：复用 Kiro Account Manager 的账户文件。先查看可用账号及其零基索引，
命令不会输出 Token 或 Client Secret：

```bash
jq 'to_entries
  | map(select(.value.enabled == true and .value.status == "active"))
  | map({
      index: .key,
      label: (.value.label // .value.email // ""),
      provider: .value.provider,
      region: .value.region,
      has_profile: (.value.profileArn != null and .value.profileArn != "")
    })' \
  "$HOME/.local/share/.kiro-account-manager/accounts.json"
```

收紧账户文件权限：

```bash
chmod 600 "$HOME/.local/share/.kiro-account-manager/accounts.json"
```

在 `proxy.env` 中填写账户文件绝对路径和选中的索引：

```dotenv
RUNTIME_ENABLED=true
RUNTIME_CREDENTIALS_FILE=/home/你的用户名/.local/share/.kiro-account-manager/accounts.json
RUNTIME_ACCOUNT_INDEX=填写上一步显示的索引
TRANSPORT_PRIORITY=runtime,acp,cli
```

`runtime,acp,cli` 表示优先直连 Runtime，失败时回退到 ACP/CLI。若希望验证
完全不依赖 CLI，可临时使用 `TRANSPORT_PRIORITY=runtime`；确认无误后建议恢复
带回退的配置。

### 4. 安装 systemd 服务

```bash
mkdir -p "$HOME/.config/systemd/user"
```

创建 `~/.config/systemd/user/kiro-api-proxy.service`：

```ini
[Unit]
Description=Kiro OpenAI 与 Anthropic 兼容 API 代理
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/kiro-api-proxy/proxy.env
WorkingDirectory=%h/.local/share/kiro-api-proxy/app
ExecStart=%h/.local/share/kiro-api-proxy/venv/bin/uvicorn kiro_api_proxy.main:app --host 127.0.0.1 --port 3458
Restart=on-failure
RestartSec=2
KillMode=control-group
TimeoutStopSec=10

[Install]
WantedBy=default.target
```

加载并启动：

```bash
systemctl --user daemon-reload
systemctl --user enable --now kiro-api-proxy.service
systemctl --user status kiro-api-proxy.service --no-pager
```

如需在用户未登录时也启动服务，可由管理员执行：

```bash
sudo loginctl enable-linger "$USER"
```

### 5. 验证部署

检查健康状态：

```bash
curl -fsS http://127.0.0.1:3458/health
```

验证模型发现和真实生成：

```bash
PROXY_API_KEY="$(<"$HOME/.config/kiro-api-proxy/api-key")"

curl -fsS http://127.0.0.1:3458/v1/models \
  -H "Authorization: Bearer $PROXY_API_KEY"

curl -fsS http://127.0.0.1:3458/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.6",
    "messages": [{"role": "user", "content": "只回复 RUNTIME_OK"}]
  }'
```

确认生成请求实际使用 Runtime：

```bash
journalctl --user -u kiro-api-proxy.service --since "5 minutes ago" \
  --no-pager | grep '"transport": "runtime"'
```

预期日志包含 `first_token` 事件和 `"transport": "runtime"`。如果指定账号
返回 `MONTHLY_REQUEST_COUNT`，请选择另一个仍有可用额度的活动账号并更新
`RUNTIME_ACCOUNT_INDEX`。

### 6. 更新与回滚

更新代码并重启：

```bash
cd "$HOME/.local/share/kiro-api-proxy/app"
git pull --ff-only
"$HOME/.local/share/kiro-api-proxy/venv/bin/pip" install -e .
systemctl --user restart kiro-api-proxy.service
```

若 Runtime 私有协议发生变化，在 `proxy.env` 中设置：

```dotenv
RUNTIME_ENABLED=false
TRANSPORT_PRIORITY=acp,cli
```

然后执行：

```bash
systemctl --user restart kiro-api-proxy.service
```

## 接口

- `GET /`
- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`（Anthropic Claude API）
- `POST /v1/messages/count_tokens`
- `POST /admin/models/refresh`

## 已实现的性能能力

- 模型列表 TTL 缓存、single-flight 刷新和 stale-if-error。
- OpenAI Chat、Responses 与 Anthropic Messages 实时增量 SSE。
- 常驻 ACP worker 池、进程故障检测、CLI 自动降级和优雅关闭。
- 按 API Key/会话头隔离的会话复用、TTL 与 LRU 清理。
- request ID、实际传输、首字时间和总时长结构化日志。

## 兼容边界

- 支持原生工具调用（function calling）：解析 Anthropic `tools` 与 OpenAI
  `tools`，将客户端工具定义转换为 Kiro `toolSpecification`，并把上游
  `toolUseEvent` 映射为原生 `tool_use`（Anthropic，`stop_reason: tool_use`）
  与 `tool_calls`（OpenAI，`finish_reason: tool_calls`），流式与非流式均支持。
  客户端回填的 `tool_result`/`role:tool` 结果按工具调用 ID 关联为 Kiro
  `toolResults`，支撑 Claude Code 等 Agent 的多步工具回路。工具由客户端执行，
  代理只透传调用与结果；`KIRO_TRUST_TOOLS` 仅控制 Kiro 自身的 agent 工具。
- Claude API 支持文本消息、system、Thinking 映射、原生工具调用及 SSE。
- Anthropic 流式响应优先使用 ACP 提供的 token 统计：`message_delta`
  回传真实的 `input_tokens`、缓存与 `output_tokens`，供客户端准确计算
  会话上下文占用；上游未提供时优先采用其上报的上下文用量（`used`），
  再退回按 Prompt 与输出文本估算。
- OpenAI/Anthropic 非流式响应也复用同一生成管线，优先回传上游真实
  token 用量，仅在上游未提供时才估算；`/v1/messages/count_tokens`
  为请求前预估接口，无上游调用，始终使用字符级估算。
- OpenAI Chat Completions 在 `stream_options.include_usage=true` 时于
  `[DONE]` 前返回最终用量 chunk；Responses 流的 `response.completed`
  也包含输入、输出、缓存、推理和总 token 用量。
- Kiro 尚未公开直接 Runtime API 契约；`RuntimeTransport` 基于逆向
  观察实现，默认关闭并安全降级到 ACP/CLI。端点或协议变化时只需
  `RUNTIME_ENABLED=false` 即可回滚。

## 配置

服务从环境变量读取配置：

- `PROXY_API_KEY`：代理接口 Bearer 密钥。
- `KIRO_CLI_PATH`：默认 `kiro-cli`。
- `DEFAULT_MODEL`：默认 `auto`。
- `MAX_CONCURRENCY`：默认 `2`。
- `REQUEST_TIMEOUT_SECONDS`：请求绝对总超时，默认 `600`。
- `KIRO_WORKING_DIRECTORY`：Kiro 允许执行的根目录。Claude Code
  请求会通过会话 ID 解析本地 transcript 中的 `cwd`；其他客户端的系统
  提示中存在 `Working directory` 时也可解析。代理校验目录位于根目录内，
  再将 ACP 会话或 CLI 回退切换到对应项目。
- `KIRO_EXTRA_PATH`：额外工具目录列表；Linux/macOS 使用冒号分隔，
  Windows 使用分号分隔，配置项优先级最高。
- `KIRO_EFFORT`：可选 reasoning effort。
- `KIRO_TRUST_TOOLS`：逗号分隔工具名，`*` 表示全部信任；默认不信任工具。
- `RESPONSE_LANGUAGE`：面向用户内容的语言，默认 `简体中文`。
- `MODEL_CACHE_ENABLED`：模型缓存，默认 `true`。
- `MODEL_CACHE_TTL_SECONDS`：新鲜快照时间，默认 `300`。
- `MODEL_CACHE_STALE_SECONDS`：上游失败时最大陈旧时间，默认 `3600`。
- `INCREMENTAL_STREAMING`：标准增量 SSE，默认 `true`。
- `ACP_ENABLED`：常驻 ACP 传输，安全默认值为 `false`，验收后可开启。
- `ACP_MIN_WORKERS` / `ACP_MAX_WORKERS`：worker 池范围，默认 `1/2`。
- `ACP_QUEUE_SIZE`：有界等待队列，默认 `16`。
- `SESSION_REUSE_ENABLED`：会话复用，安全默认值为 `false`。
- `SESSION_TTL_SECONDS` / `SESSION_MAX_ENTRIES`：会话 TTL/LRU 上限。
- `SESSION_MAX_TURNS`：单个 ACP 上游会话最多复用轮数，默认 `40`。
- `SESSION_MAX_CONTEXT_CHARS`：ACP 上游会话的估算字符预算，默认
  `200000`。
- `SESSION_COMPACTION_RATIO`：当前完整 Prompt 小于上一轮该比例时，
  视为客户端已压缩上下文并轮换 ACP 会话，默认 `0.7`。
- `RUNTIME_ENABLED`：直接 Runtime 传输，默认 `false`。启用前需配置
  `RUNTIME_CREDENTIALS_FILE` 指向包含 OIDC 凭据的 JSON 文件。
- `RUNTIME_CREDENTIALS_FILE`：Runtime 凭据 JSON 文件路径（含
  `refresh_token`、`client_id`、`client_secret`、`auth_region`、
  `profile_arn`）。也可直接指向 Kiro Account Manager 的账户数组文件。
  格式参见 `credentials.example.json`。
- `RUNTIME_ACCOUNT_INDEX`：凭据文件为账户数组时，指定要使用的零基账号
  索引；普通凭据对象留空。
- `RUNTIME_ENDPOINT`：可选端点覆盖；留空时从 `profile_arn` 区域自动
  构造 `https://codewhisperer.<region>.amazonaws.com`。
- `TRANSPORT_PRIORITY`：传输优先级，推荐 `acp,cli`；启用 Runtime 后
  可设为 `runtime,acp,cli`。

Kiro 子进程会按以下顺序构造 `PATH`：`KIRO_EXTRA_PATH`、当前项目的
`.venv/bin` / `venv/bin` / `node_modules/.bin`、常见用户工具目录
（`~/.local/bin`、Cargo、npm、pnpm、Bun、Deno、Go），最后保留
systemd 原始 `PATH`。ACP worker 会绑定项目目录，确保项目级 PATH
不被其他项目的常驻 worker 环境覆盖。

会话 ID 按以下优先级提取：
`X-Claude-Code-Session-Id`、`X-OpenCode-Session-Id`、`X-Session-Id`、
`OpenAI-Conversation-Id`。响应通过 `X-Kiro-Session-Id` 和
`X-Claude-Code-Session-Id` 回传。

代理不自行调用模型生成上下文摘要。OpenCode、Claude Code 等客户端压缩
消息后，代理检测 Prompt 明显缩短并新建 ACP 会话，以客户端提供的完整
压缩上下文初始化。达到轮数/字符上限或上游返回 context overflow 时也会
轮换会话；上下文超限仅在尚未输出内容时自动重试一次。

## 回滚

无需改代码即可逐级回滚：

1. `RUNTIME_ENABLED=false`
2. `SESSION_REUSE_ENABLED=false`
3. `ACP_ENABLED=false`
4. `INCREMENTAL_STREAMING=false`

每次修改后执行 `systemctl --user restart kiro-api-proxy.service`。

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
