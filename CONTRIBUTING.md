# 贡献指南

这是 `occcc-bot/gcli2api-multiuser` 维护的 **agy2api 多用户 fork**，不是上游官方发行版。上游归属与 CNC-1.0 许可证见 [UPSTREAM.md](UPSTREAM.md) 和 [LICENSE](LICENSE)。

## 开发与测试

需要 Python 3.12+、Node.js 22+。仓库不依赖不存在的 Makefile 或 `make test` 命令：

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt pytest pytest-asyncio
python -m pytest tests
node --test tests/test_member_quota_ui.mjs
python -m compileall -q src scripts web.py config.py
```

合成测试不应访问真实 Google 账号或运行环境数据库。新增 Python 测试放入 `tests/`，异步测试可使用 pytest-asyncio。个人页额度测试直接由 Node.js 执行，无需安装 npm 依赖。CI 与上面的三个验证命令保持一致，不忽略失败。

需要手工启动时，复制 `.env.example` 为 `.env`，配置自己的密码/OAuth 客户端，使用独立的新数据目录，再执行 `docker compose up -d --build`。不要将个人数据库、Google token、OAuth secret、部署地址、SSH 信息或备份放入补丁与日志。

## 修改边界

- `src/multiuser/`：账户、会话、Key、OAuth、额度、存储适配与隔离选择器。新增多用户功能优先放在此目录。
- `src/utils.py`：聊天 API 鉴权与身份上下文，保留上游面板鉴权与 OAuth 默认常量；`src/multiuser/oauth.py` 支持成对环境覆盖优先，未配置时回退上游常量。
- `src/credential_manager.py`：凭证选择/刷新 hooks，不得绕过所有权校验。
- `web.py`：统一调用 `install(app)` 安装个人页/API 与上下文中间件，启动预检拒绝 `WORKERS!=1` / `WEB_CONCURRENCY!=1`（未设默认 1）、非空 `POSTGRESQL_URI` 及误挂的旧 filename schema。
- `src/storage/database.py`：保留 `store_credential` guard decorator，防止面板刷新在成员删除后重新插入 token；上游更新不能移除此写入边界。
- `front/users.html`：兼容旧个人页 API、浏览器会话存储 key；界面品牌更新不能强制注销旧用户。
- `scripts/migrate_legacy.py`：只读旧目录、离线迁移、原子发布、保留归档；`--preserve-collisions` 是显式保留重复 email 的每文件 alias，不豁免同 refresh token 跨 owner 冲突。
- `fork-version.json` / `src/multiuser/version.py`：fork 独立 release 与 `/version/info`，更新源为本 fork `main/fork-version.json`，不要用上游版本号替换。

修改 API 或数据结构时同步更新 [README.md](README.md)、[docs/MULTIUSER.md](docs/MULTIUSER.md) 和迁移测试。字段 `filename` 现为 email 的兼容名，不能再解释成可读写的任意磁盘路径；不能仅按上传 JSON 中自报的 email 分配归属。

凭证不可用、归属数据库异常、未知身份、失效 Key 等情况必须拒绝或报错，不能回退到公共池。随机、sticky、cache 选择、重试、流式和子任务必须执行同样的隔离检查。

## 提交与上游同步

1. 从当前开发分支创建工作分支，描述变更及复现方式。
2. 为行为改动增加合成测试，完整执行上述验证命令。
3. 检查 `git diff --check` 和待提交文件；不要附带环境文件或运行数据。
4. 上游升级按 [docs/UPSTREAM_SYNC.md](docs/UPSTREAM_SYNC.md) 执行真实三方合并，不用覆盖复制或 `ours` 策略伪装升级。
5. 更新上游固定版本、文档和迁移说明后再提交。

提交问题时提供脱敏复现步骤、Python/Node/Docker 版本、预期与实际结果；不提交原始授权回调、用户库或完整凭证。贡献沿用本项目 CNC-1.0 许可证。
