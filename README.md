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
- Kiro CLI 不公开 token 统计，因此 usage 返回零。
- Kiro 尚未公开直接 Runtime API 契约；项目不逆向私有接口，`RuntimeTransport` 默认关闭并安全降级。

## 配置

服务从环境变量读取配置：

- `PROXY_API_KEY`：代理接口 Bearer 密钥。
- `KIRO_CLI_PATH`：默认 `kiro-cli`。
- `DEFAULT_MODEL`：默认 `auto`。
- `MAX_CONCURRENCY`：默认 `2`。
- `REQUEST_TIMEOUT_SECONDS`：默认 `900`。
- `KIRO_WORKING_DIRECTORY`：Kiro 执行目录。
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
- `RUNTIME_ENABLED`：直接 Runtime 实验开关，默认且当前必须为 `false`。
- `TRANSPORT_PRIORITY`：传输优先级，推荐 `acp,cli`。

会话 ID 按以下优先级提取：
`X-Claude-Code-Session-Id`、`X-OpenCode-Session-Id`、`X-Session-Id`、
`OpenAI-Conversation-Id`。响应通过 `X-Kiro-Session-Id` 和
`X-Claude-Code-Session-Id` 回传。

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
