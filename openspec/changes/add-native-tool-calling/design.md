## Context

代理当前把整条链路建模为“文本 prompt 进、文本流出”：`anthropic_to_messages` /
`responses_to_messages` → `messages_to_prompt` 把消息拍平成一段文本
（`prompts.py:36`），`content_text` 对未知 block 兜底 `json.dumps`
（`prompts.py:31`）；`RuntimeTransport._do_stream` 只发送 `content/modelId/origin`
（`runtime.py:243`）；`map_event` 把 `toolUseEvent` 文本化成 `[Tool: ...]`
（`event_mapper.py:78`）；`anthropic_stream` 写死单个 text block 与
`stop_reason: end_turn`（`main.py:560`）。`EventType.TOOL` 已在 `base.py` 定义，
ACP 也会发出（`acp.py:38`），但 `main.py` 的出站层根本不消费它。

已通过实机 spike 确认 Kiro 数据面的真实工具契约（账号 index=4，us-east-1）：

- 请求：`userInputMessage.userInputMessageContext.tools` 接受
  `{"toolSpecification": {"name","description","inputSchema": {"json": <schema>}}}`。
- 响应：分片 `toolUseEvent`，同一 `toolUseId` 串联——
  起始 `{name, toolUseId}` → 若干 `{input: "<JSON 片段>", name, toolUseId}` →
  结束 `{name, stop: true, toolUseId}`；`input` 需按顺序拼接成完整 JSON。
- 工具场景用量走 `contextUsageEvent` + `meteringEvent`，无显式 completion，
  依赖流 EOF 结束（现有 `_do_stream` 的 `if not completed: yield DONE` 已覆盖）。

该契约与 Anthropic `content_block(tool_use)` + `input_json_delta` + `stop_reason:
tool_use`、OpenAI `tool_calls` 增量 + `finish_reason: tool_calls` 一一对应。

## Goals / Non-Goals

**Goals:**

- 让 Anthropic 与 OpenAI 客户端的工具定义贯通到 Kiro，并把 Kiro 的工具调用以
  原生 `tool_use` / `tool_calls` 返回，支撑 Claude Code 等 Agent 的工具回路。
- 结构化处理入站历史工具消息（`tool_use`/`tool_result`、`tool_calls`/`role:tool`），
  以正确的 `toolResults` 关联回填给 Kiro。
- 保持无工具对话完全兼容，保留传输降级语义。

**Non-Goals:**

- 不实现工具的服务端执行（工具由客户端执行，代理只透传调用与结果）。
- 不改变 Kiro 自身 agent 工具的信任模型（`KIRO_TRUST_TOOLS` 语义不变）。
- 不为 ACP/CLI 新增工具协议实现；仅统一消费其既有 `EventType.TOOL`（能力有限
  时可安全跳过）。本变更的工具回路以 Runtime 为主验证路径。
- 不实现工具流式输入的 JSON 提前校验/纠错；分片按上游顺序透传。

## Decisions

### 1. 扩展内部契约而非解析文本

在 `GenerationRequest` 增加 `tools: list[dict]` 与 `tool_results: list[dict]`
（默认空），保留 `prompt` 不变。理由：工具规格与结果是强结构化数据，塞进文本
prompt 再逆解析既脆弱又丢信息；新增字段对 CLI/ACP 向后兼容（忽略即可）。

备选：把工具编码进 prompt 文本——否决，无法可靠还原 `toolUseId` 关联。

### 2. `EventType.TOOL` 统一 data 结构

规定 `EventType.TOOL` 的 `data`：`{"id": str, "name": str, "input": str,
"stop": bool}`，其中 `input` 为本次分片（可空），`stop` 标记结束。API 出站层
按 `id` 聚合。理由：直接对应 spike 观测到的 Runtime 分片形态，同时 ACP 的
tool_call 也能归一到该结构（名称/输入映射）。

### 3. 入站转换：结构化提取，绕开 messages_to_prompt

新增独立转换函数（`prompts.py` 或新模块），从 Anthropic/OpenAI 请求里分离出三类
信息：(a) 文本对话 → 仍走 `messages_to_prompt`；(b) `tools` → Kiro
`toolSpecification`；(c) 历史工具调用/结果 → Kiro `toolResults`（按
`tool_use_id`/`tool_call_id` 关联）。`content_text` 增加对 `tool_use`/
`tool_result` block 的识别，避免它们被 `json.dumps` 混入文本。

Claude Code 与 OpenAI Agent 每轮都会重发完整历史（含 assistant 的工具调用与
后续工具结果），因此代理可**无状态**地从入站消息重建 Kiro 所需的工具结果关联，
不引入服务端会话状态。

### 4. Runtime 请求体填充

`_do_stream` 在 `userInputMessageContext` 下按需加入 `tools` 与 `toolResults`；
当历史含多轮工具往返时，用 `conversationState.history` 承载既往
`userInputMessage`/`assistantResponseMessage`（含 `toolUses`）以维持 `toolUseId`
关联。首版可先支持“单轮工具规格 + 最近一轮 toolResults 回填”，多轮 history 重建
作为同一 change 内的递进任务。

### 5. 出站聚合与 index 管理

Anthropic 出站维护一个“当前块”状态机：文本增量落在 text 块，遇到新 `tool_use.id`
先 `content_block_stop` 关闭上一块、再开 `tool_use` 块，`input` 分片走
`input_json_delta`。结束时若出现过工具调用则 `stop_reason: tool_use`，否则
`end_turn`。OpenAI 出站把每个工具调用映射为 `tool_calls[index]`，`arguments`
分片透传，`finish_reason: tool_calls`。非流式路径（`anthropic_messages`、`chat`、
`responses`、`_collect_generation`）复用同一聚合器收敛为完整块。

### 6. 以 Runtime 为主验证，ACP/CLI 尽力兼容

工具回路的正确性以 Runtime 传输验证（spike 已证实）。ACP 的 `EventType.TOOL`
归一到同一 data 结构后由出站层消费；若 ACP/CLI 的工具语义不完整，出站层保证
“不产生工具事件即退回纯文本”，不破坏现有行为。

## Risks / Trade-offs

- [Runtime 工具协议为逆向、可能变更] → 已 spike 验证；无工具路径不受影响；
  Runtime 本身可 `RUNTIME_ENABLED=false` 回退到 ACP/CLI。
- [多轮 history 重建复杂、易错] → 首版限定单轮规格 + 最近结果回填，多轮作为
  递进任务并以 Claude Code 真实多步任务验证。
- [文本块与工具块 index 交错出错] → 出站聚合器集中管理 index，加针对性单测。
- [OpenAI 与 Anthropic 分片 arguments 累积不一致] → 统一在聚合器按 `id`/`index`
  累积，端到端断言完整 JSON 可解析。
- [ACP 工具事件结构与 Runtime 不一致] → 归一映射层吸收差异；不一致时安全跳过。

## Migration Plan

1. 扩展 `base.py` 契约与 `EventType.TOOL` data 约定，不改变默认行为。
2. 实现入站解析与转换（Anthropic 先行，OpenAI 跟进），补单测。
3. `event_mapper` 改为结构化 TOOL 事件（替换文本化），补构造帧单测。
4. `runtime.py` 请求体填充 tools/toolResults，补 mock 上游集成测试。
5. 出站聚合器接入 `anthropic_stream`/`anthropic_messages`，再接 OpenAI 路径。
6. 用 Claude Code 真实工具任务（Bash/Glob）端到端验证 `stop_reason: tool_use`
   与工具结果回填闭环。

回退：功能对无工具请求零影响；Runtime 可整体 `RUNTIME_ENABLED=false` 回退。

## Open Questions

- 多轮工具往返是否需要完整 `conversationState.history`，还是 Kiro 可仅凭
  `toolResults` + 当前消息续接？需在多步 Claude Code 任务中实测确定。
- `tool_choice` 强制/指定工具在 Kiro 侧是否有等价字段，还是只能靠 prompt 引导？
  首版按“透传定义、不强制”处理，待验证。
