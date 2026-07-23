## ADDED Requirements

### Requirement: 复用常驻 ACP 进程
系统 SHALL 使用常驻 Kiro ACP 进程处理生成请求，并在健康进程可用时避免为每个请求重新启动完整 CLI。

#### Scenario: 连续热请求
- **WHEN** 两个无会话请求依次到达且 ACP worker 健康
- **THEN** 两个请求复用已启动的 ACP 进程池且不产生每请求 CLI 冷启动

### Requirement: 有界进程池调度
系统 SHALL 将 ACP worker 数量限制在配置的最小值和最大值之间，并按负载及会话亲和性调度请求。

#### Scenario: 并发请求达到池容量
- **WHEN** 所有 worker 均忙且新的请求到达
- **THEN** 系统在有界队列中等待或返回明确的过载错误，不得无限创建进程

### Requirement: 检测并恢复 worker 故障
系统 SHALL 监控 ACP 进程退出、JSON-RPC 协议错误和心跳超时，并替换不健康 worker。

#### Scenario: ACP 进程意外退出
- **WHEN** 正在服务的 ACP worker 意外退出
- **THEN** 系统标记该 worker 不健康、清理关联请求并补充新的健康 worker

### Requirement: ACP 不可用时回退 CLI
系统 SHALL 在 ACP 初始化或能力协商失败时按配置回退一次性 CLI 传输，并记录降级原因。

#### Scenario: 当前 Kiro CLI 不支持所需 ACP 能力
- **WHEN** ACP 启动成功但缺少请求所需的模型或生成能力
- **THEN** 系统使用 CLI 传输完成请求或返回明确的不支持错误

### Requirement: 优雅关闭常驻进程
系统 SHALL 在服务停止时停止接收新请求、等待宽限期并终止剩余 ACP 子进程。

#### Scenario: systemd 重启服务
- **WHEN** 服务收到终止信号
- **THEN** 系统在宽限期内完成或取消活动请求并确保没有遗留 ACP 进程
