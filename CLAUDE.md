# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

把 Kiro（CodeWhisperer 数据面）包装成 OpenAI 与 Anthropic 兼容的 HTTP API。直连 CodeWhisperer/Q 端点，用 OIDC Bearer 认证，是唯一上游路径（不再回退本地 kiro-cli）。`RuntimeTransport` 基于逆向观察实现，Kiro 未公开契约，端点或协议变化时需要相应调整。

## 常用命令

```bash
# 安装（含测试依赖）
python -m venv .venv
./.venv/bin/pip install -e ".[test]"

# 本地启动（无需 .env；api_key 默认为空，无鉴权，可经 /admin 生成并保存）
./.venv/bin/uvicorn kiro_api_proxy.main:app --host 127.0.0.1 --port 3458

# 全部测试
./.venv/bin/pytest

# 单个文件 / 单个用例
./.venv/bin/pytest tests/test_api.py
./.venv/bin/pytest tests/test_api.py::test_thinking_model_alias
```

- 项目根目录已由安装脚本部署为 launchd/systemd 常驻服务，长期监听 `3458`；在项目根目录做本地验证/联调时用 `--port 3459` 启动，避开与常驻服务抢端口。
- 无 lint/格式化工具链配置，也无 CI；改动后至少跑一遍 `pytest`。
- `pytest` 已配 `asyncio_mode = auto`，`async def test_*` 直接生效，无需 `@pytest.mark.asyncio`。
- 启动时凭据文件不存在不会崩溃：`lifespan` 容忍无凭据启动，核心场景是先起服务、再经管理界面登录（见 `main._reload_transport`）。凭据文件路径固定为 `config.json` 同目录下的 `runtime-credentials.json`。

## 请求处理主链路

三套入站协议在 `main.py` 里最终收敛到同一条生成链路，改动任一协议前先看清这条链路：

1. **入站端点**（`main.py`）：`/v1/chat/completions`、`/v1/responses`、`/v1/messages` 三套请求体各自在 `schemas.py` 用 pydantic 定义（都设 `extra="allow"`，保留未知字段）。
2. **归一化为 prompt**（`prompts.py`）：`messages_to_prompt` 把多角色消息拼成单条中文提示；`content_text` 会**丢弃** `tool_use`/`tool_result` 块——工具信息走结构化通道，绝不能写进 prompt 文本，否则模型会模仿出伪工具语法。
3. **工具契约转换**（`tools.py`）：客户端 `tools` → Kiro `toolSpecification`；历史工具往返 → `toolResults` + `history`。**关键约束**：Runtime 要求最后一条 `assistantResponseMessage` 的 `toolUses` 必须与当前 `toolResults` 的 ID 完全对应，否则上游返回 HTTP 400。`*_tool_history` 校验不齐时返回 `[]`（放弃结构化历史），调用方据此把 `tool_results` 也清空。
4. **构造 `GenerationRequest`**（`transports/base.py`）→ `RuntimeTransport`（`transports/runtime.py`）：拼 `conversationState` 请求体，POST 到 `generateAssistantResponse`，返回 AWS Binary Event Stream 字节流。
5. **解码 + 映射**：`event_stream.py`（`EventStreamDecoder` 增量解码二进制帧、校验 CRC）→ `event_mapper.py`（`map_event` 把各类 Runtime 事件统一成 `GenerationEvent`：`TEXT_DELTA`/`THINKING_DELTA`/`TOOL`/`USAGE`/`DONE`/`ERROR`）。
6. **出站重组**：`main.py` 的 `chat_stream` / `anthropic_stream` / `responses_stream` 把统一事件流重新编码成各协议的 SSE；`_collect_generation` 聚合非流式响应。三者共享 `_events`（`main.py`），后者负责超时、客户端断连检测、in-flight 去重。

`transports/base.py` 的 `EventType`、`GenerationEvent`、`GenerationRequest`、`KiroTransport` 协议是入站与传输层之间的稳定契约，跨层改动从这里入手。

## 关键不变量

- **工具通道分离**：工具的定义、调用、结果只走 `GenerationRequest.tools/tool_results/history` 与 `EventType.TOOL`，永远不进 prompt 文本。修改 `prompts.content_text` 或 `tools.py` 时务必保持这一点。
- **Token 用量优先用上游真实值**：`usage.TokenUsage` 与 `event_mapper` 的 usage 提取都遵循「缺失字段不覆盖已累积真实值、不补 0」。`ensure_estimates` 的 `input_tokens` 优先级是 `max(占比换算值, context_tokens, input_tokens)`，三者全为 0 时才退回 prompt 字符估算——**字符估算只是兜底，绝不能覆盖上游真实值**（客户端靠 `input_tokens` 判断压缩时机，注入估算值会让时机偏离真实占用）。`/v1/messages/count_tokens` 是纯本地估算，不调上游。
- **上下文占比换算**：上游 `contextUsageEvent` 只给浮点百分比 `contextUsagePercentage`（不给绝对 token 数），`event_mapper` 必须与整型 token 字段分开提取（浮点过不了 `isinstance(value, int)`），由 `TokenUsage.context_usage_tokens(model)` 乘上下文窗口换算成绝对值。窗口优先取上游 `usageEvent` 的 `size`，缺失时由 `usage.context_window_for_model` 按模型版本判档：Claude ≥ 4.6（含 major ≥ 5）为 1M，4.5 及更早为 200K；版本正则同时认 `4.8` 与 `4-8` 两种写法，所以传未经 `resolve_model` 归一的客户端模型名也能判对。**档位判错会成倍失真**（opus-4.8 当成 200K 会低估 5 倍，导致客户端压缩不及时）。`config.default_context_window` 只用于 `/v1/models` 回显兜底，不参与此换算。
- **Token 刷新**：`token_provider.py` 单航班刷新（`asyncio.Lock` + 双重检查）+ 文件原子回写；`runtime.py` 首次 401 会 `force_refresh` 后重放一次。改认证流程要保住「并发只刷一次」和「回写不破坏账户数组其他项」。
- **模型缓存**：`model_cache.py` TTL + single-flight + stale-if-error；本地凭据文件 mtime 变化会自动失效缓存（`main.available_models`）。
- **思考 / 模型别名**：`main.resolve_model` 处理 `-thinking` 后缀（触发 effort）、`[1m]` 后缀、`claude-opus-4-8`→`claude-opus-4.8` 等点号别名。Anthropic 侧 `thinking.type` 也会映射成 `-thinking`（`prompts.anthropic_upstream_model`）。

## 配置与凭据

- **`config.json` 是主配置，环境变量/`.env` 只是可选覆盖**：`config.json`（默认为配置目录下的 `config.json`；配置目录默认安装目录下的 `.config/`，可用 `KIRO_PROXY_CONFIG_DIRECTORY` 覆盖）由 `admin/config_store.py` 读写，持久化 `api_host`/`api_port`/`api_key`/`runtime_account_index` 这 4 个字段（`_PERSISTED_FIELDS`）；未显式写入的字段回退到 `config.Settings.from_env()`（即环境变量/`.env`）的默认值。运行时读 `config_store.get()`，所以界面改 API Key 能即时生效。普通部署无需写 `.env`——安装脚本会把该写的都写进 `config.json`。
- **api_key 默认为空、不再自动生成**：`lifespan` 不再生成密钥，仅在 `api_key` 为空时 `logger.warning` 提醒当前无鉴权。密钥由管理界面「设置」页生成并保存（前端 `crypto.getRandomValues` 生成 32 字节 base64url，等价 `secrets.token_urlsafe(32)`），经 `/settings` 写入 `config.json`。所以「无鉴权裸奔」是未配置密钥时的正常默认状态。
- 鉴权：`main.authorize` 与 `admin/routes.require_admin_auth` 都动态读 `config_store` 的 `api_key`；`api_key` 为空时管理接口放行（未配置密钥的默认场景，首次访问 `/admin` 会提醒生成）。`/settings` 端点受 `require_admin_auth` 保护，回显真实 `api_key` 供设置页「小眼睛」查看。
- 凭据文件路径**固定不可配置**：恒为 `config.json` 同目录下的 `runtime-credentials.json`（`config_store.credentials_path`），随 `KIRO_PROXY_CONFIG_DIRECTORY` 一起移动。`config.Settings.runtime_credentials_file` 不再从环境变量读，仅由 `_reload_transport` 以该推导值填充。内容既支持单凭据对象，也支持 Kiro Account Manager 的账户数组（配 `RUNTIME_ACCOUNT_INDEX` 选账号）。加载见 `runtime_credentials.load_credentials`，格式见 `credentials.example.json`。
- 凭据/账户索引变更后，`admin/routes` 通过 `set_reload_hook` 注入的钩子调用 `main._reload_transport` **热重载** `RuntimeTransport`（避免管理模块直接依赖 transport 造成循环导入）。
- **真实账号验证**：用真实 Kiro 账号做本地联调/验证时，相关配置统一放在仓库根目录的 `.config/` 下——只需两个文件：`.config/config.json`（服务配置，含 `api_key`）和 `.config/runtime-credentials.json`（OIDC 凭据，程序会自动回写刷新）。两者必须同目录，凭据路径由 `config.json` 位置推导得出。该目录已在 `.gitignore` 中忽略，含真实凭据，切勿提交或把其中的值回显到输出。启动时用 `KIRO_PROXY_CONFIG_DIRECTORY=.config` 指向该目录即可，无需 `.env`。

## 管理界面（admin 子包）

挂在 `/admin`（静态页 `admin/static/`）与 `/admin/api/*`（`admin/routes.py`）。能力：改配置、查额度（`admin/usage.py`）、SSO 登录换凭据（`admin/sso.py`）、扫描并导入本机 kiro-cli/ide 凭据（`admin/local_import.py`，落盘与补全走 `admin/credentials_import.py`）。单凭据缺 `profile_arn` 时用 refresh_token 刷新补全。`main.py` 里 `include_router` 在所有 `@app` 路由之后、`StaticFiles` 挂载之前注册，顺序不能乱，否则会抢占 `/admin/api/*`。

## 会话与工作目录

- 会话 ID 优先级：`X-Claude-Code-Session-Id` > `X-OpenCode-Session-Id` > `X-Session-Id` > `OpenAI-Conversation-Id`，回退到 request id；响应回传 `X-Kiro-Session-Id`、`X-Claude-Code-Session-Id`。
- 工作目录：Claude Code 请求按会话 ID 读本地 `~/.claude/projects/*/​<session>.jsonl` 里的 `cwd`（`prompts.claude_session_working_directory`）；其他客户端从 prompt 里的 `Working directory:` 提取。两者都用 `validated_working_directory` 校验必须落在 `KIRO_WORKING_DIRECTORY` 根内，再作为 prompt 前缀。

## 约定

- 面向用户的自然语言输出统一简体中文（`RESPONSE_LANGUAGE`，默认「简体中文」），`messages_to_prompt` 已把该要求写进系统提示；代码、命令、路径、标识符保持原样。
- 结构化日志走 `main._log`，自动脱敏 `authorization`/`api_key`/`token`/`prompt` 字段——新增日志字段别把敏感信息塞进去。
- 该仓库用 OpenSpec 管理规格（`openspec/`，schema `spec-driven`）；有 `.codegraph/` 索引可供代码检索。
