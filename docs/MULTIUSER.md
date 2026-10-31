# 多用户扩展与迁移

本 fork 运行上游 agy2api 的 Antigravity 核心；多用户扩展位于 `src/multiuser/`。它不是可单独部署的 HTTP 网关，而是通过少量 hooks 在同一应用内限定凭证归属。默认 Compose 是单容器、单副本、单进程，从本仓库构建。启动预检要求 `WORKERS=1`，未设置即为 `1`，其他值拒启；`WEB_CONCURRENCY` 同样必须未设或为 `1`，不是只限制容器数量。误挂旧 filename schema 的数据库也会拒启，必须先离线迁移。

## 账户和 API 兼容

- 账户、邀请、会话、API Key 散列继续保存在 `multiuser.sqlite3`，旧账户表结构保留。
- `/users` 个人页及已有 `/users` API 路径/字段继续使用；旧客户端不需要因为目录重构换 API。
- sessionStorage 的 `gcli2api.user.token` 名称刻意不改，个人页标题改为 agy2api 不应使旧会话丢失。
- 仅 `mode=antigravity` 可用；Gemini CLI 旧数据只作归档。
- `filename` 是兼容字段名，值现在是规范化 Google email，或迁移保留的稳定 `@migration.invalid` alias，不再是旧文件名。迁移时必须将凭证归属同步重映射，旧缓存的文件名不能继续用来定位新凭证。

管理员使用面板认证生成邀请。个人会话仅用于账户管理；调用聊天接口使用个人 API Key，不使用浏览器会话 token。完整 Key 只在创建时返回，可单独撤销。

## 数据与隔离

默认目录布局：

```text
creds/                         # 新部署目录，或离线迁移的目标
  credentials.db               # 上游 email-keyed 凭证/配置 SQL 库
  multiuser.sqlite3            # 账户、会话、Key 与所有权
  legacy_archive/              # 仅迁移产生的旧数据归档，不进入运行池
  .legacy-migration.json       # 迁移清单（仅迁移生成）
```

Compose 固定容器路径 `CREDENTIALS_DIR=/app/creds` 和 `MULTIUSER_DB_PATH=/app/creds/multiuser.sqlite3`，统一由 `./creds` 持久化；若修改路径，须同步修改挂载。本迁移版只支持 SQLite；非空 `POSTGRESQL_URI` 会被启动预检拒绝，不能因上游支持 PostgreSQL 就直接启用。多用户库仍为 SQLite；即使 Redis 可用也仍要求单进程。

请求从默认未认证身份开始，API Key 解析后绑定账户。选择器、刷新、重试、流式任务及会话凭证缓存均受此身份限制；个人池失效不能选管理员池。`MULTIUSER_ALLOW_LEGACY_API_PASSWORD=true` 只允许管理员 API 密码调用管理员凭证；设为 `false` 关闭这条兼容入口，不影响面板密码。

email 必须来自 Google 对刷新后 token 的身份验证，而非上传者自行填写。重复身份不得跨账户重新认领。个人额度查询仅查指定的已拥有凭证，不遍历公共池；删除或待保存记录不能被重新解释为管理员可用凭证。

## OAuth 与个人页

新 OAuth 授权必须成对设置 `ANTIGRAVITY_OAUTH_CLIENT_ID` / `ANTIGRAVITY_OAUTH_CLIENT_SECRET`，无内置客户端默认值，不再回退上游常量。缺少配置或只设置一项时，个人 OAuth 返回通用 `502` 失败，不返回原始异常、授权码或凭据。已上传或迁移的凭据使用各自保存的 `client_id` / `client_secret` 刷新和调用，无需为已有凭据额外设置新授权客户端。

成员在个人页生成链接、Google 授权后复制 `http://localhost:11451` 的完整回调地址，再粘贴完成授权；不启动本地监听器。OAuth 状态绑定当前用户、有时限、一次性消费。上传 JSON 也需要联网验证身份，并非离线导入捷径。

## 离线迁移与回滚

上游凭证存储与标识均已变化，**不能挂载旧目录原地启动新版，也不能只搬凭证而丢掉所有权库**。迁移脚本只支持旧本地 SQLite；外部 PostgreSQL/MongoDB 旧数据须另行导出与适配，不能假定脚本已经覆盖。

### 执行顺序

1. 保留旧应用镜像/代码、环境配置、完整凭证目录与用户库备份。
2. 停止旧应用和所有写库/后台任务。即使脚本会核对源文件，也不支持在线迁移。
3. 指定源和新的目标目录；源不能等于目标，目标须不存在或为空，父目录须存在。

```sh
python3 scripts/migrate_legacy.py --help
python3 scripts/migrate_legacy.py --source ../legacy-creds --target ./creds --dry-run
python3 scripts/migrate_legacy.py --source ../legacy-creds --target ./creds
```

以上参数已按脚本 `--help` 核对。`--dry-run` 做完整检查但不发布目标。可选参数：

- `--accounts-db PATH`：旧账户库不在默认 `multiuser.sqlite3` 路径时显式指定。
- `--credentials-db PATH`：旧凭证库不在默认 `credentials.db` 路径时显式指定。
- `--email-map /private/map.json`：可信私有 JSON 对象，将旧凭证文件名映射到 email，仅用于补齐缺失身份，不覆盖已有 email；不要提交该文件。dry-run 和正式迁移使用同一份映射。
- `--preserve-collisions`：显式保留重复 Google email 的原始独立记录。组内每条旧 filename 都映射到自己的稳定 `legacy-…@migration.invalid` alias，不合并 token、状态或归属。用于同账号不同 token 确实分别归属成员/管理员的部署，dry-run 与正式命令须一致。默认仍严格拒绝跨 owner 的同 email 冲突；同 owner 重复默认保守合并。
- `--report PATH`：输出仅含计数的 JSON 报告，路径须在源/目标之外且不能覆盖已有文件。

`--preserve-collisions` **不豁免同 refresh token 跨 owner 冲突**。alias 只分离存储身份，不转移凭证归属；真实 email 与 alias 的私有映射保存在 `legacy_archive/identity-map.json`，仅报告 `aliased_collision_credentials` 计数，不输出身份。该选项也参与已迁移目标核验，改变选项不能覆盖已有结果。

脚本通过只读文件复制取得 DB/WAL，再在临时目录使用 SQLite 恢复一致性快照，不直接打开修改旧源库；发布前再次检查源文件，最后单次目录重命名发布。完全相同的重复迁移会核验并无操作，不会覆盖发生变化的非空目标。

### 迁移后核对

- 核对用户、邀请、会话、API Key 记录数与原账户可用性；不需要重新生成所有 Key。
- 核对 Antigravity 凭证迁移数及归属映射数；`credential_owners.filename` 转为 email 或稳定 alias。启用 `--preserve-collisions` 时核对 alias 计数和私有映射，确保每条原记录的独立归属保持。
- 核对缺失 email、孤立成员凭证、重复凭证及归属冲突的报告计数；不能把未迁移数据默认为“管理员凭证”。冲突报错时先核对来源；仅确需保留重复 email 的原语义时使用上述显式选项，不绕过同 refresh token 的归属检查。
- 缺失 email 可由可信 `--email-map` 补齐，否则使用基于旧文件名的稳定 `legacy-…@migration.invalid` 本地标识；它不是 Google 的真实身份，不会触发联网查询。核对 `mapped_emails` / `synthetic_emails` 计数。孤立成员数据被隔离，待保存/已删除归属不能变成活跃记录。Gemini CLI 及旧库保存在 `legacy_archive/`，不加载到运行池。
- `.env` 中密码、OAuth 客户端和代理等运行配置单独核对，不依赖迁移脚本复制环境。`PROXY` 未使用时留空。
- 验证账户 A/B 的个人页、原 API Key、额度查询和流式调用互不可见，管理员 API 密码仅见管理员池。

完成后才执行 `docker compose up -d --build` 并切换入口。旧目录及其备份继续保留，不用新结果覆盖旧源。

### 回滚

停止新版，再使用旧镜像/旧代码、旧环境和保留的旧目录启动旧版；不要让旧程序打开新版 email 主键数据库。新版启用后的新增用户、Key、凭证或状态不会自动反向迁移。若需要保留这些新增数据，应单独导出核对，而不是覆盖任一目录。

## 验证入口

全部 Python 测试：`python -m pytest tests`；个人额度 UI：`node --test tests/test_member_quota_ui.mjs`。依赖、编译及 CI 命令见 [README](../README.md#开发验证)。上游 hooks 清单与完整升级验收见 [UPSTREAM_SYNC.md](UPSTREAM_SYNC.md)。
