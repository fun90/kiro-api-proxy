# 直接 Kiro Runtime 支持边界

探测日期：2026-07-23。

Kiro 官方文档公开并支持的自动化入口是 `kiro-cli chat` 和 ACP。官方防火墙
文档列出了 `runtime.<region>.kiro.dev` 服务域名，但没有公开第三方可依赖的
Runtime 请求协议、鉴权头、事件格式、兼容性承诺或服务条款授权。

因此本项目不逆向、不读取或转储本地访问令牌，也不直接调用私有 Runtime
端点。`RuntimeTransport` 只保留默认关闭的安全占位和明确错误；启用后自适应
路由会自动降级至 ACP/CLI。只有在 Kiro 发布正式 API 契约并确认第三方代理
调用条款后，才能实现该实验通道。

区域元数据必须区分：

- Identity Center 区域：登录和 OIDC 令牌交换所在区域；
- Runtime 区域：Kiro Profile ARN 中的区域，也是推理和数据存储区域。

例如本机登录区域为 `ap-southeast-2`，Profile ARN 区域为 `us-east-1`，
两者不同是合法且受官方区域说明支持的配置。
