# gcli2api-multiuser

基于 [su-kaka/gcli2api](https://github.com/su-kaka/gcli2api) 二次开发的邀请制、多用户凭证隔离版本。

保留原项目的 Google Antigravity / Gemini CLI 请求转换、OpenAI / Gemini / Anthropic API 兼容和重试逻辑，增加个人账户、OAuth 获取凭证和独立 API Key。**不是在另一台 gcli2api 前再套一层代理，而是直接改造原应用。**

上游基线：[`934c8ce8b3648b2530ab8a36c409d70d699ab680`](https://github.com/su-kaka/gcli2api/commit/934c8ce8b3648b2530ab8a36c409d70d699ab680)。原版权及许可证保留，详见 [LICENSE](LICENSE) 和 [UPSTREAM.md](UPSTREAM.md)。

## 功能

- 管理员生成邀请链接，默认有效期 72 小时，一次邀请只能注册一个账户。
- 用户注册、登录、退出；会话与 API Key 分离。
- 个人页面生成 Google OAuth 授权链接，粘贴回调 URL 后自动获取 Antigravity 凭证。
- 支持上传已有 JSON 凭证、查看个人凭证列表和删除。
- 每个账户创建多个 API Key，可单独撤销；完整 Key 只在创建时返回。
- 每次调用只选择 Key 所属账户的凭证，流式、非流式、自动重试、预热任务均按用户隔离。
- 自己的凭证不可用时返回错误，**不回退到管理员或其他用户的凭证池**。
- 原管理员 API 密码可以继续使用，只选择原有的管理员凭证。

```text
用户的 API Key → 用户身份 → 用户自己的凭证池 → 原 gcli2api 请求逻辑 → Google
```

## Docker Compose 部署

```sh
git clone https://github.com/occcc-bot/gcli2api-multiuser.git
cd gcli2api-multiuser
cp .env.example .env
# 编辑 .env：设置管理员密码及 Google OAuth 客户端配置
docker compose up -d --build
```

默认端口为 `7861`：

- 原管理员页面：`http://localhost:7861/`
- 邀请注册与个人页面：`http://localhost:7861/users`
- Antigravity OpenAI Base URL：`http://localhost:7861/antigravity/v1`

如果有自己的反向代理，域名指向该端口即可，无需修改个人页面的 API 地址。

### OAuth 客户端配置

公开仓库**没有硬编码 Google OAuth 客户端 ID/Secret**。运行时通过环境变量提供：

| 变量 | 用途 |
|---|---|
| `ANTIGRAVITY_OAUTH_CLIENT_ID` | Antigravity OAuth 客户端 ID |
| `ANTIGRAVITY_OAUTH_CLIENT_SECRET` | 对应客户端 Secret |
| `GEMINICLI_OAUTH_CLIENT_ID` | 原 Gemini CLI OAuth 客户端 ID（使用该模式时配置） |
| `GEMINICLI_OAUTH_CLIENT_SECRET` | 对应客户端 Secret |

使用与目标 Google API 兼容的 OAuth 客户端配置；此版本沿用上游的 OAuth 协议、scopes 和回调方式。已有部署可从自己现有的配置中迁移这些值，不要提交 `.env`。

Antigravity 配置未提供时，个人 OAuth 初始化会失败；已有凭证的上传和 API Key 管理仍可使用。

### 获取个人凭证

1. 管理员在 `/users` 的“管理员：生成邀请链接”中输入面板密码并生成邀请。
2. 朋友通过邀请注册并登录。
3. 点击“生成 Google 授权链接”，用自己的 Google 账号完成授权。
4. Google 会跳转到 `http://localhost:11451`。本流程**不启动本地监听器**，页面可能显示无法连接；复制浏览器地址栏的完整 URL 即可。
5. 回到个人页面粘贴 URL，点击“完成授权并保存凭证”。
6. 创建自己的 API Key，填入兼容客户端。

OAuth 状态绑定发起用户，有效期 15 分钟，只能消费一次；重新生成链接会替换自己的旧链接，不影响其他用户。系统自动查询 Google 项目与订阅等级，凭证直接归属该成员。

### 数据持久化与迁移

Compose 将 `./creds` 挂载至 `/app/creds`，其中包含凭证和用户数据库。账户、邀请、会话、Key 散列与凭证归属默认保存为 `multiuser.sqlite3`，可用 `MULTIUSER_DB_PATH` 指定。

从原 gcli2api 迁移时，保留原来的凭证目录和配置即可：未关联成员的旧凭证归管理员；成员凭证使用 `mu_` 随机文件名，并在写入前登记归属。用户数据库和凭证目录应一起保留。缺失归属的成员文件不会被当成管理员凭证。

凭证后端保留上游接口；本项目的用户账户与归属使用 SQLite。多副本部署需要共享同一份用户数据库，本仓库提供的 Compose 是单副本方案。

管理员仍能管理服务器和原管理员面板；成员隔离不意味着服务器管理员无法读取服务器数据。

## 测试

使用 Python 3.12 或更新版本，安装项目依赖后执行：

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_multiuser*.py' -v
```

也可以在构建后的容器中执行合成测试：

```sh
docker compose build
docker compose run --rm --no-deps --entrypoint python \
  -e MULTIUSER_DB_PATH=/tmp/test-members.sqlite3 \
  gcli2api -m unittest discover -s tests -p 'test_multiuser*.py' -v
```

51 项合成测试覆盖邀请的一次性消费、账户/Key 鉴权、跨用户凭证隔离、并发/流式上下文、OAuth 回调校验、过期/重放、单次认领、失败保存及二开版本检查。测试使用临时数据库与模拟 Google 接口，不使用真实凭证。

## 公开源码与脱敏

- 不包含个人 Google 凭证、API Key、会话令牌、用户数据库或 SSH 密钥。
- 不包含线上域名、IP、主机用户名、部署日志和备份。
- `.env.example` 仅为示例；`.env`、数据库、凭证目录、密钥和日志均被 Git 与 Docker 构建上下文排除。
- 原上游源码中的 OAuth 客户端配置改为环境变量，本仓库历史从脱敏快照开始，不导入任何运行环境历史。
- 部署和测试请使用自己的配置；配置数据不放入仓库。

## 主要改动

| 文件 | 功能 |
|---|---|
| `src/multiuser.py` | 邀请、账户、会话、API Key、凭证归属和个人管理接口 |
| `src/member_oauth.py` | 用户隔离的 OAuth 链接与回调交换 |
| `src/utils.py` | API 鉴权解析用户身份、OAuth 环境变量 |
| `src/credential_manager.py` | 在选择/刷新凭证前限定用户归属 |
| `web.py` | 用户路由、页面和请求身份上下文 |
| `front/users.html` | 中文个人管理页面 |
| `tests/test_multiuser*.py` | 多用户与 OAuth 合成测试 |

暂无公开注册、计费或用户级限流。

## 归属与许可证

本项目由 `occcc-bot` 整理发布，是 **su-kaka/gcli2api 的二次开发版本**，不是上游官方发行版。沿用上游 **Cooperative Non-Commercial License (CNC-1.0)**，完整条款见 [LICENSE](LICENSE)。感谢上游作者和贡献者。
