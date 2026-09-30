# 运行与恢复手册

## 本地启动

Python 3.11+，`uv sync --locked`。复制配置示例，设置模型名和环境密钥，填写邀请 UID/关键词；转写默认关闭，启用时需要独立兼容接口和密钥。

```sh
cp config.example.toml config.toml
uv run bili-comment-bot config-check
uv run bili-comment-bot login
uv run bili-comment-bot run --once
uv run bili-comment-bot run
```

扫码使用 `data/login.png`，凭据保存在私密目录。`--once` 是有限演练：一次采集/搜索，分别一个批次的事件、动作和候选/流程处理。它读取真实平台、付费模型，并维护 Cookie；默认只模拟评论/私信/点赞。收集失败仍会执行其他独立渠道，最后以非零退出报告失败。未到期 AI 重试不强行提前执行。

常驻各采集渠道一次一个任务；模型并发、批次、平台请求限频都有上限。搜索无邀请 UID 时不运行。SIGINT/SIGTERM 停止新工作，等待 `runtime.shutdown_timeout` 后取消剩余任务。在途 POST 取消记 unknown，不自动再发。停止后才能登录、更新凭据或进行状态修改；实例锁禁止同时使用一个目录。

真实发布需同时 `publishing.dry_run=false` 和 `publish_enabled=true`，重启后使用 live 命名空间。sim 状态不转换成 live 回执。同一目录绑定同一账号，不同账号使用独立目录。本轮没有执行真实发布验收。

## 登录异常

失效/验证码、账号错配和不可恢复续期会立即阻止新的平台请求，保存状态并受控退出。先停止服务，再按 CLI 提示扫码；验证码由用户在 App 处理，不绕过。仅 refresh 或 confirm 已持久保存为开始但没有确定结果时，需要重新扫码；confirm_pending 可按已保存的新 Cookie/旧 token 恢复确认。

## 状态与恢复

```sh
uv run bili-comment-bot status --namespace sim
uv run bili-comment-bot actions --namespace live --uncertain --limit 20
uv run bili-comment-bot cancel-action --namespace live --action-id '动作 ID'
uv run bili-comment-bot verify-action --namespace live --action-id '评论或私信动作 ID' --remote-id 真实数字回执 --note '在对应账号核实回执与内容'
uv run bili-comment-bot verify-action --namespace live --action-id 'discovery:视频aid:like' --account-uid bot数字UID --aid 视频数字aid --liked --note '已在对应账号核实该视频已点赞'
```

数字参数替换成实际数字。修改/列举命令要求显式 namespace，并持同一实例锁。列举最多 100 条，仅显示动作 ID、类型、状态和回执，不显示正文或凭据。cancel 只允许 pending，不能取消在途/unknown/已发送。verify 只核实 unknown 为成功，无 HTTP、无强制重发、无释放 unknown 额度捷径。点赞的目标状态核实不证明原 POST 成功，审计记录会保留这一区别。

unknown 永久冻结，不因一小时过去释放；评论/私信的真实数字回执或绑定账号/aid/liked 证明与非空说明缺一不可。核实后重启服务，成功依赖链可继续。明确失败/取消的发现流程暂停，禁止自动越过该步骤；用动作列表检查，不能伪造核实记录。已有成功推荐按 aid 去重，不周期性反复 @ 同一视频。

## 日志、状态与备份

标准输出是字段白名单 JSON：任务类型、结果、模式、耗时、退避和脱敏错误类别。HTTP 库不输出请求 URL。`data/status-sim.json` / `status-live.json` 为 0600 聚合快照，记录 alive、ready、needs_login、采集成功时间、积压、重试、unknown、模型/转写调用和缓存指标；不含用户、消息、Cookie 或签名 URL。读取者检查快照是否超时，不能只把 PID 存活当业务正常。

在停止服务后备份整个私密 data 目录；恢复时同时恢复 auth 和数据库，保持账号一致。SQLite WAL 不要在运行中仅拷贝主文件。使用本地文件系统，不用网络共享锁，也不删除锁文件。状态库包含处理事件/批准内容，备份同样需要限制访问。

Docker/Compose 与 CI 将在下一阶段提供；本轮验证使用完全离线 HTTP fixture，不是实际账号或供应商验收。
