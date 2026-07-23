# 分阶段启用与回滚验收

验收日期：2026-07-23。

| 阶段 | 配置 | 结果 |
|---|---|---|
| 基线 CLI | 全部优化关闭 | `/v1/models` 热请求 2.37 秒，流式首字约 7.74 秒 |
| 模型缓存 | `MODEL_CACHE_ENABLED=true` | 第二次模型请求 1.28 毫秒，无新增 `--list-models` 进程 |
| 增量流 | `INCREMENTAL_STREAMING=true` | Chat 实际输出“你”“好”两个独立 chunk |
| ACP | `ACP_ENABLED=true` | 常驻一个 ACP worker，连续热请求首字中位数 2.32 秒 |
| 会话复用 | `SESSION_REUSE_ENABLED=true` | 同会话第二轮约 2.24 秒；上下文“蓝鲸”“红杉”均正确恢复 |
| Runtime | `RUNTIME_ENABLED=false` | 官方未公开 API，按安全边界不启用 |

故障与回滚验证：

- 杀掉 ACP worker 后请求自动降级成功，并重新拉起健康 worker；
- systemd 重启后旧 ACP 子进程消失，只保留新服务的 worker；
- `RuntimeTransport` 初始化失败会被熔断并降级；
- 关闭会话开关时不生成内部会话键；
- 关闭 ACP 时构造纯 `CliTransport`；
- 关闭增量流时恢复单块输出。
- 首个 ACP 文本事件的代理转发耗时实测 0.44 毫秒；
- 3 个并发请求全部成功，客户端提前断开后服务和 2 个 worker 保持健康；
- ACP worker 池满载时连续请求均立即降级，容量许可不会泄漏；
- 忙碌的会话亲和 worker 不再阻塞其他会话；代理会扩容或替换空闲的
  异模型 worker，同时继续使用会话锁保证同一会话顺序；
- OpenAI Python SDK、Anthropic Python SDK、Claude Code 2.1.217 与
  OpenCode Desktop 1.18.4 均通过标准增量 SSE；
- OpenCode 同一会话两轮请求复用相同会话 ID，第二轮正确恢复验证口令；
- 同一流式会话先发送长 Prompt、再发送压缩后的短 Prompt，两轮均返回
  完整增量 SSE 与 `[DONE]`，日志记录
  `session_rebuilt reason=client_compacted`；
- API Key 改由权限 `0600` 的文件读取，不再进入服务进程环境。

当前安装配置为缓存、增量流、ACP、会话复用开启，Runtime 关闭。
所有已知客户端通过后，临时 `SINGLE_CHUNK_STREAMING` 开关已移除；
需要回滚时使用 `INCREMENTAL_STREAMING=false`。
