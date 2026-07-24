## Context

当前代理通过统一的 `KiroTransport` 接口支持一次性 CLI 和常驻 ACP，
但 `RuntimeTransport` 始终返回协议错误。模型发现固定委托给 CLI，即使
补全生成方法，也不能完成无 CLI 启动。

Kiro-Go 证明了另一条链路：使用 AWS OIDC Refresh Token 换取 Access Token，
以 Bearer Token 调用区域化 Kiro 数据面，解析 AWS Binary Event Stream。
该链路可消除二进制依赖和进程管理开销，但端点、请求体与客户端标识不是 Kiro
官方承诺的公共 API，必须视为可关闭、可降级的实验传输。

现有 `GenerationRequest`（`prompt: str`）、`GenerationEvent` 和自适应路由
是稳定边界。`RuntimeTransport` 只需将 `request.prompt` 整体作为当前消息
发送，不需要解析结构化多轮消息，从而避免扩展接口或逆解析 prompt。

## Goals / Non-Goals

**Goals:**

- 在未安装 `kiro-cli` 时，凭有效凭据完成模型发现和流式生成。
- 支持单一凭据类型（OIDC/Builder ID）的 Refresh Token 自动刷新。
- 将 Kiro 请求和 Event Stream 隔离在 Runtime 模块内，对上继续使用
  `GenerationRequest` 与 `GenerationEvent`。
- 保持 Runtime 默认关闭，失败时按现有路由语义降级至 ACP/CLI。

**Non-Goals:**

- 不同时支持 OIDC 和 Social 两种凭据；只实现实际使用的类型。
- 不实现多端点容错或自动 Profile ARN 发现。
- 不扩展 `GenerationRequest` 的消息结构。
- 不实现结构化工具调用传递（工具事件转为文本或跳过）。
- 不实现原子文件替换或并发读写安全（单实例场景）。
- 不默认启用 Runtime，不移除 ACP/CLI。

## Decisions

### 1. prompt 直接作为 currentMessage

`GenerationRequest.prompt` 是 API handler 已序列化的完整 prompt 文本。
RuntimeTransport 将其整体放入 Kiro `conversationState.currentMessage.content`，
历史消息为空。这牺牲了多轮会话 token 优化（上游可能多消耗 token），但避免
扩展 Protocol 接口和逆解析 prompt。

备选方案是扩展 `GenerationRequest` 增加 `messages` 字段，但这会改变
KiroTransport Protocol 契约，影响 CLI/ACP 传输，不值得为实验传输引入。

### 2. 凭据文件手动配置

凭据通过 JSON 文件加载，包含 `refresh_token`、`client_id`、`client_secret`、
`auth_region`、`profile_arn` 和可选的 `access_token`/`expires_at`。

Profile ARN 直接写在凭据文件里（从 Kiro-Go 配置或 AWS Console 获取），
不自动调用 `ListAvailableProfiles`。启动时权限检查仅 warning 不阻断。

### 3. 单航班 Token 刷新 + 文件回写

Access Token 缺失或过期时，使用 `asyncio.Lock` 保证只有一个刷新请求。
OIDC 刷新调用 `https://oidc.<auth-region>.amazonaws.com/token`。刷新后
直接覆盖写入凭据文件（单实例不需要原子替换）。

首次 401 在未输出内容时强制刷新并重放一次；第二次 401 或已输出后不重试。

### 4. 单端点固定配置

配置一个 `RUNTIME_ENDPOINT`，默认从 Profile ARN 解析区域构造端点 URL
（`codewhisperer.<region>.amazonaws.com` 或 `q.<region>.amazonaws.com`）。
不做端点回退、不做多端点探测。连接错误和 5xx 由 AdaptiveTransport 降级到
ACP/CLI 处理。

### 5. 增量解码 AWS Event Stream

解码器按 12 字节 Prelude、Header、Payload 和尾部 CRC 增量读取，校验
Prelude CRC 和 Message CRC，设置帧大小上限。事件映射：

- `assistantResponseEvent` → `TEXT_DELTA`
- `reasoningContentEvent` → `THINKING_DELTA`
- `toolUseEvent` → 跳过或转为 `TEXT_DELTA` 文本
- metering/token usage → `USAGE`
- 正常 EOF → `DONE`
- 上游异常帧 → `ERROR`

不缓存完整响应；客户端取消关闭 HTTP 响应流。

### 6. models() 按传输优先级遍历

改造 `AdaptiveTransport.models()` 不再硬编码找 CLI，改为按优先级遍历
可用传输调用 `models()`，复用现有 `_available()` 熔断检查。Runtime
调用 `ListAvailableModels` 获取模型列表。

### 7. 工具事件简单处理

`toolUseEvent` 收到时转为 `TEXT_DELTA`（将工具名和参数 JSON 作为文本输出），
不实现结构化 `TOOL` 事件传递。后续如果项目支持原生 tool_calls 再扩展。

## Risks / Trade-offs

- [私有端点变化] → Runtime 默认关闭 + ACP/CLI 回退 + 协议测试夹具。
- [prompt 整体发送浪费 token] → 个人使用可接受；大模型上下文窗口足够。
- [单端点无容错] → 依赖 AdaptiveTransport 整体降级到 ACP/CLI。
- [Token 泄漏] → 日志中截断显示 `token[:8]...`；个人机器风险可控。
- [Event Stream 帧损坏] → CRC 校验 + 帧大小上限；损坏即终止流。
- [凭据文件并发写入] → 单实例单并发，不存在竞争。

## Migration Plan

1. 增加配置和凭据加载，跑通单元测试。
2. 实现 Token 刷新和 Event Stream 解码器。
3. 实现 HTTP 客户端、models() 和 stream()。
4. 替换 RuntimeTransport 占位，集成到 AdaptiveTransport。
5. 设置 `RUNTIME_ENABLED=true`、`TRANSPORT_PRIORITY=runtime,acp,cli` 验证。

回滚只需 `RUNTIME_ENABLED=false` 并重启。
