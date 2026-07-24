## ADDED Requirements

### Requirement: 加载 Runtime 凭据
系统 SHALL 从配置路径加载 JSON 凭据文件，包含 `refresh_token`、
`client_id`、`client_secret`、`auth_region`、`profile_arn` 及可选的
`access_token`/`expires_at`。缺少必需字段时 Runtime 启动失败并降级。

#### Scenario: 成功加载凭据
- **WHEN** Runtime 已启用且凭据文件存在、JSON 有效且必需字段齐全
- **THEN** 系统加载凭据并供 Runtime 认证组件使用

#### Scenario: 凭据文件不存在
- **WHEN** Runtime 已启用但配置的凭据文件路径不存在
- **THEN** Runtime 启动失败并记录配置错误，系统降级到 ACP/CLI

#### Scenario: 凭据格式无效
- **WHEN** 凭据文件内容非法 JSON 或缺少 `refresh_token`/`client_id`/`client_secret`
- **THEN** Runtime 启动失败并记录不含敏感值的解析错误

#### Scenario: 权限过宽时警告
- **WHEN** POSIX 系统上凭据文件允许组或其他用户读取
- **THEN** 系统记录安全警告但不阻断启动

### Requirement: 自动刷新 Access Token
系统 SHALL 在 Access Token 缺失或过期时使用 OIDC Refresh Token 端点
获取新 Token。多个并发请求 SHALL 合并为一次刷新。刷新后回写凭据文件。

#### Scenario: Token 过期自动刷新
- **WHEN** Access Token 缺失或 `expires_at` 已过期
- **THEN** 系统调用 `https://oidc.<auth_region>.amazonaws.com/token` 获取新 Token

#### Scenario: 并发请求合并刷新
- **WHEN** 多个请求同时发现 Token 需要刷新
- **THEN** 只发送一次刷新 HTTP 请求，所有等待者复用结果

#### Scenario: 刷新后回写文件
- **WHEN** 刷新成功返回新的 Access Token（和可能的新 Refresh Token）
- **THEN** 系统将更新后的凭据写回文件

#### Scenario: 刷新失败
- **WHEN** OIDC 端点返回错误（网络不可达、invalid_grant 等）
- **THEN** 系统返回认证错误且日志中不包含完整 Token 值

### Requirement: 首次 401 安全重放
系统 SHALL 在生成请求尚未产生任何事件时，对首次 401 强制刷新并重放一次。
已输出内容后或第二次 401 不重试。

#### Scenario: 首次 401 后恢复
- **WHEN** 上游在输出事件前返回 401 且刷新成功
- **THEN** 系统使用新 Token 重放一次请求

#### Scenario: 已输出后认证失败
- **WHEN** 上游已输出事件后报告认证错误
- **THEN** 系统终止流并返回错误，不重放

#### Scenario: 重复 401
- **WHEN** 重放后仍返回 401
- **THEN** 系统返回认证错误且不再重试
