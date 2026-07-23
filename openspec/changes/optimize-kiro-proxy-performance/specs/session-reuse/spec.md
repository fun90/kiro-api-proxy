## ADDED Requirements

### Requirement: 建立稳定的会话映射
系统 SHALL 将经过命名空间隔离的外部会话 ID 映射到传输 worker 和上游会话，并在映射有效期内复用。

#### Scenario: 同一客户端继续对话
- **WHEN** 同一 API Key 使用相同外部会话 ID 发送后续请求
- **THEN** 系统路由到原上游会话并复用其上下文

### Requirement: 防止跨客户端会话串扰
系统 MUST 将 API Key 或租户身份纳入会话命名空间，且不得仅凭客户端提供的裸会话 ID 跨身份复用上游会话。

#### Scenario: 两个 API Key 使用相同会话 ID
- **WHEN** 不同 API Key 都发送值相同的外部会话 ID
- **THEN** 系统创建两个相互隔离的上游会话

### Requirement: 同会话请求串行化
系统 SHALL 串行处理同一会话内会改变上下文的请求，同时允许不同会话并发执行。

#### Scenario: 同一会话并发提交两条消息
- **WHEN** 两个生成请求同时绑定到同一会话
- **THEN** 系统按确定顺序执行并确保第二个请求看到第一个请求完成后的上下文

### Requirement: 有界清理会话
系统 SHALL 按空闲 TTL 和最大容量清理会话，并在清理时释放 worker 亲和性、锁和历史数据。

#### Scenario: 会话长时间未使用
- **WHEN** 会话空闲时间超过配置 TTL
- **THEN** 系统移除映射并释放相关资源

#### Scenario: 会话数量达到上限
- **WHEN** 新会话到达且会话存储已达到最大容量
- **THEN** 系统优先清理符合条件的最久未使用会话

### Requirement: worker 故障后恢复会话
系统 SHALL 在会话绑定 worker 失效时使用客户端当前请求中的完整消息恢复会话，或在无法恢复时显式标记重建。

#### Scenario: 会话 worker 崩溃
- **WHEN** 活跃会话绑定的 ACP worker 崩溃且客户端继续请求
- **THEN** 系统在健康 worker 上恢复上下文，或创建新会话并向日志和响应元数据标记重建

### Requirement: 尊重客户端上下文压缩
系统 SHALL 在客户端完整 Prompt 显著缩短时轮换 ACP 上游会话，并 MUST 使用客户端当前提供的完整压缩上下文初始化新会话。

#### Scenario: OpenCode 压缩长会话
- **WHEN** 同一外部会话的新请求相对上一轮明显缩短
- **THEN** 系统不再只发送最新用户消息，而是创建新 ACP 会话并发送当前完整 Prompt

### Requirement: 有界管理上游上下文
系统 SHALL 限制单个 ACP 上游会话的复用轮数与估算字符数，且 MUST 仅保存有界计数元数据，不得为判断会话状态重复保存每轮完整 Prompt。

#### Scenario: ACP 会话达到配置上限
- **WHEN** 会话达到最大轮数或下一轮预计超过字符预算
- **THEN** 系统创建新 ACP 会话并用当前完整 Prompt 初始化

#### Scenario: 上游报告上下文超限
- **WHEN** ACP 在尚未输出内容前返回上下文超限错误
- **THEN** 系统重建会话并使用当前完整 Prompt 受控重试一次
