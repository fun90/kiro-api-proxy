## Why

当前代理为每次 API 请求重复查询模型、启动 `kiro-cli`、初始化认证与会话，并在完整响应结束后才一次性返回内容，导致固定延迟高、长任务无首字反馈且多轮对话无法利用会话与提示词缓存。需要在保持 OpenAI/Anthropic 兼容和企业 SSO 正确性的前提下，将代理改造成可复用、可流式、可降级的低延迟运行时。

## What Changes

- 缓存 Kiro 模型列表，支持过期刷新、主动失效与失败时使用最近成功快照。
- 将 Kiro 输出实时转换为 OpenAI 与 Anthropic SSE 事件，降低首字延迟并正确处理客户端断开。
- 引入常驻 ACP 进程池，复用 CLI 初始化、认证和模型运行时，避免每个请求创建完整进程。
- 建立客户端会话与 Kiro/ACP 会话的映射，支持有界生命周期、并发隔离、恢复与清理。
- 增加直接 Kiro Runtime 传输通道，正确区分 IAM Identity Center 区域与 Kiro Profile 区域，并以 ACP/CLI 为安全回退。
- 增加分阶段开关、运行指标、结构化日志与基准测试，确保优化可观测、可回滚。
- **BREAKING**：流式接口将从“完成后单块 SSE”改为真正的增量 SSE；依赖单块行为的非标准客户端需要适配标准事件序列。

## Capabilities

### New Capabilities

- `model-discovery-cache`: 模型发现结果的缓存、刷新、失效、容错与并发合并。
- `incremental-streaming`: OpenAI 和 Anthropic 接口的真实增量流式响应、取消传播与错误事件。
- `persistent-acp-runtime`: 常驻 ACP 进程池、健康检查、请求调度和故障恢复。
- `session-reuse`: 外部会话与 Kiro 会话的稳定映射、生命周期、并发与清理规则。
- `adaptive-runtime-transport`: 直接 Runtime、ACP 与一次性 CLI 三种传输的选择、区域解析、凭证刷新和降级策略。

### Modified Capabilities

无。

## Impact

- 主要影响 `kiro_api_proxy/main.py`，并需要拆分模型缓存、传输、会话、流式转换和配置模块。
- OpenAI `/v1/chat/completions`、`/v1/responses` 与 Anthropic `/v1/messages` 的非流式契约保持兼容，流式时序改为标准增量事件。
- 新增 ACP 协议客户端和可选直接 Runtime 客户端；继续依赖官方 `kiro-cli` 完成认证或作为回退。
- systemd 服务需要新增运行参数、健康检查与优雅关闭配置。
- 测试体系需要覆盖并发、断线、进程恢复、区域分离、凭证刷新、降级和性能基线。
