# 发布前凭据审计

## 发布来源与历史

GHCR 镜像只从 GitHub 事件对应的已提交源码构建，不使用生产容器 `docker commit`，不上传 VPS 的运行目录、环境文件、数据库或迁移备份。发布工作流只使用 GitHub 自动提供的 `GITHUB_TOKEN`；不要求将个人 PAT、SSH 私钥或生产密码加入仓库 Secrets。

上游公开历史包含硬编码 Google OAuth 应用客户端常量，GitHub Push Protection 会拦截。**不绕过此保护，也不把这段原始历史推送到本 fork。** 公开仓库使用经过脱敏的 vendor 快照，并保留本 fork 已公开的历史；原始上游提交号仅用作溯源元数据。后续同步通过 `scripts/sync_upstream.py` 生成下一个脱敏 vendor 提交，再正常三方合并。详见 [UPSTREAM_SYNC.md](UPSTREAM_SYNC.md)。

构建发布前必须先通过完整待发布 Git 历史的 Gitleaks 扫描、Python 回归测试、Node 页面测试和编译检查。PR 只检查、不发布镜像。

## 本次审计范围

- 当前受版本控制文件及本次将推送的完整提交历史。
- SSH 私钥结构、Google access/refresh token 格式、GitHub PAT，以及部署地址/本地部署路径等额外检查。
- 在生产主机内将待发布 Git blob 与真实服务密码、Google token、账号密码材料、用户邮箱和 API Key 摘要逐值比较。真实值不离开生产主机，报告只含类型和计数；未发现这些用户运行数据进入待发布内容。
- 公共默认 project ID 与本地数据重合的匹配，已核验为上游源码 `DEFAULT_PROJECT_ID` 的共享回退常量，不是用户私有项目标识。
- 删除不必要的原始响应示例；镜像排除散落的凭据 JSON、数据库、环境文件、私钥、备份与归档。

审计不通过时不得跳过检查直接发布；详细的部署环境比较结果保存在运维侧，不提交到本仓库。

## 不允许 OAuth 客户端常量进入发布历史

`ANTIGRAVITY_OAUTH_CLIENT_ID` 与 `ANTIGRAVITY_OAUTH_CLIENT_SECRET` 必须由部署环境成对提供，不再内置上游默认值。原有凭据中的客户端配置仍随凭据私有保存，既有凭据调用不因此需要重新授权。生成新的 OAuth 授权链接需要先配置这两个环境变量。

Gitleaks 保留完整默认规则，并额外检测 Google OAuth client ID 和 `GOCSPX-` client secret。`.gitleaks.toml` 只允许两个**完整值精确匹配**：文档密码占位符与合成测试密码。没有 OAuth 常量例外，没有 Git 历史指纹例外，也不忽略整个文件、目录或所有 token 字符串。

## 本地复查

使用 Gitleaks 8.30.1，在仓库根目录执行：

```sh
gitleaks git . --log-opts=HEAD --redact=100 --no-banner
python -m pytest tests
node --test tests/test_member_quota_ui.mjs
python -m compileall -q src scripts web.py config.py
git diff --check
```

CI 下载固定版本扫描器并校验 SHA256。扫描日志强制脱敏，真实生产凭据不得用作 CI 检测样本。发布时只推送经过审计的 `main`、vendor 分支及指定版本标签；不得将保留的本地原始合并分支或全部本地 refs 一并推送。
