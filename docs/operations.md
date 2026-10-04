# 运行与恢复手册

## 本地启动

Python 3.11+，执行 `uv sync --locked`，然后启动：

```sh
uv run bili-comment-bot run
```

打开终端输出的完整控制台链接，在网页保存模型、人格和运行配置，点击扫码登录后启动。网站随项目启动，无配置时也可访问；所有配置均在网页编辑。运行中保存会安全停止并重启机器人，网页登录会先停止机器人。网站可持续用于处理认证或模型故障。

网页扫码凭据仍保存到私密数据目录。需要单轮 CLI 演练时先停止网页中的机器人，再执行 `uv run bili-comment-bot run --once`。该命令读取真实平台、模型并维护 Cookie，默认仅模拟平台写入。无网站运行使用 `run --headless`。配置路径和迁移规则见 [部署手册](deployment.md)。

常驻各采集渠道一次一个任务；模型并发、批次、平台请求限频都有上限。搜索无邀请 UID 时不运行。SIGINT/SIGTERM 停止新工作，等待 `runtime.shutdown_timeout` 后取消剩余任务。在途 POST 取消记 unknown，不自动再发。停止后才能登录、更新凭据或进行状态修改；实例锁禁止同时使用一个目录。

真实发布需在网页同时设置 `publishing.dry_run=false` 和 `publish_enabled=true`，保存并重启后使用 live 命名空间。sim 状态不转换成 live 回执。同一目录绑定同一账号，不同账号使用独立目录。本轮没有执行真实发布验收。

## 登录异常

失效/验证码、账号错配和不可恢复续期会立即阻止新的平台请求，保存状态并受控退出机器人进程，配置网站保持可用。在网站重新扫码，或先停止服务再按 CLI 提示扫码；验证码由用户在 App 处理，不绕过。仅 refresh 或 confirm 已持久保存为开始但没有确定结果时，需要重新扫码；confirm_pending 可按已保存的新 Cookie/旧 token 恢复确认。

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

Docker/Compose 构建、扫码查看、停止与卷权限见 [部署手册](deployment.md)，AI 评测见 [评测手册](evaluation.md)。离线 HTTP fixture 不代表实际账号或供应商验收。

## 任务结果与健康检查

任务结果分为 completed（完成）、skipped（正常跳过）、deferred（安排重试）、failed（技术失败）和 attention（需人工核查）。正常安全拒绝、超额度静默、低分候选和未到期任务不算技术失败。事件、发现和动作意外异常都有稳定身份的持久退避；只枚举到期 pending 动作，绝不把 unknown 退避成自动重发。

`run --once` 本轮遇到处理失败/延后/新隔离记录时返回非零，其他独立任务仍完成。之前已安排、尚未到期的重试不导致本轮报技术失败；状态仍显示其降级原因。成功完成清理重试；已完成/unknown/在途的状态保持原语义。

坏事件、动作和流程 payload 单独进入 quarantined 表并保留原始记录/脱敏审计，不无限扫描剩余任务。下一有限批次能处理后面的合法记录。隔离需要人工检查私密数据库或恢复完整的有效备份，命令不会自动丢弃记录、重置状态或绕过核实。

```sh
uv run bili-comment-bot status --namespace sim --check
```

普通 status 输出聚合信息；`--check` 仅健康时退出 0，缺失、陈旧、未完成首轮、降级、停机或需要扫码都非零。alive 表示进程生命周期；ready 表示初始化/认证就绪；business_health 用于业务判断：

| 值 | 含义 |
| --- | --- |
| starting | 启动检查/首轮检查未完成 |
| normal | 所有启用任务完成检查，没有技术失败/待重试/核查积压 |
| degraded | 渠道连续失败或有待重试任务；其他渠道仍可处理 |
| attention | 损坏记录被隔离或有 unknown 动作，需人工核查 |
| needs_login | 认证故障，需要人工操作；该原因在退出后仍可见 |
| stopped | 进程已经退出 |

快照记录各任务最近尝试/成功、连续失败和五种批次结果数量。成功恢复后清除对应失败；空批次不能证明模型恢复。没有配置关键词/邀请 UID 的搜索不属于待检查任务。状态新鲜度单独校验，更新快照不能掩盖业务降级。
