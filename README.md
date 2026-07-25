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
./.venv/bin/uvicorn kiro_api_proxy.main:app --host 127.0.0.1 --port 3458
```

首次启动会自动生成鉴权 API Key 并写入 `config.json`（默认
`~/.config/kiro-api-proxy/config.json`），密钥打印在启动日志中，也可在
`/admin` 管理界面查看或修改。凭据无需提前配置，也无需指定路径——固定存放在
`config.json` 同目录下的 `runtime-credentials.json`：启动后经 `/admin` 用 SSO
登录或导入本机 kiro 凭据即可，代理会自动写入该文件并热重载生效。

无需手写 `.env`——所有配置项均可选，留空即用默认值或由管理界面写入
`config.json`。如需覆盖高级项（模型、超时、缓存等），可用环境变量或
`--env-file`，见文末「配置」。

服务启动后可访问 `http://127.0.0.1:3458/docs` 查看接口文档。未配置凭据
时可正常启动（先起服务、再经管理界面登录），但生成请求会失败。

## 安装

安装目录结构如下，`venv` 使用非 editable 方式安装，配置和凭据统一放在
`.config/`，不要提交到 Git：

```text
<安装目录>/
├── .config/
│   ├── config.json              # 服务配置（含 api_key），脚本预生成
│   └── runtime-credentials.json # OIDC 凭据，程序自动回写刷新
├── scripts/
└── venv/
```

### macOS / Linux（推荐）

在源码根目录执行交互式安装脚本，按提示输入安装目录、工作目录和凭据路径，
脚本会自动创建 venv、生成 API 密钥写入 `config.json`，并注册系统服务
（服务定义通过环境变量注入 `KIRO_PROXY_CONFIG_FILE`/`KIRO_WORKING_DIRECTORY`，
不再需要 `.env`）：

```bash
bash scripts/install.sh
```

脚本会在最后打印 API 密钥和常用服务管理命令。

### Windows

在源码根目录以 PowerShell 执行交互式安装脚本，按提示输入安装目录、工作目录和凭据路径，
脚本会自动创建 venv、生成 API 密钥写入 `config.json`，并注册登录时自动启动的计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

脚本会在最后打印 API 密钥和常用服务管理命令。
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

- `PROXY_API_KEY`：代理接口 Bearer 密钥。留空则首次启动自动生成随机密钥
  并写入 `config.json`（打印在启动日志），无需手动设置。`config.json` 中的
  值优先于此环境变量。
- `KIRO_PROXY_CONFIG_FILE`：管理端动态配置文件路径，其中的配置优先于
  环境变量。默认 `~/.config/kiro-api-proxy/config.json`；安装脚本通过服务
  定义的环境变量指向 `<安装目录>/.config/config.json`。
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
- 凭据文件：**不可配置路径**，固定为 `config.json` 同目录下的
  `runtime-credentials.json`，由程序（SSO 登录/导入/刷新回写）自动读写。
  内容既支持单凭据对象（含 `refresh_token`、`client_id`、`client_secret`、
  `auth_region`、`profile_arn`），也支持 Kiro Account Manager 的账户数组，
  格式参见 `credentials.example.json`。未配置凭据时服务仍可正常启动（先起
  服务、再经管理界面登录），但在此之前生成请求会失败。
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

`$PROXY_API_KEY` 为鉴权密钥，取自首次启动日志或 `config.json` 中的
`api_key`（也可在 `/admin` 管理界面查看）。

```bash
curl http://127.0.0.1:3458/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "只回复 OK"}]
  }'
```
