# 上游归属与版本

本项目是 **[su-kaka/agy2api](https://github.com/su-kaka/agy2api)** 的多用户二次开发，仓库沿用 `occcc-bot/gcli2api-multiuser` 名称，不代表上游官方发行版。

- 原上游名称：`su-kaka/gcli2api`。
- 原二开源码基线：`934c8ce8b3648b2530ab8a36c409d70d699ab680`（仅来源元数据）。
- 本次采用的 agy2api 源码版本：`6cc45620f1ecbe12bda7792b2daf8940d8a4ffde`（仅来源元数据，不引入原始 Git parents）。
- 公开历史基线：原 `origin/main` 的 `f866725`；后续通过独立脱敏 `vendor/agy2api` 历史集成。
- Vendor sanitizer：`scripts/sync_upstream.py`，当前版本 `agy2api-v1`；每个快照 commit message 记录完整 `Upstream-Commit:` 和 `Vendor-Sanitizer:`。
- 上游作者及贡献者：su-kaka、gcli2api / agy2api contributors。
- 二开维护账号：`occcc-bot`。
- Fork release：`0.2.0`，由根目录 `fork-version.json` 记录；`/version/info` 由 `src/multiuser/version.py` 提供，更新源为本 fork 的 `main/fork-version.json`，不是上游更新源。
- 许可证：沿用上游 Cooperative Non-Commercial License（CNC-1.0），全文见 [LICENSE](LICENSE)。

Google API 转换、Antigravity 请求与重试、模型接口、凭证数据库、管理员面板及前端构建产物来自上游，保留原版权和署名。新增邀请制账户、会话、个人 API Key、凭证归属、用户隔离、个人 OAuth、个人额度页和离线迁移脚本。

## 历史与同步边界

最初公开仓库从脱敏快照发布，未导入运行数据或部署历史。原始上游历史含 Google OAuth 内置 client ID / secret，不能上传。此前接入原始祖先并真实 merge 的实验只保存在本地 `migration/agy2api-modular` 等 local-only 分支，不属于公开 main，也不能以最终 tree 已脱敏为理由推送这些 parents。

公开集成保留原公开 main 为祖先，并将脱敏 vendor baseline 作为另一 parent。首次 vendor snapshot 是**无 parent 的 root commit**，仅采用当前 upstream tree，经环境化/过滤后保存；原始上游 SHA 只写入 message。后续快照的唯一 parent 是上一个 vendor commit，由此建立可持续正常三方 merge 的共同祖先，但不引入 raw upstream 历史。

必须使用 `scripts/sync_upstream.py` 生成快照、通过外部历史 secret scanner 后再合并脚本输出的 vendor commit。**普通 `git merge upstream/master` 或 raw SHA 会再次引入被拦历史，禁止用于公开同步；脚本不自动 merge 或 push。** 完整首次/后续流程及祖先核验见 [docs/UPSTREAM_SYNC.md](docs/UPSTREAM_SYNC.md)。不要 `git push --all` / `--mirror` 发布本地 raw/migration refs。

Sanitizer 将 `src/utils.py` 两个 OAuth 常量替换为读取 `ANTIGRAVITY_OAUTH_CLIENT_ID` / `ANTIGRAVITY_OAUTH_CLIENT_SECRET` 环境变量，默认空，不保留硬编码 fallback；剔除上游 `.github/workflows/`、原始响应 example 日志及凭据/运行数据。其余上游实现保留。窄模式检查不替代外部 scanner，不为未知 token 自动加白名单，也不绕过 GitHub push protection。

## 应用维护边界

上游核心为基础，多用户实现集中在 `src/multiuser/`，核心仅保留鉴权、凭证选择/刷新、数据库写入 guard 与应用安装/启动预检 hooks。仍为单应用、单容器、单进程；`WORKERS!=1`、`WEB_CONCURRENCY!=1`、非空 `POSTGRESQL_URI` 或误挂旧 filename schema 时拒启。OAuth 需要成对环境配置，未配置时不能恢复上游内置常量；不能在同步时用上游默认鉴权/随机凭证池覆盖隔离边界。上游原版镜像不包含多用户扩展，因此 Compose 必须构建本 fork。

本版仅保留 Antigravity。旧账号库表结构与 `/users` 接口继续兼容，但归属表及兼容字段 `filename` 的凭证标识从旧文件名切换为 email 或迁移用稳定 alias。旧凭证必须通过 `scripts/migrate_legacy.py` 离线导入新目录；不能原地启动。运行数据和部署环境不属于本仓库历史。
