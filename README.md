# agy2api-multiuser

Fork release：**0.2.0**（`multiuser-0.2.0`）。版本元数据见 `fork-version.json`；`/version/info` 的更新检查读取本 fork `main/fork-version.json`，不是上游版本号。

基于 **[su-kaka/agy2api](https://github.com/su-kaka/agy2api)** 的邀请制多用户二开；仓库地址沿用 `occcc-bot/gcli2api-multiuser`。上游原名 gcli2api，本版已合并上游 `6cc45620f1ecbe12bda7792b2daf8940d8a4ffde`，**仅支持 Antigravity，不再提供 Gemini CLI 后端**。

Google API 转换、重试、模型接口、凭证数据库和管理员面板来自上游。多用户扩展集中于 `src/multiuser/`，通过少量核心 hooks 接入，仍是**单应用、单容器、单进程（WORKERS=1）**，不是外置 HTTP 网关。归属与合并记录见 [UPSTREAM.md](UPSTREAM.md)，原上游用法见 [上游文档](https://github.com/su-kaka/agy2api#readme)。本项目不是上游官方发行版，沿用 [CNC-1.0](LICENSE)。

## 多用户功能

- 邀请注册、个人会话、多个可撤销的用户 API Key。
- 个人 OAuth、JSON 凭证上传/删除、按个人凭证查询模型额度。
- 请求、流式输出、重试和会话凭证缓存均限定在当前账户的凭证池；不可用时不回退到其他用户或管理员池。
- `MULTIUSER_ALLOW_LEGACY_API_PASSWORD=true` 时，管理员 API 密码只使用管理员池；设为 `false` 可关闭该入口。
- 保留旧账户 SQLite 表和 `/users` API 契约；兼容字段 `filename` 现在承载规范化 Google **email** 或迁移保留的稳定 alias ID，不是磁盘文件名。

没有公开注册、计费或用户级限流。服务器管理员仍能管理整台服务及其数据。详见 [多用户说明](docs/MULTIUSER.md)。

## 新部署：构建本 fork

```sh
git clone https://github.com/occcc-bot/gcli2api-multiuser.git
cd gcli2api-multiuser
cp .env.example .env
# 编辑 .env：分别设置 API_PASSWORD、PANEL_PASSWORD 和 OAuth 配置
docker compose config --quiet
docker compose up -d --build
```

Compose 从当前 fork 构建 `agy2api-multiuser:local`，**不能替换成 `ghcr.io/su-kaka/agy2api` 原版镜像，否则没有多用户功能**。服务名为 `agy2api`，默认端口 `7861`：

| 用途 | 地址 |
|---|---|
| 管理员面板 | `http://localhost:7861/` |
| 邀请注册与个人页 | `http://localhost:7861/users` |
| Antigravity OpenAI Base URL | `http://localhost:7861/antigravity/v1` |
| Gemini / Anthropic 兼容接口 | `/antigravity/v1/models/{model}:generateContent`、`/antigravity/v1/messages` |

反向代理需支持长连接与 SSE；端口可用 `PUBLISHED_PORT` 调整。默认 `PROXY=`，不依赖容器内不存在的本地代理。

### 可选：拉取 GHCR 镜像与自动发布

默认 Compose 仍从源码构建。首次发布成功且 GHCR 包可读后，也可使用本 fork 镜像（仅 `linux/amd64`）：

```sh
docker pull ghcr.io/occcc-bot/gcli2api-multiuser:latest
# 保存为 compose.ghcr.yml；保留原 Compose 的配置和持久化挂载
printf 'services:\n  agy2api:\n    image: ghcr.io/occcc-bot/gcli2api-multiuser:latest\n' > compose.ghcr.yml
docker compose -f docker-compose.yml -f compose.ghcr.yml up -d --no-build
```

`.github/workflows/publish.yml` 在推送 `main`、`v*` 标签或手动触发时，先扫描完整 Git 历史中的凭据，再用 Python 3.13 执行全部 Python 测试、Node.js UI 测试和编译检查，全部通过才从对应提交的干净 Git 树构建并推送 GHCR；不发布 PR，手动发布也仅允许 `main` 或 `v*` 标签。认证仅使用 GitHub 自动提供的 `GITHUB_TOKEN`，无需配置 PAT secret。

`main` 发布 `latest` 和 `sha-<短提交号>`；例如 `v0.2.0` 发布 `0.2.0`、`0.2` 和 SHA 标签，不更新 `latest`，避免旧版本标签覆盖主线镜像。需要固定版本时，将上面两处 `latest` 替换为已发布的版本或 SHA 标签。镜像来自本 fork 源码，不是从 VPS 运行容器导出；发布不会改变现有 VPS 部署。

### 持久化与配置

Compose 把本项目的**新目录** `./creds` 挂载到 `/app/creds`：

- `credentials.db`：上游 email-keyed Antigravity 凭证与配置。
- `multiuser.sqlite3`：账户、邀请、会话、API Key 散列及凭证归属。
- `MULTIUSER_DB_PATH=/app/creds/multiuser.sqlite3` 保证用户数据库随同持久化；自定义路径必须另行挂载。

本 Compose 是单容器、单副本、单 worker 方案。启动预检拒绝 `WORKERS!=1`（未设置时默认为 `1`），不能在一个容器里启动多个进程规避限制；误挂旧 filename schema 的凭证数据库也会拒绝启动。本迁移版仅支持 SQLite；虽然上游支持 PostgreSQL，当前启动预检会拒绝非空 `POSTGRESQL_URI`，直到具备对应的只读 schema 检查。Redis 可选但不放宽单进程限制，`WEB_CONCURRENCY` 也必须未设或为 `1`。迁移脚本不直接迁移远程数据库。

新 OAuth 授权必须在 `.env` 中成对设置 `ANTIGRAVITY_OAUTH_CLIENT_ID`、`ANTIGRAVITY_OAUTH_CLIENT_SECRET`；源码不包含 OAuth 客户端默认值，也不再回退上游常量。未配置或只配置一项时，个人 OAuth 返回通用 `502` 失败，不暴露原始错误或凭据。**已上传或迁移的凭据仍使用各自保存的 `client_id` / `client_secret` 刷新和调用，不受此配置缺失影响；只有新授权需要配置。** 不再配置 Gemini CLI 客户端。个人 OAuth 使用手动回调：

1. 管理员在 `/users` 使用面板密码生成邀请，成员通过邀请注册。
2. 成员登录后生成 Google 授权链接并授权。
3. 跳到 `http://localhost:11451` 后，复制浏览器地址栏的完整回调 URL；无需本地监听器。
4. 粘贴回个人页完成授权，创建自己的 API Key。

会话和 API Key 分离；OAuth 状态绑定用户、限时且只能消费一次。上传已有凭证时也会联网验证 Google 身份，不能仅凭 JSON 中自报的 email 认领凭证。

## 从旧 gcli2api-multiuser 迁移

**旧凭据必须离线迁移到新目录；不能直接将旧目录原地挂载给新版本。** 上游凭证结构已由文件名/旧表变为 email 主键，旧归属必须同步映射。只复制凭证而不保留用户数据库会丢失隔离关系。

1. 保留旧镜像/代码、`.env` 和完整旧数据备份，停止旧服务以及所有后台写入者。
2. 使用以下脚本检查并迁移到不存在或为空的目标目录，目标父目录须已存在。下面假设旧目录为 `../legacy-creds`，当前项目 `./creds` 是新目录；按实际路径替换。

```sh
python3 scripts/migrate_legacy.py --help
python3 scripts/migrate_legacy.py --source ../legacy-creds --target ./creds --dry-run
python3 scripts/migrate_legacy.py --source ../legacy-creds --target ./creds
```

脚本仅依赖 Python 标准库，不修改源目录；先恢复一致性快照，再原子发布目标目录，不覆盖已有不同内容。账户、邀请、会话、Key 散列保留；Antigravity 归属映射至 email。Gemini CLI 数据仅归档，不进入新运行池。缺失身份、孤立或冲突数据的处理与核验见 [迁移细节](docs/MULTIUSER.md#离线迁移与回滚)。

缺少 email 时，可用 `--email-map /private/map.json` 提供可信的旧文件名到 email 映射；未提供映射时脚本使用 `@migration.invalid` 的稳定合成标识，不伪造真实 Google 地址。映射文件不得提交。已有 email 不允许被映射覆盖。

默认严格拒绝跨 owner 的同 email 冲突。同一 Google 账号确实以不同 token 分别归属成员/管理员，且必须保留原语义时，可在 dry-run 与正式命令中都显式加 `--preserve-collisions`：重复 email 组中每条旧记录按原 filename 生成稳定 alias ID，token、状态及归属独立保留，不合并。核对 `aliased_collision_credentials` 计数和私有 `legacy_archive/identity-map.json`。**同 refresh token 跨 owner 仍一律拒绝**，此选项不豁免该冲突，也不能覆盖已有不同目标。

3. 单独迁移环境配置，检查计数报告和归属，再运行 `docker compose up -d --build`。自定义旧库路径可用 `--accounts-db` / `--credentials-db`；`--report` 指向源/目标之外的新 JSON 文件。
4. 验证原账户登录、个人凭证、旧 API Key、管理员池及跨账户隔离后再切换入口。

回滚时先停止新版，再以**旧镜像和保留的旧目录**启动旧服务；不要让旧程序读取新目录。新版启用后的新增数据不会自动反向同步，不能用旧备份覆盖新版目录。

## 开发验证

Python 3.12+、Node.js 22+：

```sh
python -m pip install -r requirements.txt pytest pytest-asyncio
python -m pytest tests
node --test tests/test_member_quota_ui.mjs
python -m compileall -q src scripts web.py config.py
```

测试使用临时数据与模拟接口，不需要真实 Google 凭证。CI 执行全部 `tests`、额度 UI 测试及编译检查，任一步失败都会阻止通过。上游升级步骤与必过测试清单见 [docs/UPSTREAM_SYNC.md](docs/UPSTREAM_SYNC.md)，贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

不要提交 `.env`、用户库、凭证、令牌、部署日志或备份。Git 与 Docker 忽略规则不替代对提交内容的检查。发布前审计范围、精确例外和本地复查命令见 [docs/RELEASE_SECURITY.md](docs/RELEASE_SECURITY.md)。
