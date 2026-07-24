## 1. 内部契约

- [x] 1.1 在 `transports/base.py` 为 `GenerationRequest` 增加 `tools: list[dict]`
  与 `tool_results: list[dict]`（默认空），保持 `prompt` 不变
- [x] 1.2 在 `base.py` 以文档/注释固化 `EventType.TOOL` 的 data 约定
  （`{"id","name","input","stop"}`），并补充最小单测
- [x] 1.3 确认 CLI/ACP 传输对新增字段向后兼容（忽略即可，加回归测试）

## 2. 入站解析与转换

- [x] 2.1 `schemas.py`：`AnthropicRequest` 增加 `tools`、`tool_choice`；
  `ChatRequest`/`ResponsesRequest` 增加 `tools`、`tool_choice`
- [x] 2.2 新增工具转换模块/函数：Anthropic `tools` → Kiro `toolSpecification`
- [x] 2.3 OpenAI `tools`（function）→ Kiro `toolSpecification`
- [x] 2.4 `content_text`/消息处理识别 `tool_use`/`tool_result` block，避免被
  `json.dumps` 混入文本
- [x] 2.5 从 Anthropic 历史消息按 `tool_use_id` 提取 `tool_result` → `tool_results`
- [x] 2.6 从 OpenAI 历史消息按 `tool_call_id` 关联 `assistant.tool_calls` 与
  `role:tool` → `tool_results`
- [x] 2.7 打通 `_generation_request` 与各端点，把 tools/tool_results 注入
  `GenerationRequest`
- [x] 2.8 入站解析单元测试（Anthropic/OpenAI，有工具/无工具/多历史轮）

## 3. 事件映射（结构化 TOOL）

- [x] 3.1 `event_mapper.py`：`toolUseEvent` 起始/`input` 分片/`stop` 映射为
  结构化 `EventType.TOOL`，移除文本化分支
- [x] 3.2 兼容 `contextUsageEvent` 到 `USAGE`（若未覆盖）
- [x] 3.3 更新/替换 `test_tool_use_text_delta` 等相关单测为结构化断言
- [x] 3.4 构造真实分片帧的单测（start→input 分片→stop，验证 id 串联）

## 4. Runtime 请求体

- [x] 4.1 `runtime.py`：`_do_stream` 在 `userInputMessageContext` 填充 `tools`
- [x] 4.2 填充 `toolResults`（按 `toolUseId` 携带内容与状态）
- [x] 4.3 无工具时不改变请求体（回归测试）
- [x] 4.4 重建活动工具轮次的 `conversationState.history` 以关联 `toolUseId`；
  仅在最后一条 assistant `toolUses` 与当前 `toolResults` 完整匹配时保留结构化
  结果，孤立结果回退到文本上下文，避免 Runtime HTTP 400
- [x] 4.5 mock 上游集成测试：带工具规格的请求体断言 + toolUseEvent 解码闭环

## 5. 出站聚合与协议映射

- [x] 5.1 实现出站聚合器：按 `id`/`index` 管理文本块与工具块交错、累积 `input`
- [x] 5.2 Anthropic 流式：`content_block_start(tool_use)`/`input_json_delta`/
  `content_block_stop`，`stop_reason: tool_use`
- [x] 5.3 Anthropic 非流式（`anthropic_messages`）：聚合为 `tool_use` 块
- [x] 5.4 OpenAI 流式（`chat_stream`）：`tool_calls` 增量，`finish_reason: tool_calls`
- [x] 5.5 OpenAI 非流式（`chat`）：`message.tool_calls`，`finish_reason: tool_calls`
- [x] 5.6 Responses（`responses`/`responses_stream`）：function call 输出项
- [x] 5.7 `_collect_generation` 复用聚合器返回结构化工具调用
- [x] 5.8 出站单元测试（流式/非流式、纯文本/纯工具/文本+工具混合、双协议）

## 6. 端到端验证与文档

- [x] 6.1 运行完整测试套件确保无回归（112 passed）
- [x] 6.2 用真实 Runtime 验证 Anthropic 工具回路：第 1 轮 `stop_reason: tool_use`
  + 原生 `tool_use`，第 2 轮 `tool_result` 回填后返回最终文本
- [x] 6.3 验证 OpenAI `tool_calls` 闭环：`finish_reason: tool_calls` + 完整调用
- [x] 6.4 更新 README 兼容边界章节（原生工具调用已支持、边界与限制）
