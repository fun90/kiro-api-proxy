## ADDED Requirements

### Requirement: 提供可配置的传输优先级
系统 SHALL 支持 Runtime、ACP 和一次性 CLI 传输，并允许配置启用状态、优先顺序和每种传输的超时。

#### Scenario: 默认安全配置
- **WHEN** 用户未显式启用直接 Runtime
- **THEN** 系统优先使用 ACP，并在 ACP 不可用时回退 CLI

#### Scenario: 显式启用 Runtime
- **WHEN** 管理员启用 Runtime 且健康检查通过
- **THEN** 系统优先使用 Runtime，并保留 ACP 和 CLI 降级链

### Requirement: 分离身份区域与推理区域
系统 MUST 分别解析和保存 IAM Identity Center 区域与 Kiro Profile/Runtime 区域，且 MUST 使用 Profile 区域构造推理端点。

#### Scenario: 跨区域企业 SSO
- **WHEN** Identity Center 位于 `ap-southeast-2` 且 profile ARN 位于 `us-east-1`
- **THEN** 系统使用 `ap-southeast-2` 完成身份流程并向 `us-east-1` Runtime 发送推理请求

### Requirement: 安全管理和刷新凭证
系统 MUST 从受支持的官方认证状态获得短期凭证，禁止在日志中输出令牌，并 SHALL 在认证失败时最多执行一次受控刷新。

#### Scenario: 访问令牌过期
- **WHEN** Runtime 返回可刷新的 401 或 403
- **THEN** 系统刷新凭证并重试一次，且日志中不包含原始令牌

### Requirement: 熔断并降级不健康传输
系统 SHALL 对连续网络错误、协议错误和 5xx 维护传输健康状态，并在达到阈值时临时熔断后切换到下一个传输。

#### Scenario: Runtime 连续失败
- **WHEN** Runtime 在窗口内达到配置的失败阈值
- **THEN** 系统打开熔断器、使用 ACP 处理后续请求并在冷却后探测恢复

### Requirement: 保持协议结果一致
不同传输实现 SHALL 输出相同的内部事件和错误分类，使外部 OpenAI/Anthropic 接口不因传输切换而改变结构。

#### Scenario: Runtime 降级到 ACP
- **WHEN** 一个请求从 Runtime 降级到 ACP 并成功完成
- **THEN** 客户端仍收到符合所请求 API 协议的完整响应和结束事件

### Requirement: 传输选择可观测
系统 SHALL 为每个请求记录最终传输、尝试顺序、降级原因、首字时间和总时长，且不得记录敏感凭证或完整私密提示词。

#### Scenario: 请求发生两次降级
- **WHEN** Runtime 和 ACP 均失败且 CLI 最终成功
- **THEN** 结构化日志记录三次尝试及脱敏原因，并将最终状态记为成功
