## Why

当前 `RuntimeTransport` 仅为不可用占位实现，所有生成和模型发现仍依赖
`kiro-cli` 子进程，无法在不安装 CLI 的容器或服务端环境中运行。参考
Kiro-Go 已验证的直连链路，需要增加可选的 Kiro 数据面传输，在保留
ACP/CLI 回退能力的同时支持真正的无 CLI 部署。

## What Changes

- 实现 Kiro OIDC、Builder ID 与 Social 凭据的加载、刷新和 Profile ARN
  解析，不再依赖 `kiro-cli whoami` 获取运行时身份。
- 直接调用区域化的 Kiro、Amazon Q Developer 与 CodeWhisperer 数据面，
  支持模型发现、生成请求和端点容错。
- 将现有 OpenAI/Anthropic 消息转换为 Kiro `conversationState` 请求，
  覆盖历史消息、Thinking、图片、工具定义和工具结果。
- 增量解析 AWS Binary Event Stream，并转换为现有
  `GenerationEvent` 文本、思考、工具、用量、完成和错误事件。
- 将直连 Runtime 接入现有自适应路由，支持 `runtime,acp,cli` 优先级以及
  按错误类别安全降级。
- 增加凭据脱敏、文件权限校验、日志过滤和运行时开关；默认不开启私有数据面
  传输，避免无意改变现有部署行为。
- 补充协议单元测试、模拟上游集成测试和直连/降级验证文档。

## Capabilities

### New Capabilities

- `direct-runtime-auth`: 无 CLI 场景下的 Kiro 凭据加载、OIDC/Social Token
  刷新、Profile ARN 解析及敏感信息保护。
- `direct-runtime-generation`: Kiro 私有数据面的区域路由、模型发现、请求
  转换、AWS Event Stream 解码及生成事件输出。

### Modified Capabilities

无。

## Impact

- 主要影响 `kiro_api_proxy/transports/runtime.py`、`credentials.py`、
  `config.py`、`main.py` 和传输路由，并新增 Runtime 认证、请求转换及
  Event Stream 协议模块。
- OpenAI 与 Anthropic 对外接口保持兼容；启用 Runtime 后，上游实际传输
  可能从 ACP/CLI 变为直连 HTTP。
- 新增异步 HTTP 客户端及 AWS Event Stream CRC 解码依赖，部署配置需要提供
  独立的 Kiro 凭据文件或环境变量。
- 直连端点和请求结构属于未公开契约，必须可观测、可关闭并始终保留官方
  ACP/CLI 回退路径。
