# 上游同步：只合并脱敏 vendor 历史

## 公开历史边界

原始上游历史含内置 Google OAuth client ID / secret，会触发 GitHub push protection。**不能直接 `git merge upstream/master`、合并原始上游 SHA、推送原始上游分支，或通过忽略规则/绕过 push protection 发布这些历史。** 即使最终源码删掉常量，只要 raw commit 是祖先，敏感历史仍会被一并上传。

公开 `main` 保留现有公开提交 `f866725` 的祖先关系，但不引入原始上游 parents。原始接祖先/合并实验只保存在本地 `migration/agy2api-modular` 等 local-only 分支，不能 `git push --all` / `--mirror`。原上游基线 `934c8ce8b3648b2530ab8a36c409d70d699ab680` 与本次版本 `6cc45620f1ecbe12bda7792b2daf8940d8a4ffde` 仅作来源元数据，不是公开提交的 parent。

公开同步链为：

```text
vendor/agy2api: V1 (无 parent 的脱敏 root) --- V2 --- V3
                  \                          \      \
main: f866725 ----- M1 ----------------------- M2 --- M3
```

每个 V 的 tree 来自对应 raw upstream tree，经固定版本 sanitizer 处理；commit message 记录完整 `Upstream-Commit:` 和 `Vendor-Sanitizer:`。V 的唯一 parent 是上一个 V，初次 V 没有 parent。M1 把 vendor baseline 接入公开 fork；后续 M2/M3 才针对脱敏 vendor 分支执行正常三方 merge。不能把本地包含原始历史的合并提交拿来作为公开 M1。

## 生成脱敏快照

`scripts/sync_upstream.py` 仅依赖 Python 标准库和 Git，不联网，不改源码文件、工作树或真实 index，不自动 merge/push。可以在有未提交 fork 改动的工作树中运行，因为整个 tree 由 Git blob 和临时 index 构建；真正 merge 前仍须清理工作树。若 vendor 分支在任一 worktree 中被检出，脚本拒绝更新，避免 HEAD 与工作树失配。

```sh
# remote 不存在时，先添加：
# git remote add upstream https://github.com/su-kaka/agy2api.git
git fetch upstream
# 审阅/固定本次来源，不依赖移动标签：
git rev-parse upstream/master
python3 scripts/sync_upstream.py \
  --upstream-ref upstream/master --vendor-branch vendor/agy2api --dry-run
# 审阅 dry-run 后以固定 SHA 再运行（替换占位符）：
python3 scripts/sync_upstream.py \
  --upstream-ref <reviewed-raw-upstream-sha> --vendor-branch vendor/agy2api
```

脚本输出 upstream hash、sanitizer 版本、文件计数、tree hash，以及真实创建/复用的完整 `Vendor-Commit:`，不输出常量值。dry-run 使用临时 object store，不更新 ref、真实 index、工作树或持久对象；不会声称已创建 commit。实际运行使用仓库现有 Git 作者/提交者配置，可由调用方标准 `GIT_AUTHOR_*` / `GIT_COMMITTER_*` 环境覆盖；脚本不自行设身份。

当前 `agy2api-v1` 规则：

- 仅将 `src/utils.py` 顶层 `ANTIGRAVITY_CLIENT_ID/SECRET` 字符串赋值替换为 `os.getenv("ANTIGRAVITY_OAUTH_CLIENT_ID", "")` / `os.getenv("ANTIGRAVITY_OAUTH_CLIENT_SECRET", "")`。默认空，不保留上游常量 fallback；缺少 `import os` 时在赋值前加入。支持原有 `import os`；不读取本机环境值写入快照。
- 排除上游 `.github/workflows/` 全部内容，避免发布上游镜像、自动发版或覆盖 fork 工作流；fork 自己的工作流由公开分支独立维护。
- 排除原始 `Response_example.txt` / `Response_exmaple.txt` 响应日志及同类 response example 文本；不把日志当实现文件。
- 排除 `creds`、`data`、`privatekeys` / `private_keys`、`.ssh`、虚拟环境、缓存、`node_modules` 目录，以及 `.env` / `.env.*`（保留 `.env.example`）、常见私钥、数据库和日志文件。其余文件及 executable/symlink 模式保留；只读取 Git blob，不跟随仓库 symlink 读取外部文件。未知 submodule 拒绝，需人工审阅。
- 拒绝剩余 Google 原生 OAuth ID / secret 形状及 PEM 私钥标记；已知赋值结构变化、重复/缺失或路径变化时拒绝并要求更新 sanitizer。**不为未知 token 自动加白名单。该窄检查不是通用 scanner。**
- 相同脱敏 tree 不重复提交；若 raw 版本只改变被排除文件，也复用现有 vendor commit。此次来源 SHA 仍会输出，但不会改写旧 commit 元数据。
- 更新分支使用 compare-and-swap；已有 vendor 历史必须是带 sanitizer 来源记录的线性链，拒绝 raw upstream / merge 历史作 parent。该结构校验不替代外部历史 secret scan。

新增上游敏感文件或格式变化时，先审阅，修订 sanitizer、递增版本并补测试，不通过 ignore/allowlist 让真实 token 过检。不要把原始常量复制到测试 fixture；需要原生形状的假值时在运行时由片段拼接。

## Scanner 与首次公开合并

创建 vendor ref 不代表已获准发布。使用已安装的外部 scanner（例如 Gitleaks；按本机 `gitleaks git --help` 核对版本参数）扫描**全部将发布的历史**，而不只是 staged diff：

```sh
gitleaks git --redact --config .gitleaks.toml --log-opts='vendor/agy2api' .
# 公开 main/升级分支完成后，再扫描 publishable HEAD 的全部祖先：
gitleaks git --redact --config .gitleaks.toml --log-opts='HEAD' .
```

严格规则必须覆盖 Google OAuth client ID 与 secret，不能用全路径白名单或 baseline 忽略掩盖真实常量。不要把含敏感值的未 redacted scanner 日志写入仓库。普通宽泛 `--all` scan 会扫到刻意保留的 raw/local-only refs；这不能用来为发布分支豁免，而应分别明确检查真正发布的 refs。

首次 V1 与公开 fork 没有共同祖先，在干净、以 `origin/main`（公开 `f866725`）为祖先的集成分支处理一次：

```sh
git switch -c sync/agy2api-initial origin/main
git merge --no-commit --no-ff --allow-unrelated-histories <vendor-commit-id>
# 逐个审阅冲突，保留 fork 隔离能力，测试并 scanner 检查后完成 merge commit。
```

这会产生 add/add 冲突；不可整体接受 ours 来假装升级，也不可从含 raw 上游祖先的本地 migration 分支直接继续发布。若通过 plumbing 复用已审阅的集成 tree 建立首次公开提交，parents 只能是原公开 main 和 V1，不能包括 raw/migration commit。

## 后续常规三方合并

```sh
git fetch upstream
python3 scripts/sync_upstream.py --upstream-ref <reviewed-raw-upstream-sha> --dry-run
python3 scripts/sync_upstream.py --upstream-ref <reviewed-raw-upstream-sha>
# 先扫描生成的 vendor 历史，再在干净升级分支合并脚本输出的固定 commit ID：
git switch -c sync/agy2api-next main
git merge --no-commit --no-ff <vendor-commit-id>
```

后续不用 `--allow-unrelated-histories`：前一次 V 已是共同祖先。脚本不会替用户执行上述 merge，也不会自动提交冲突解决或 push。禁止把命令最后的 vendor SHA 换成 raw upstream SHA。

逐项审阅上游转换器、数据库、路由、多用户 hooks、测试、迁移、个人页、部署配置与 fork 工作流。`.env.example` / Compose 不能恢复为原版镜像、隐式通用密码或不可达代理；OAuth 只能使用成对环境配置，不能恢复常量 fallback。更新 [UPSTREAM.md](../UPSTREAM.md) 和 `fork-version.json` 的来源 SHA 时，SHA 只是 metadata；fork 更新源仍是本 fork `main/fork-version.json`。

## 必须保留的核心 hooks

| 接入点 | 职责 |
|---|---|
| `web.py` → `src.multiuser.install(app)` / startup preflight | 账户、个人页面/OAuth/额度、fork 版本路由及身份上下文；拒绝多进程、非空 `POSTGRESQL_URI` 和旧 filename schema 误挂 |
| `src/utils.py` 鉴权 | 个人 Key / 可选管理员 API 密码；Bearer、Gemini header/query 统一解析，未知身份拒绝；面板管理员上下文不能由个人会话取得 |
| `src/multiuser/oauth.py` | `ANTIGRAVITY_OAUTH_CLIENT_ID/SECRET` 成对环境配置；缺失即明确失败，不 fallback 内置常量 |
| `src/credential_manager.py` | 用户归属集合内选择、刷新前后校验、单 email 锁、个人日志隔离；random/sticky/cache 均按用户命名空间，禁止回退公共池 |
| `src/storage/database.py` 写入 guard | 防止刷新与删除竞态重新插入已删除凭证；不能仅靠 selector 隔离 |

新增凭证直读、预取、刷新、后台任务及重试路径时，重新审计底层隔离。HTTP 路径中间件不是底层隔离替代品。

## 验收与祖先核验

```sh
python -m pytest tests/test_sync_upstream.py
python -m pytest tests
node --test tests/test_member_quota_ui.mjs
python -m compileall -q src scripts web.py config.py
git diff --check
docker compose config --quiet
docker compose build
# 首次 vendor root 的这一行只有一个 ID；后续每行最多两个 ID：
git rev-list --parents vendor/agy2api
# 公开分支必须保留原公开 main：
git merge-base --is-ancestor f866725 HEAD
# vendor 已接入（必须成功）：
git merge-base --is-ancestor vendor/agy2api HEAD
# raw upstream 不应成为祖先（必须返回非零）：
git merge-base --is-ancestor 6cc45620f1ecbe12bda7792b2daf8940d8a4ffde HEAD
```

还须通过上面的外部 scanner；OAuth 常量即使只存在祖先也不能发布。9 项同步脚本测试使用真实临时 Git 仓库，覆盖 root/后续 parent、过滤/环境化、无变化幂等、无分支首次 dry-run、真实 index 和脏工作树保持、未知 OAuth fail-closed、赋值结构变化、raw parent 拒绝、已检出 vendor 拒绝。

应用验收继续覆盖邀请/会话/Key、A/B/管理员强隔离、各 API 及流式重试、OAuth 绑定/重放/删除竞态、个人额度与日志隐私、离线迁移幂等/碰撞/回滚及启动预检。测试成功不等于真实账号调用已验证；实际部署另做隔离 smoke test。本流程不操作 VPS。
