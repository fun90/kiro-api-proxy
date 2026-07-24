## ADDED Requirements

### Requirement: Anthropic 入站工具解析
系统 SHALL 解析 Anthropic 请求的 `tools` 与 `tool_choice` 字段，并将请求消息中
的 `tool_use` 与 `tool_result` content block 结构化提取，转换为传输层的工具规格
与工具结果，而非序列化为文本。

#### Scenario: 解析工具定义
- **WHEN** Anthropic 请求包含非空 `tools` 数组
- **THEN** 系统将每个工具的 `name`/`description`/`input_schema` 转换为传输层
  工具规格并随生成请求传递

#### Scenario: 提取历史工具结果
- **WHEN** 请求消息中包含 `role:user` 的 `tool_result` block
- **THEN** 系统按 `tool_use_id` 结构化提取结果内容，作为工具结果随请求传递，
  不再作为纯文本拼入 prompt

#### Scenario: 无工具请求保持兼容
- **WHEN** Anthropic 请求不含 `tools`
- **THEN** 系统行为与既有文本对话完全一致

### Requirement: OpenAI 入站工具解析
系统 SHALL 解析 OpenAI Chat Completions 与 Responses 请求的 `tools` 与
`tool_choice` 字段，并将历史 `assistant.tool_calls` 与 `role:tool` 消息结构化
提取，转换为传输层的工具规格与工具结果。

#### Scenario: 解析 function 工具定义
- **WHEN** OpenAI 请求包含 `tools`（`type:function`）
- **THEN** 系统将每个 function 的 `name`/`description`/`parameters` 转换为传输层
  工具规格并随生成请求传递

#### Scenario: 提取历史工具调用与结果
- **WHEN** 请求消息包含 `assistant.tool_calls` 与对应的 `role:tool` 结果消息
- **THEN** 系统按 `tool_call_id` 关联并结构化提取，作为工具结果随请求传递

#### Scenario: 无工具请求保持兼容
- **WHEN** OpenAI 请求不含 `tools`
- **THEN** 系统行为与既有文本对话完全一致

### Requirement: Anthropic 出站工具调用
系统 SHALL 将传输层的结构化工具事件转换为 Anthropic 原生 `tool_use`
内容块（流式与非流式），并在存在工具调用时将结束原因设为 `tool_use`。

#### Scenario: 流式工具调用
- **WHEN** 上游在流式生成中产生工具调用事件
- **THEN** 系统按顺序发出 `content_block_start`（`type:tool_use`，含 `id`、`name`）、
  `content_block_delta`（`type:input_json_delta`，`partial_json` 分片）与
  `content_block_stop`，并正确管理与文本块交错的块 index

#### Scenario: 流式结束原因
- **WHEN** 本轮流式生成产生过至少一个工具调用
- **THEN** `message_delta` 的 `stop_reason` 为 `tool_use`

#### Scenario: 非流式工具调用聚合
- **WHEN** 非流式生成产生工具调用
- **THEN** 响应 `content` 数组包含完整的 `tool_use` 块（`id`、`name`、`input`），
  且 `stop_reason` 为 `tool_use`

#### Scenario: 文本与工具混合输出
- **WHEN** 上游同时产生文本增量与工具调用
- **THEN** 系统在同一响应中依序输出 `text` 块与 `tool_use` 块，块 index 连续且不冲突

### Requirement: OpenAI 出站工具调用
系统 SHALL 将传输层的结构化工具事件转换为 OpenAI 原生 `tool_calls`
（流式与非流式），并在存在工具调用时将 `finish_reason` 设为 `tool_calls`。

#### Scenario: 流式工具调用
- **WHEN** 上游在流式生成中产生工具调用事件
- **THEN** 系统发出带 `tool_calls` 的 delta 增量（含 `index`、`id`、
  `function.name` 与分片 `function.arguments`），末尾 chunk 的
  `finish_reason` 为 `tool_calls`

#### Scenario: 非流式工具调用聚合
- **WHEN** 非流式生成产生工具调用
- **THEN** 响应 `message.tool_calls` 包含完整调用（`id`、`function.name`、
  `function.arguments`），且 `finish_reason` 为 `tool_calls`

#### Scenario: Responses 接口工具调用
- **WHEN** Responses 接口生成产生工具调用
- **THEN** 系统输出对应的 function call 输出项与结束状态
