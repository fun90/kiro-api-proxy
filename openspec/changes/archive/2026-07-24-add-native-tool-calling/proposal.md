## Why

代理目前把所有请求拍平成文本 prompt、把所有上游输出当作文本流，管线里没有
任何结构化工具概念。客户端（Claude Code、OpenAI 兼容 Agent）传入的 `tools`
定义被丢弃，Kiro 返回的 `toolUseEvent` 被文本化成 `[Tool: ...]`，对外固定回
`stop_reason: end_turn` / `finish_reason: stop`，从不返回原生 `tool_use` 或
`tool_calls`。因此 Claude Code 等 Agent 客户端无法执行 Bash/Glob 等工具，每轮
只会说“让我调用工具”然后正常结束回合，工具类任务完全不可用。

已通过直连 Runtime 的协议探测确认：Kiro `generateAssistantResponse` 接受客户端
`userInputMessageContext.tools`（`toolSpecification`），并以分片 `toolUseEvent`
（start / input 分片 / stop，带 `toolUseId`）返回工具调用交客户端执行，而非自行
执行。该行为与 Anthropic `tool_use` 和 OpenAI `tool_calls` 的流式契约一一对应，
打通工具协议即可让 Agent 客户端正常工作。

## What Changes

- 扩展内部契约：`GenerationRequest` 增加结构化 `tools` 与 `tool_results`；
  统一 `EventType.TOOL` 的 `data` 结构（`id`、`name`、`input` 增量、`stop`）。
- Anthropic 入站：解析 `tools`/`tool_choice`，将请求消息中的 `tool_use` /
  `tool_result` content block 结构化提取（不再 `json.dumps` 成文本），转换为
  Kiro `toolSpecification` 与 `toolResults`。
- OpenAI 入站：解析 `tools`/`tool_choice`，将历史 `assistant.tool_calls` 与
  `role:tool` 消息结构化提取并同样转换。
- Runtime 出站：在 `userInputMessage.userInputMessageContext` 填充 `tools` 与
  `toolResults`，按需重建 `conversationState.history` 关联多轮工具调用。
- 事件映射：`toolUseEvent`（start/input 分片/stop）映射为结构化
  `EventType.TOOL`，取代现有文本化处理（**BREAKING**：`toolUseEvent` 不再输出
  为 `TEXT_DELTA` 文本）。
- Anthropic 出站：消费 `EventType.TOOL`，发出 `content_block_start(tool_use)` /
  `input_json_delta` / `content_block_stop`，管理文本块与工具块交错的 index，
  出现工具调用时 `stop_reason` 置为 `tool_use`；非流式聚合为 `tool_use` block。
- OpenAI 出站：消费 `EventType.TOOL`，流式发出 `tool_calls` 增量，
  `finish_reason` 置为 `tool_calls`；非流式聚合为 `message.tool_calls`。
- 保持向后兼容：无工具的普通对话行为不变；ACP/CLI 传输仍可降级（其工具事件
  按同一 `EventType.TOOL` 契约处理或安全跳过）。

## Capabilities

### New Capabilities

- `native-tool-calling`: 对外 API 层的原生工具调用支持——解析 Anthropic 与
  OpenAI 入站 `tools`/`tool_choice` 及历史工具消息，出站以原生 `tool_use` /
  `tool_calls`（流式与非流式）返回工具调用并设置正确的结束原因。
- `runtime-tool-protocol`: 传输层工具协议——`GenerationRequest` 结构化工具契约、
  Runtime 发送 `toolSpecification`/`toolResults`、将分片 `toolUseEvent` 映射为
  结构化 `EventType.TOOL`。

### Modified Capabilities

无（主 `openspec/specs/` 暂无已归档 capability；`direct-runtime-generation`
中“工具事件文本化”行为的取代通过本变更的 `runtime-tool-protocol` 归档时同步）。

## Impact

- 内部契约：`transports/base.py`（`GenerationRequest`、`EventType.TOOL` data 约定）。
- 入站转换：`schemas.py`（新增 tools 字段）、`prompts.py`（结构化提取工具消息）。
- 出站与聚合：`main.py`（`anthropic_stream`、`anthropic_messages`、`chat_stream`、
  `chat`、`responses`、`responses_stream`、`_collect_generation`）。
- 传输：`transports/runtime.py`（请求体填充 tools/toolResults）、`event_mapper.py`
  （toolUseEvent → 结构化 TOOL）；`transports/acp.py` 的 `EventType.TOOL` 复用。
- 对外接口：普通对话保持兼容；Agent 客户端将开始收到原生 `tool_use`/`tool_calls`。
- 依赖：无新增第三方依赖。
- 风险：依赖逆向的 Runtime 工具协议，需保留“无工具时行为不变”与传输降级；
  已通过实机 spike 验证 `toolSpecification` 被接受且返回 `toolUseEvent`。
