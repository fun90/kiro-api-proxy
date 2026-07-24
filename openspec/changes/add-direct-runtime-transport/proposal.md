## Why

当前 `RuntimeTransport` 仅为不可用占位实现，所有生成和模型发现仍依赖
`kiro-cli` 子进程，无法在不安装 CLI 的容器或服务端环境中运行。参考
Kiro-Go 已验证的直连链路，需要增加可选的 Kiro 数据面传输，在保留
ACP/CLI 回退能力的同时支持真正的无 CLI 部署。

## What Changes

- 从 JSON 凭据文件加载 OIDC Refresh Token，实现单航班自动刷新和文件
  回写，不依赖 `kiro-cli` 获取运行时身份。
- 将 `GenerationRequest.prompt` 整体作为 Kiro `currentMessage` 发送，
  使用 Bearer Token 调用配置的单一数据面端点。
- 增量解析 AWS Binary Event Stream（CRC 校验、帧大小上限），转换为
  `GenerationEvent` 文本、思考、用量、完成和错误事件；工具事件文本化输出。
- 实现 `ListAvailableModels` 调用，改造 `AdaptiveTransport.models()` 按
  传输优先级遍历，支持无 CLI 启动。
- 将直连 Runtime 接入现有自适应路由，支持 `runtime,acp,cli` 优先级配置
  和首次 401 刷新重放。
- Runtime 默认关闭，失败时安全降级到 ACP/CLI，不改变现有部署行为。

## Capabilities

### New Capabilities

- `direct-runtime-auth`: 无 CLI 场景下的凭据加载、OIDC Token 自动刷新、
  单航班并发合并及首次 401 重放。
- `direct-runtime-generation`: 单端点数据面调用、模型发现、prompt 直传、
  AWS Event Stream 解码及 GenerationEvent 输出。

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
