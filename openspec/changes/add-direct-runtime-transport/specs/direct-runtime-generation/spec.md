## ADDED Requirements

### Requirement: 区域化访问 Kiro 数据面
系统 SHALL 将认证区域与数据面区域分离，并 SHALL 优先从 Profile ARN 解析
数据面区域。非 `us-east-1` Profile SHALL 路由至对应区域的 Amazon Q 数据面。

#### Scenario: Profile 区域覆盖认证区域
- **WHEN** 认证区域与 Profile ARN 中的区域不同
- **THEN** Token 刷新使用认证区域而模型和生成请求使用 Profile 区域

#### Scenario: 非默认区域路由
- **WHEN** Profile ARN 的区域不是 `us-east-1`
- **THEN** 系统向 `q.<profile-region>.amazonaws.com` 发送数据面请求

#### Scenario: 区域信息缺失
- **WHEN** Profile ARN 和显式 Runtime 区域均不可用
- **THEN** 系统使用 `us-east-1` 作为兼容回退并记录非敏感诊断信息

### Requirement: 无 CLI 模型发现
Runtime 传输 SHALL 直接调用区域化 `ListAvailableModels` 并返回现有模型缓存
所需的标准模型记录。自适应路由 SHALL 优先使用第一个健康且支持模型发现的传输，
而非固定选择 CLI。

#### Scenario: 无 CLI 获取模型列表
- **WHEN** 系统只启用 Runtime 且凭据有效
- **THEN** `/v1/models` 成功返回 Runtime 数据面的可用模型

#### Scenario: Runtime 模型发现降级
- **WHEN** Runtime 模型发现发生可重试错误且 CLI 可用
- **THEN** 自适应路由使用 CLI 获取模型列表

#### Scenario: 模型发现认证失败
- **WHEN** Runtime 模型发现返回不可恢复的认证错误
- **THEN** 系统返回认证错误且不以其他端点掩盖该错误

### Requirement: 转换统一生成请求
Runtime 传输 SHALL 将 `GenerationRequest` 转换为 Kiro
`conversationState`，包含稳定 Conversation ID、每次请求唯一的 Continuation
ID、当前用户消息、历史消息、模型、推理配置及可表达的工具上下文。

#### Scenario: 多轮消息转换
- **WHEN** 请求包含 system、user 和 assistant 多轮消息
- **THEN** 系统将最后一条用户消息放入 `currentMessage` 并按顺序保留之前历史

#### Scenario: Thinking 请求转换
- **WHEN** 请求指定受支持的推理强度
- **THEN** 系统生成对应的 Kiro Thinking 指令或推理配置

#### Scenario: 合法活动工具回合
- **WHEN** 当前工具结果与最后一条 assistant 工具调用 ID 匹配
- **THEN** 系统以结构化工具结果发送该活动回合

#### Scenario: 孤立工具结果
- **WHEN** 当前工具结果无法匹配最后一条 assistant 工具调用
- **THEN** 系统将其安全转换为文本上下文而不是发送无效结构

### Requirement: 直接执行生成请求
Runtime 传输 SHALL 使用 Bearer Access Token 调用配置的 Kiro 兼容数据面，并
设置协议必需的 Content Type、客户端标识、Agent Mode 和请求 Invocation ID。
端点回退 MUST 由显式配置启用。

#### Scenario: Runtime 流式生成成功
- **WHEN** Runtime 已启用、凭据有效且主数据面返回成功
- **THEN** 系统不启动 `kiro-cli` 并将上游事件增量输出给客户端

#### Scenario: 可重试端点故障
- **WHEN** 已启用端点回退且主端点发生连接错误、超时、429 或 5xx
- **THEN** 系统在尚未输出内容时尝试下一个配置端点

#### Scenario: 不可重试上游错误
- **WHEN** 上游返回 400、401、402 或 403
- **THEN** 系统返回相应分类错误且不切换端点，首次 401 的 Token 刷新规则除外

### Requirement: 严格解析 AWS Binary Event Stream
系统 SHALL 增量解析 AWS Binary Event Stream，校验帧长度、Prelude CRC 和
Message CRC，并对单帧大小设置上限。系统 MUST NOT 为流式请求缓存完整响应。

#### Scenario: 文本和思考增量
- **WHEN** 上游发送 `assistantResponseEvent` 和 `reasoningContentEvent`
- **THEN** 系统按到达顺序输出文本与思考增量事件

#### Scenario: 工具和用量事件
- **WHEN** 上游发送工具调用、metering、context usage 或 token usage 事件
- **THEN** 系统将可表达字段转换为统一工具或用量事件

#### Scenario: CRC 校验失败
- **WHEN** Event Stream 帧的 Prelude CRC 或 Message CRC 不匹配
- **THEN** 系统终止流并返回协议错误，不继续解析后续字节

#### Scenario: 帧大小超限
- **WHEN** Event Stream 声明的帧长度超过配置上限
- **THEN** 系统在分配对应内存前拒绝该帧并返回协议错误

#### Scenario: 客户端取消
- **WHEN** 下游客户端在生成期间断开或取消请求
- **THEN** 系统关闭上游响应流并释放连接，不继续读取或重试

### Requirement: Runtime 可关闭且可降级
Runtime SHALL 默认关闭。启用后，系统 SHALL 按 `TRANSPORT_PRIORITY` 选择
Runtime、ACP 或 CLI，并使用错误类别、熔断状态及是否已输出内容决定是否降级。

#### Scenario: 默认部署保持原行为
- **WHEN** 未设置 `RUNTIME_ENABLED=true`
- **THEN** 系统不加载 Runtime 凭据且继续使用现有 ACP/CLI 路径

#### Scenario: Runtime 启动失败后降级
- **WHEN** Runtime 初始化发生可重试错误且 ACP 或 CLI 可用
- **THEN** 系统标记 Runtime 失败并继续通过下一优先级传输服务

#### Scenario: 已输出内容后失败
- **WHEN** Runtime 已输出文本或思考增量后发生错误
- **THEN** 系统向当前流返回错误且不切换传输重新生成
