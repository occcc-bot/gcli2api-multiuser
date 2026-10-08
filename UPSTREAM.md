# 上游归属与版本

本项目基于 **[su-kaka/gcli2api](https://github.com/su-kaka/gcli2api)** 二次开发。

- 上游作者与贡献者：su-kaka 及 gcli2api contributors。
- 上游源码基线：`934c8ce8b3648b2530ab8a36c409d70d699ab680`。
- 上游许可证：Cooperative Non-Commercial License (CNC-1.0)，原文保留在 `LICENSE`。
- 二开维护账号：`occcc-bot`。
- 新增能力：邀请制账户、会话、用户 API Key、凭证归属、按用户隔离的调用/重试、个人 OAuth 流程和管理页面。
- 公开发布调整：OAuth 客户端配置改为环境变量；独立 Compose 构建与测试 CI；不包含任何运行环境凭证、用户数据或部署信息。

Google API 转换、凭证后端、模型接口及原管理面板等核心实现来自上游。保留原源码与许可证署名；本项目不代表上游官方发行版。

为了避免历史中包含本地运行数据，仓库从脱敏后的源码快照开始提交，不导入 VPS 或本地工作目录的 Git 历史。上游历史与原文档可通过上述上游链接查阅。
