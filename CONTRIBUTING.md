# 参与开发

本项目是 [su-kaka/gcli2api](https://github.com/su-kaka/gcli2api) 的邀请制多用户二开版本，沿用 [CNC-1.0](LICENSE)。贡献时保留上游归属与版权信息。

## 开发环境

```sh
python -m venv .venv
# Linux/macOS
. .venv/bin/activate
python -m pip install -r requirements.txt
```

将部署配置作为环境变量传入，或使用 Docker Compose 的 `env_file`；示例见 `.env.example`。不要把实际配置写到源码和测试中。

## 验证

```sh
python -m unittest discover -s tests -p 'test_multiuser*.py' -v
python -m compileall -q src web.py config.py
```

现有 member 测试使用临时 SQLite、内存凭证适配器和模拟 Google 响应，不需要真实 Google 账号、数据库或网络。新的测试也应遵守此约定。

## 核心隔离规则

- 由服务端根据会话或 API Key 决定用户身份，不接受客户端指定 owner。
- 任何选择、刷新、预热、流式响应和失败重试都只能使用该用户的 active 凭证。
- 先保留凭证归属，再写凭证及状态，最后激活；失败保留 pending 归属。
- OAuth state 绑定用户、有有效期并原子单次消费；不调用旧管理员 OAuth 保存函数。
- 不记录授权码、完整回调 URL、Google token、用户密码或完整 API Key。

## 提交范围

只提交源码、合成测试及文档。凭证目录、用户数据库、环境配置、密钥、日志、备份和本地部署脚本不进入 Git 或 Docker 构建上下文。

Pull Request 请说明改动、执行的验证、对旧管理员模式和用户隔离的影响。上游通用能力的修复应注明是否适合回馈上游项目。
