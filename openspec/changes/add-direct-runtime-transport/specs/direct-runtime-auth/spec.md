## ADDED Requirements

### Requirement: 显式加载并保护 Runtime 凭据
系统 SHALL 从明确配置的凭据文件加载 Runtime 身份，并 SHALL 支持认证方法、
Refresh Token、OIDC Client ID/Secret、认证区域、Profile ARN、Access Token
及过期时间字段。系统 MUST NOT 在日志、异常、健康响应或 API 响应中暴露任何
Token 或 Client Secret。

#### Scenario: 安全加载凭据文件
- **WHEN** Runtime 已启用且凭据文件存在、格式有效并满足权限要求
- **THEN** 系统加载凭据并仅在进程内存中提供给 Runtime 认证组件

#### Scenario: 拒绝权限过宽的凭据文件
- **WHEN** POSIX 系统上的凭据文件允许组用户或其他用户读取或写入
- **THEN** Runtime 启动失败并返回不包含凭据内容的配置错误

#### Scenario: 日志不泄漏凭据
- **WHEN** Token 刷新或上游认证请求失败且错误体包含敏感值
- **THEN** 系统记录脱敏错误且输出中不包含 Access Token、Refresh Token 或 Client Secret

### Requirement: 自动刷新 Access Token
系统 SHALL 在 Access Token 缺失、过期或进入刷新安全窗口时刷新 Token。
OIDC/Builder ID 凭据 SHALL 使用认证区域的 AWS OIDC Token 端点，Social
凭据 SHALL 使用配置允许的 Kiro Social Token 端点。

#### Scenario: OIDC Token 到期刷新
- **WHEN** OIDC 凭据的 Access Token 即将过期
- **THEN** 系统使用 Refresh Token、Client ID 和 Client Secret 获取新的 Access Token

#### Scenario: Social Token 到期刷新
- **WHEN** Social 凭据的 Access Token 即将过期
- **THEN** 系统使用 Social Refresh Token 端点获取新的 Access Token

#### Scenario: 并发请求合并刷新
- **WHEN** 多个请求同时发现同一凭据需要刷新
- **THEN** 系统只发送一次刷新请求且所有等待请求复用刷新结果

#### Scenario: 持久化轮换后的 Refresh Token
- **WHEN** Token 端点返回不同于当前值的新 Refresh Token
- **THEN** 系统通过原子文件替换保存新 Token，且并发读取不会看到部分写入内容

### Requirement: 解析并缓存 Profile ARN
系统 SHALL 优先使用凭据中的 Profile ARN；缺失时 SHALL 尝试
`ListAvailableProfiles`，并可使用 Token 刷新响应中的 Profile ARN 作为回退。
成功解析的 Profile ARN SHALL 被缓存。

#### Scenario: 通过 Profile 列表解析
- **WHEN** 凭据没有 Profile ARN 且 `ListAvailableProfiles` 返回可用 Profile
- **THEN** 系统选择有效 ARN 并将其用于后续 Runtime 请求

#### Scenario: 使用刷新响应回退
- **WHEN** Profile 列表不支持当前身份且 Token 刷新响应包含 Profile ARN
- **THEN** 系统使用刷新响应中的 Profile ARN

#### Scenario: 无可用 Profile
- **WHEN** 所有 Profile ARN 解析路径均失败
- **THEN** 系统返回分类为认证或授权的错误且不发送生成请求

### Requirement: 认证失败只允许一次安全重放
系统 SHALL 在生成请求尚未产生任何事件时，对首次 401 强制刷新 Token 并重放
一次。系统 MUST NOT 对第二次 401、403、402 或已经开始输出的请求执行重放。

#### Scenario: 首次 401 后恢复
- **WHEN** 上游在输出任何事件前返回首次 401 且刷新成功
- **THEN** 系统使用新 Access Token 重放一次原始请求

#### Scenario: 流开始后认证失败
- **WHEN** 上游已经输出事件后报告认证错误
- **THEN** 系统终止当前流并返回错误，不重放请求

#### Scenario: 重复 401
- **WHEN** 使用刷新后 Token 重放仍返回 401
- **THEN** 系统返回认证错误且不再重试
