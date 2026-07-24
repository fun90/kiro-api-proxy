## Context

当前代理已经通过统一的 `KiroTransport` 接口支持一次性 CLI 和常驻 ACP，
但 `RuntimeTransport` 始终返回协议错误。模型发现还固定委托给 CLI，因此即使
补全生成方法，也不能完成无 CLI 启动。

Kiro-Go 证明了另一条链路：使用 AWS OIDC 或 Kiro Social Refresh Token
换取 Access Token，以 Bearer Token 调用区域化的 Kiro/Amazon Q/
CodeWhisperer 数据面，并解析 AWS Binary Event Stream。该链路可消除二进制
依赖和进程管理开销，但端点、请求体与客户端标识均不是 Kiro 官方承诺的公共
HTTP API，必须视为可关闭、可降级的实验传输。

现有外部 OpenAI/Anthropic API、`GenerationRequest`、`GenerationEvent`
和自适应路由是稳定边界，应复用而非复制 Kiro-Go 的完整代理层。

## Goals / Non-Goals

**Goals:**

- 在未安装 `kiro-cli` 时，凭有效 Kiro 凭据完成模型发现和流式/非流式生成。
- 支持 OIDC/Builder ID 与 Social 两类 Refresh Token 刷新，并正确区分认证
  区域和 Profile 数据面区域。
- 将 Kiro 请求和 AWS Event Stream 隔离在 Runtime 模块内，对上继续使用
  `GenerationRequest` 与 `GenerationEvent`。
- 保持 Runtime 默认关闭，失败时按照现有路由语义降级至 ACP/CLI。
- 对凭据、协议边界、超时、取消和错误分类提供可测试且可观测的实现。

**Non-Goals:**

- 不承诺私有 Kiro 数据面是官方支持或长期稳定的公共 API。
- 不实现 Web 管理面板、多账号池、自动注册批量账号或规避额度限制。
- 不从 Kiro CLI 二进制中抓取或解密用户凭据。
- 不改变现有 OpenAI/Anthropic 对外请求和响应契约。
- 首期不移除 ACP/CLI，也不默认启用 Runtime。

## Decisions

### 1. 以模块化 Python 客户端补全 `RuntimeTransport`

新增凭据模型、Token Provider、Payload Codec、Event Stream Decoder 和
Runtime HTTP Client，`RuntimeTransport` 仅负责编排并转换统一事件。这样认证
刷新和二进制协议可以独立测试，也避免把私有协议扩散到 `main.py`。

备选方案是直接移植 Kiro-Go 服务或从 Python 调用 Go sidecar。该方案会形成
第二套 API、配置和生命周期，破坏当前传输抽象，因此不采用。

### 2. 凭据通过显式文件加载，环境变量仅用于单值覆盖

凭据文件包含 `auth_method`、`refresh_token`、OIDC Client ID/Secret、
认证区域、Profile ARN 和可选 Access Token/过期时间。程序要求文件为普通文件，
在 POSIX 上拒绝组或其他用户可读写的权限，并禁止在日志、异常或健康接口中输出
Token。Access Token 只在内存中刷新；Refresh Token 轮换时通过原子替换持久化，
同时保留最小化的失败恢复。

不直接读取 Kiro 内部 SQLite 或账户管理器文件，因为格式和加密策略不稳定，也
容易扩大凭据访问范围。可以后续增加显式导入命令，但运行时只消费本项目格式。

### 3. Token Provider 使用单航班刷新

当 Access Token 缺失或距离过期不足安全窗口时，按账户加锁，只允许一个刷新
请求；等待者复用结果。OIDC 使用
`https://oidc.<auth-region>.amazonaws.com/token`，Social 使用受配置约束的
Kiro Refresh Token 端点。刷新返回新 Refresh Token 时必须轮换保存。

401 只触发一次强制刷新和请求重放；403、402 和第二次 401 不重试，避免循环和
重复计费。

### 4. 数据面区域以 Profile ARN 优先

认证区域只用于 OIDC。生成和 REST 模型请求优先从
`arn:<partition>:codewhisperer:<region>:...:profile/...` 解析数据面区域；
缺失时使用显式 Runtime 区域，最后才回退 `us-east-1`。非 `us-east-1`
数据面使用 `q.<region>.amazonaws.com`。

Profile ARN 优先从凭据读取，其次调用 `ListAvailableProfiles`，最后使用刷新
响应中的 `profileArn`。解析结果在内存和凭据文件中缓存。

### 5. 端点容错受配置和错误类别约束

首期支持 Kiro Q、CodeWhisperer 和 Amazon Q 三种兼容端点，但默认只使用一个
明确配置的主端点；只有开启端点回退时才按顺序尝试其他端点。连接错误、超时、
429 和 5xx 可尝试下一端点；认证、授权、付费和请求校验错误必须立即返回。

这种策略比无条件在 429 后切换更保守，避免把同一额度问题误判成独立容量。

### 6. 请求转换保留完整客户端上下文

`GenerationRequest` 的消息被映射为 Kiro `conversationState`：最后一条用户
消息进入 `currentMessage`，之前消息进入 `history`，模型映射保持与现有
`resolve_model` 一致。系统提示通过可识别的 priming history 表达。

结构化工具仅保留一个合法的活动工具回合；不匹配的历史工具调用和结果降级为
文本，避免上游拒绝。首期若现有 `GenerationRequest` 尚未承载图片或结构化工具，
Codec 保留扩展点，但不得伪造已支持的对外能力。

### 7. 严格增量解码 AWS Event Stream

解码器按 12 字节 Prelude、Header、Payload 和尾部 CRC 增量读取，设置最大帧
大小，并校验 Prelude CRC 与 Message CRC。事件映射如下：

- `assistantResponseEvent` → `TEXT_DELTA`
- `reasoningContentEvent` → `THINKING_DELTA`
- `toolUseEvent` → 工具事件或兼容文本
- metering/token usage → `USAGE`
- 正常 EOF → `DONE`
- 上游异常帧 → 分类后的 `ERROR`

解码器不得缓存完整响应；客户端取消必须关闭 HTTP 响应流并传播至传输层。

### 8. 模型发现不再强制绑定 CLI

`AdaptiveTransport.models()` 按可用传输顺序调用支持模型发现的传输，并使用与
生成相同的熔断和错误分类；Runtime 调用区域化 `ListAvailableModels`。仅当
Runtime 不可用时才降级到 CLI，从而支持真正无 CLI 启动。

## Risks / Trade-offs

- [私有端点或 Payload 发生变化] → Runtime 默认关闭；记录不含敏感信息的协议
  错误；保留 ACP/CLI 回退和独立协议测试夹具。
- [模拟客户端标识可能触发服务条款或风控] → 文档明确非官方性质，不隐藏
  `X-Kiro-Transport: runtime` 的本地可观测信息，不提供批量账号或额度规避能力。
- [Refresh Token 泄漏] → 权限校验、原子写入、日志字段拒绝列表和异常脱敏；
  测试确保任何响应与日志不包含凭据。
- [401 重试导致重复请求] → 仅在尚未收到响应事件时重放一次；流已开始后直接
  终止并报告错误。
- [Event Stream 帧损坏或恶意长度] → CRC、长度上下界和事件负载大小校验。
- [Runtime 与 ACP 语义不完全一致] → 统一在 `GenerationEvent` 边界归一化，
  并以契约测试对两种传输运行相同场景。
- [新增 HTTP 依赖增加连接资源] → 使用单个生命周期管理的异步客户端、连接池
  上限和明确的 connect/read/total timeout。

## Migration Plan

1. 增加配置、凭据模型和纯单元测试，不改变默认传输。
2. 实现认证、区域/Profile 解析和模型发现，在模拟上游下验收。
3. 实现 Payload 与 Event Stream，并完成流式、取消、错误和重试测试。
4. 在测试账号上设置 `RUNTIME_ENABLED=true`、`TRANSPORT_PRIORITY=runtime,acp,cli`
   进行灰度，比较模型列表、首字时间、输出、用量和工具事件。
5. 验证稳定后才允许无 CLI 部署使用 `TRANSPORT_PRIORITY=runtime`。

回滚只需设置 `RUNTIME_ENABLED=false` 并重启服务；凭据文件可以保留但不再读取。
若 Runtime 协议异常，运行时熔断自动把请求交给 ACP/CLI。

## Open Questions

- Kiro 官方 `KIRO_API_KEY` 是否最终会提供可直接交换数据面 Access Token 的
  公共流程；在公开前不纳入首期。
- Kiro Social Refresh Token 端点是否存在区域化或版本化约束，需要在真实账号
  灰度中确认。
- `toolUseEvent` 是否应扩展现有公共 `GenerationEvent` 类型以原生输出工具调用，
  还是首期维持当前文本兼容边界。
