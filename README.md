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

- 暂不转换 OpenAI `tools`/`tool_calls`；可通过 `KIRO_TRUST_TOOLS` 控制 Kiro 自身工具。
- Claude API 支持文本消息、system、Thinking 映射及 SSE；工具块会作为文本上下文传给 Kiro，不返回原生 `tool_use`。
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
- Kiro 尚未公开直接 Runtime API 契约；项目不逆向私有接口，`RuntimeTransport` 默认关闭并安全降级。

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
- `RUNTIME_ENABLED`：直接 Runtime 实验开关，默认且当前必须为 `false`。
- `TRANSPORT_PRIORITY`：传输优先级，推荐 `acp,cli`。

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
