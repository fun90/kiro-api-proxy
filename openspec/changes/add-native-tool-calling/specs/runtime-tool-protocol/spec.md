## ADDED Requirements

### Requirement: 生成请求的结构化工具契约
`GenerationRequest` SHALL 携带结构化的工具规格与工具结果，供各传输在保持
现有文本 `prompt` 字段的同时传递原生工具信息。

#### Scenario: 携带工具规格
- **WHEN** API 层解析出客户端工具定义
- **THEN** `GenerationRequest` 的 `tools` 字段包含标准化工具规格列表

#### Scenario: 携带工具结果
- **WHEN** API 层解析出历史工具执行结果
- **THEN** `GenerationRequest` 的 `tool_results` 字段包含按工具调用 ID 关联的结果

#### Scenario: 无工具时为空
- **WHEN** 请求不含工具
- **THEN** `tools` 与 `tool_results` 为空，传输行为与既有一致

### Requirement: Runtime 发送工具规格与结果
Runtime 传输 SHALL 将 `GenerationRequest.tools` 填入
`userInputMessage.userInputMessageContext.tools`（`toolSpecification` 结构），
将 `tool_results` 填入 `toolResults`，并在存在多轮工具调用时按需重建
`conversationState.history` 以关联 `toolUseId`。

#### Scenario: 首轮携带工具规格
- **WHEN** 生成请求包含工具规格
- **THEN** Runtime 请求体的 `userInputMessageContext.tools` 包含对应的
  `toolSpecification`（`name`、`description`、`inputSchema.json`）

#### Scenario: 回填工具结果
- **WHEN** 生成请求包含工具结果
- **THEN** Runtime 请求体的 `toolResults` 按 `toolUseId` 携带结果内容与状态

#### Scenario: 无工具时不改变请求体
- **WHEN** 生成请求不含工具
- **THEN** Runtime 请求体不包含 `tools`/`toolResults`，与既有行为一致

### Requirement: 分片工具事件映射为结构化事件
系统 SHALL 将 Kiro 分片 `toolUseEvent`（起始、`input` 分片、`stop`）映射为
结构化 `EventType.TOOL` 事件，携带 `id`、`name`、`input` 增量与结束标志，
取代既有的文本化输出。

#### Scenario: 工具调用起始
- **WHEN** 上游发送含 `name` 与 `toolUseId` 且无 `input` 的 `toolUseEvent`
- **THEN** 系统输出携带 `id` 与 `name` 的 `EventType.TOOL` 起始事件

#### Scenario: 工具输入分片
- **WHEN** 上游发送含 `input` 片段的 `toolUseEvent`
- **THEN** 系统输出携带该 `input` 分片的 `EventType.TOOL` 增量事件

#### Scenario: 工具调用结束
- **WHEN** 上游发送含 `stop:true` 的 `toolUseEvent`
- **THEN** 系统输出标记结束的 `EventType.TOOL` 事件

#### Scenario: 不再文本化
- **WHEN** 上游发送 `toolUseEvent`
- **THEN** 系统不再将其作为 `[Tool: ...]` 文本输出为 `TEXT_DELTA`

### Requirement: 统一的工具事件消费契约
API 出站层 SHALL 消费 `EventType.TOOL` 并转换为对应协议的原生工具调用；
未启用工具或传输不产生工具事件时行为不变。

#### Scenario: ACP/CLI 工具事件复用
- **WHEN** ACP 传输产生 `EventType.TOOL`
- **THEN** API 出站层按同一契约处理，不将其静默丢弃

#### Scenario: 无工具事件保持兼容
- **WHEN** 本轮生成未产生任何 `EventType.TOOL`
- **THEN** 出站响应与既有纯文本流式/非流式行为一致
