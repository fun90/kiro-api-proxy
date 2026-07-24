## 1. 凭据与配置

- [x] 1.1 在 `pyproject.toml` 引入 `httpx>=0.27,<1` 运行时依赖
- [x] 1.2 扩展 `Settings`：增加 `RUNTIME_CREDENTIALS_FILE`、`RUNTIME_ENDPOINT`（可选覆盖）配置
- [x] 1.3 实现凭据 JSON 加载（必需字段校验、文件不存在/格式错误处理、权限 warning）
- [x] 1.4 添加凭据加载的单元测试（成功/缺失/无效/权限）

## 2. Token 刷新与 Event Stream 解码

- [x] 2.1 实现 OIDC Token 刷新（`asyncio.Lock` 单航班、过期检测、刷新后回写文件）
- [x] 2.2 实现 AWS Binary Event Stream 增量解码器（Prelude/Header/Payload/CRC 校验/帧大小上限）
- [x] 2.3 实现事件映射（assistantResponse→TEXT_DELTA、reasoning→THINKING_DELTA、toolUse→文本、usage→USAGE、EOF→DONE、异常→ERROR）
- [x] 2.4 添加 Token 刷新（mock HTTP）和 Event Stream 解码器（构造二进制帧）单元测试

## 3. HTTP 客户端与传输集成

- [x] 3.1 实现 `RuntimeTransport.models()`：Bearer 认证调用 `ListAvailableModels`，转换为标准模型记录
- [x] 3.2 实现 `RuntimeTransport.stream()`：构建 conversationState（prompt 直传）、POST 请求、Event Stream 解码输出
- [x] 3.3 实现首次 401 刷新重放（仅未输出时）；`generate()` 通过收集 `stream()` 实现
- [x] 3.4 修改 `AdaptiveTransport.models()` 按传输优先级遍历（复用 `_available()` 熔断检查）
- [x] 3.5 替换 RuntimeTransport 占位逻辑，接入生命周期管理（start/close）
- [x] 3.6 添加集成测试（mock 上游：成功生成、401 重放、流后失败、models 降级）

## 4. 验证与文档

- [x] 4.1 运行完整测试套件确保无回归
- [x] 4.2 更新 `.env.example` 和 README（凭据准备、启用方式、回滚方式）
- [x] 4.3 添加不含真实 Token 的凭据示例文件

## 5. 真实协议纠偏与本机验收

- [x] 5.1 将 OIDC 刷新修正为 AWS JSON camelCase 请求和响应格式
- [x] 5.2 将模型发现修正为区域化 `ListAvailableModels` GET 请求
- [x] 5.3 将生成修正为 `generateAssistantResponse`、`userInputMessage` 请求体和 Kiro 客户端头
- [x] 5.4 将 Event Stream CRC 修正为 IEEE CRC-32，并兼容数值型 metering 事件
- [x] 5.5 支持从 Kiro Account Manager 账户数组显式选择活动账号并原子回写 Token
- [x] 5.6 使用真实账号验证模型发现、文本生成、用量与完成事件
- [x] 5.7 在本机 systemd 服务启用 Runtime 优先并通过公开 API 验证实际传输
