# 参与开发

使用 Python 3.11+ 与 uv；运行器支持 macOS/Linux 本地文件系统。

```sh
uv sync --locked
git config --local core.hooksPath .githooks
python3 scripts/check_public_tree.py
uv run --locked pytest -q
uv run --locked ruff check --no-cache .
uv run --locked ruff format --check --no-cache .
uv run --locked bili-comment-bot --config config.example.toml config-check
uv run --locked bili-comment-bot demo
uv run --locked bili-comment-bot --config config.example.toml evaluate
```

自动测试阻止真实网络访问。新增接口使用合成响应；不要录制真实账号的 Cookie、二维码、私信或未脱敏平台响应。测试需要覆盖会改变用户行为的边界，不要把预置模型答案当成真实模型评测。

修改平台接口时补充一手来源、协议契约与故障行为；修改提示、安全规则或评分时更新版本与评测。无法确定的发布结果必须暂停，不能通过重试或放宽门禁绕过。

公开提交只包含源码、合成测试、示例配置和产品文档。个人笔记放在被忽略的 `.local/`，实际配置使用 `config.toml` 与 `.env`。提交前运行公开文件检查，并检查 `git diff --cached`。该检查无法识别所有秘密；不得使用 `git add -f` 绕过私人文件的忽略规则。

Git 作者信息也会公开。可以为本仓库设置 GitHub 提供的 noreply 邮箱，或使用匿名项目身份：

```sh
git config --local user.name 'bili-comment-bot contributors'
git config --local user.email 'contributors@example.invalid'
```

提交贡献表示同意按本项目的 [MIT License](LICENSE) 提供贡献，并确认有权提交相应内容。第三方代码须保留原许可与归属信息。
