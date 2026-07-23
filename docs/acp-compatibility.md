# Kiro CLI 2.14 ACP 兼容矩阵

探测日期：2026-07-23。

| 能力 | 结果 | 验证方式 |
|---|---|---|
| 协议初始化 | 支持 ACP v1 | `initialize` 实测 |
| 创建会话 | 支持 | `session/new` 实测 |
| 加载会话 | 已声明支持 | `loadSession: true` |
| 模型选择 | 支持启动参数 | `kiro-cli acp --model` |
| 推理强度 | 支持启动参数 | `kiro-cli acp --effort` |
| 增量文本 | 支持 | 收到 `agent_message_chunk` |
| 思考事件 | 协议支持 | 映射 `agent_thought_chunk` |
| 工具事件 | 支持 | 映射 `tool_call`/`tool_call_update` |
| 用量事件 | 协议支持 | 映射 `usage_update` |
| 取消 | 支持 | `session/cancel` |
| 模型发现 | ACP 未定义 | 继续使用 CLI `--list-models` 并缓存 |

官方文档说明 Kiro ACP 使用 stdin/stdout 上的 JSON-RPC 2.0，并支持会话、
流式更新、取消和模型选择。本代理采用官方 `agent-client-protocol` Python
SDK，stderr 独立消费且不记录原文。
