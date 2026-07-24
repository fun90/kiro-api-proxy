## 1. 配置与凭据基础

- [ ] 1.1 扩展 `Settings` 与 `.env.example`，增加 Runtime 凭据文件、端点、区域、刷新窗口、帧大小和端点回退配置
- [ ] 1.2 定义 Runtime 凭据数据模型、JSON 校验和兼容版本字段
- [ ] 1.3 实现 POSIX 文件权限检查、凭据字段脱敏和原子凭据文件更新
- [ ] 1.4 添加凭据加载、权限拒绝、原子更新和日志不泄漏的单元测试

## 2. Token 与 Profile 生命周期

- [ ] 2.1 实现共用异步 HTTP 客户端及 OIDC Refresh Token 请求
- [ ] 2.2 实现受配置约束的 Kiro Social Refresh Token 请求
- [ ] 2.3 实现 Access Token 过期安全窗口、单航班并发刷新和 Refresh Token 轮换
- [ ] 2.4 实现 `ListAvailableProfiles`、刷新响应回退及 Profile ARN 缓存
- [ ] 2.5 实现认证区域与 Profile 数据面区域解析，包括非 `us-east-1` 的 Q 端点映射
- [ ] 2.6 添加 OIDC、Social、并发刷新、Profile 回退和区域分离的模拟 HTTP 测试

## 3. Kiro 请求转换

- [ ] 3.1 定义 Kiro `conversationState`、历史消息、推理配置、工具和图片请求模型
- [ ] 3.2 实现稳定 Conversation ID、唯一 Continuation ID 及 system/user/assistant 历史转换
- [ ] 3.3 实现模型别名和 Thinking 配置映射，并保持与现有 `resolve_model` 行为一致
- [ ] 3.4 实现活动工具回合匹配、结构化工具结果和孤立工具上下文文本降级
- [ ] 3.5 添加多轮消息、Thinking、工具回合、压缩后孤立工具结果和请求截断测试

## 4. AWS Event Stream

- [ ] 4.1 实现增量 AWS Binary Event Stream 帧读取、长度上限及 Header 解码
- [ ] 4.2 实现 Prelude CRC 和 Message CRC 校验及协议错误分类
- [ ] 4.3 将文本、思考、工具、metering、context usage 和 token usage 映射为统一 `GenerationEvent`
- [ ] 4.4 实现累计/增量内容归一化、正常完成、异常事件和 EOF 行为
- [ ] 4.5 添加分片读取、多个连续帧、CRC 损坏、超大帧、工具事件和用量事件测试

## 5. Runtime HTTP 客户端

- [ ] 5.1 实现区域化 `ListAvailableModels` 并转换为现有模型记录
- [ ] 5.2 实现 `generateAssistantResponse` 请求头、Bearer 认证、连接池和分阶段超时
- [ ] 5.3 实现显式配置的 Kiro、CodeWhisperer 和 Amazon Q 端点选择
- [ ] 5.4 实现连接错误、超时、429 和 5xx 的端点回退，以及 400/401/402/403 的终止规则
- [ ] 5.5 实现首次 401 强制刷新后单次重放，并禁止已输出内容后的重放
- [ ] 5.6 实现下游取消到 HTTP 响应流关闭和连接释放的传播
- [ ] 5.7 添加成功生成、端点回退、认证重放、流后失败和取消的模拟上游集成测试

## 6. 传输与应用集成

- [ ] 6.1 用真实实现替换 `RuntimeTransport` 占位逻辑并接入生命周期管理
- [ ] 6.2 使 `generate` 和 `stream` 复用统一 Runtime 客户端且正确转换错误类别
- [ ] 6.3 修改 `AdaptiveTransport.models()`，按健康传输优先级发现模型并支持 Runtime 到 CLI 降级
- [ ] 6.4 调整应用启动逻辑，使 Runtime-only 模式不构造、不启动且不调用 `kiro-cli`
- [ ] 6.5 保持 `RUNTIME_ENABLED=false` 默认行为，并验证 `runtime,acp,cli` 配置顺序
- [ ] 6.6 添加 Runtime-only、Runtime→ACP/CLI 降级、熔断恢复和已输出后不降级测试

## 7. 验证与文档

- [ ] 7.1 更新 README，说明无 CLI 部署、凭据准备、私有协议风险、启用方式和回滚方式
- [ ] 7.2 添加不包含真实 Token 的凭据示例，并记录 `0600` 权限要求
- [ ] 7.3 编写测试账号灰度清单，覆盖模型列表、文本、Thinking、工具、长流、用量、取消和区域路由
- [ ] 7.4 运行完整测试套件、类型/格式检查和 Runtime 协议夹具测试
- [ ] 7.5 对直连 Runtime 与 ACP 运行相同契约用例并记录已知语义差异
- [ ] 7.6 验证关闭 Runtime 后无需迁移即可恢复现有 ACP/CLI 行为
