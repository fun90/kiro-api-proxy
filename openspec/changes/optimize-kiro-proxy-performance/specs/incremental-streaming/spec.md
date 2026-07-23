## ADDED Requirements

### Requirement: 实时转发文本增量
系统 SHALL 在上游产生文本增量后立即按目标协议发送对应 SSE 事件，不得等待完整回答生成后再发送正文。

#### Scenario: OpenAI Chat 流式响应
- **WHEN** `/v1/chat/completions` 请求启用 `stream`
- **THEN** 系统按顺序发送一个或多个 `chat.completion.chunk` 文本增量、结束块和 `[DONE]`

#### Scenario: Anthropic Messages 流式响应
- **WHEN** `/v1/messages` 请求启用 `stream`
- **THEN** 系统发送合法的 message、content block、delta 和 stop 事件序列

### Requirement: 保持字符和事件边界完整
系统 MUST 使用增量解码处理上游字节，并 SHALL 保证 UTF-8 多字节字符、JSON 和 SSE 事件不会因任意分块而损坏。

#### Scenario: 中文字符跨上游字节块
- **WHEN** 一个中文字符的 UTF-8 字节分布在多个上游块中
- **THEN** 客户端收到完整字符且所有 SSE 数据均可解析

### Requirement: 传播客户端取消
系统 SHALL 在客户端断开或取消请求时停止上游生成、释放传输资源并解除会话锁。

#### Scenario: 客户端中途断开
- **WHEN** 客户端在生成完成前关闭流式连接
- **THEN** 系统在配置的宽限期内取消上游任务且不留下孤儿进程

### Requirement: 流中错误符合协议
系统 SHALL 根据响应是否已开始选择 HTTP 错误或协议内错误事件，并不得在已发送成功状态后静默吞掉错误。

#### Scenario: 首个事件后上游失败
- **WHEN** 系统已发送 SSE 响应头和部分内容后上游失败
- **THEN** 系统发送目标协议允许的错误事件并关闭流

### Requirement: 返回非零且一致的 token 用量
系统 SHALL 将上游 ACP 用量事件映射为协议用量字段，并在上游未提供完整
统计时使用统一估算规则填充输入和输出 token，不得固定返回零。

#### Scenario: Claude Code 流式请求
- **WHEN** Claude Code 通过 Anthropic Messages SSE 完成一次生成
- **THEN** `message_start` 包含非零输入 token，且 `message_delta` 包含非零输出 token

#### Scenario: 上游提供真实用量
- **WHEN** ACP 在生成结束时提供输入、输出或缓存 token 统计
- **THEN** 系统优先采用上游统计，并保留上下文已用量与窗口大小的内部事件
