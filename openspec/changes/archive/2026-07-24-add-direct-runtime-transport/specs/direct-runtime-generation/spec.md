## ADDED Requirements

### Requirement: 无 CLI 模型发现
Runtime 传输 SHALL 调用数据面 `ListAvailableModels` 并返回标准模型记录。
`AdaptiveTransport.models()` SHALL 按传输优先级遍历，不再硬编码 CLI。

#### Scenario: Runtime 获取模型列表
- **WHEN** 系统只启用 Runtime 且凭据有效
- **THEN** `/v1/models` 返回 Runtime 数据面的可用模型

#### Scenario: Runtime 模型发现失败降级
- **WHEN** Runtime 模型发现发生错误且 CLI 可用
- **THEN** 系统使用 CLI 获取模型列表

#### Scenario: 模型发现认证失败
- **WHEN** Runtime 模型发现返回 401/403
- **THEN** 系统返回认证错误且不掩盖

### Requirement: 流式生成请求
Runtime 传输 SHALL 将 `GenerationRequest.prompt` 整体作为
`conversationState.currentMessage.content` 发送，使用 Bearer Access Token
认证，解码 AWS Event Stream 并输出 `GenerationEvent`。

#### Scenario: 流式生成成功
- **WHEN** Runtime 已启用、凭据有效且数据面返回成功
- **THEN** 系统将上游事件增量输出给客户端，不启动 kiro-cli

#### Scenario: 连接错误或 5xx
- **WHEN** 数据面返回连接错误、超时或 5xx
- **THEN** AdaptiveTransport 降级到 ACP/CLI

#### Scenario: 不可重试错误（400/402/403）
- **WHEN** 数据面返回 400、402 或 403
- **THEN** 系统返回对应分类错误且不降级

#### Scenario: 客户端取消
- **WHEN** 下游在生成期间断开
- **THEN** 系统关闭上游响应流并释放连接

### Requirement: 解析 AWS Binary Event Stream
系统 SHALL 增量解析 Event Stream 帧，校验 Prelude CRC 和 Message CRC，
设置帧大小上限。不缓存完整响应。

#### Scenario: 文本和思考增量
- **WHEN** 上游发送 `assistantResponseEvent` 和 `reasoningContentEvent`
- **THEN** 系统输出对应的 `TEXT_DELTA` 和 `THINKING_DELTA` 事件

#### Scenario: 工具事件文本化
- **WHEN** 上游发送 `toolUseEvent`
- **THEN** 系统将工具名和参数转为 `TEXT_DELTA` 文本输出

#### Scenario: 用量事件
- **WHEN** 上游发送 token usage 或 metering 事件
- **THEN** 系统输出 `USAGE` 事件

#### Scenario: CRC 校验失败
- **WHEN** 帧的 CRC 不匹配
- **THEN** 系统终止流并返回协议错误

#### Scenario: 帧大小超限
- **WHEN** 帧声明长度超过配置上限
- **THEN** 系统拒绝该帧并返回协议错误

### Requirement: Runtime 可关闭且可降级
Runtime SHALL 默认关闭。启用后 SHALL 按 `TRANSPORT_PRIORITY` 选择传输，
已输出内容后的失败不切换传输。

#### Scenario: 默认不加载 Runtime
- **WHEN** 未设置 `RUNTIME_ENABLED=true`
- **THEN** 系统不加载凭据且使用现有 ACP/CLI 路径

#### Scenario: Runtime 启动失败降级
- **WHEN** Runtime 凭据加载或初始化失败且 ACP/CLI 可用
- **THEN** 系统标记 Runtime 不可用并使用下一优先级传输

#### Scenario: 已输出后不降级
- **WHEN** Runtime 已输出内容后发生错误
- **THEN** 系统返回错误且不切换传输重新生成
