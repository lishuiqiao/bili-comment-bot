# Docker 部署

需要 Docker Engine 与 Compose v2。默认非 root 用户、只读根目录及私密持久卷。配置网站在容器内监听 8765，Compose 仅将它映射到宿主机的 127.0.0.1。

## 启动与网页配置

```sh
docker compose up -d --build bot
docker compose logs --tail 30 bot
```

无需复制 `.env` 或 TOML。打开日志中的完整控制台链接（带 `#token=…`），填写模型服务、模型名称与密钥，保存后点击「扫码登录」。二维码直接显示在网页，用哔哩哔哩 App 扫码确认后点击「启动」。初次配置期间机器人处于停止状态，网站保持可用。

网站包括人格、模型、平台、额度、发现、发布、字幕、转写、本地模型、画面理解、调度、存储与安全十二个分组，覆盖 Settings 的所有字段。列表每行一项，数字 UID 必须为正整数。密钥留空保留原值；「清除密钥」在保存时生效。字段不合法会拒绝保存并定位错误；不同页面编辑同一版本时，后保存的页面必须重新加载。

运行中保存先等待旧进程安全退出，再持久化配置并启动新进程；sim/live 状态仍隔离。停止等待可能持续 `runtime.shutdown_timeout` 秒，期间不要关闭控制台进程。扫码时不允许保存配置。网站关闭时一并停止机器人；机器人故障则保留网站，等待人工处理，不自动重试登录。

## 配置存储与旧版迁移

- Docker：`/data/settings.web.json`，权限 0600，包含模型密钥。账号 Cookie 仍独立保存在 `/data/auth.json`。
- 原生 CLI：默认在 `--config` 指定的 TOML 旁保存同名 `.web.json`，例如 `config.web.json`。
- `BILI_BOT_WEB_CONFIG` 可指定固定的网页配置存储位置。它是启动参数，不受网页中的数据目录字段影响。
- 第一次保存前读取旧 TOML，再用原有环境变量覆盖；第一次保存后完整网页配置优先，旧环境变量不会悄悄覆盖网页输入。
- 数据目录字段可在网页修改，但不会自动搬迁旧账号或数据库；Docker 建议保持 `/data`。切换账号请使用独立数据目录。

已有部署在第一次启动新版本前，可临时恢复 Compose 中的 `env_file: .env` 和 `./config.toml:/app/config.toml:ro` 挂载，以导入旧设置。网页检查并保存后可移除这两项。务必继续使用原数据卷，以保留去重、账号绑定和发布状态。网页配置文件存在时即使旧 TOML 损坏也不影响加载；若网页配置本身损坏，控制台显示提示和默认值，需重新填写并保存。

Apple Silicon 本地推理需原生安装，参见 [本地模型](local-models.md)；Docker 部署继续使用 API 模式。

## 原生运行与远程访问

```sh
uv sync --locked
uv run bili-comment-bot run
# 可省略 run；自定义本机端口：
uv run bili-comment-bot run --web-port 8766
# 保留命令行演练与无网页运行：
uv run bili-comment-bot run --once
uv run bili-comment-bot run --headless
```

本地默认只监听 `127.0.0.1:8765`。端口和监听地址分别由 `--web-port`、`--web-host` 指定。每次启动生成新访问令牌，旧令牌失效；令牌只存于当前浏览器会话，网页读取后清除地址栏片段。不要公开日志中的完整链接。

在远程服务器部署时，使用 SSH 隧道访问，无需公开控制台端口：

```sh
ssh -L 8765:127.0.0.1:8765 your-server
```

随后在本机打开日志中的链接。控制台校验 Host、Origin 和 API 令牌，不提供 CORS，也不支持直接暴露到公网或任意反向代理域名。容器直接使用 `docker run` 时需要追加 `run --web-host 0.0.0.0` 并只映射宿主机回环端口。

## 健康、停止与备份

```sh
docker compose logs --tail 100 bot
docker compose exec bot bili-comment-bot --config /app/config.toml status --namespace sim
docker compose stop bot
```

健康探针检查机器人业务状态，而非网站能否访问。等待首次配置、未登录、手动停止、认证故障和陈旧状态均不健康，网站仍可打开。Compose 不因 unhealthy 自动重启。认证失败后在网站重新扫码并手动启动。手机要求验证时须先在 App 完成验证。

默认 `dry_run=true`、`publish_enabled=false`。真实发布需在网站同时关闭模拟、开启发布许可；修改后保存会安全重启。已发出的请求无法撤回，结果不确定的动作继续冻结，不自动重发。CLI 的 `--once` 只执行一轮有限预算；失败或延期非零退出。

备份前停止服务，保存完整持久卷（包括网页配置、账号凭据、数据库/WAL）。备份包含密钥与私信，限制读取。`docker compose down -v` 会删除全部持久状态。使用本地文件系统，已有卷须保持 UID/GID 10001 的所有权和 0700 目录权限。

需要 CLI 登录、认证维护或发布结果核实时，先在网站停止机器人；这些命令继续使用原有数据目录锁。详细恢复步骤见 [运维手册](operations.md)。

## 二维码生成失败

`QRLoginProtocolFault` 表示二维码字段无效，或展示地址未通过可信 HTTPS 校验。先更新 bili-comment-bot 并重建镜像，再在网站重新点击「扫码登录」；这类错误不表示需要反复扫码。展示地址兼容 `passport.bilibili.com` 和 `account.bilibili.com`，生成与轮询 API 仍使用原地址，详见 [接口核对记录](bilibili-interface-notes.md)。

```sh
docker compose stop bot
git pull --ff-only
docker compose build bot
```

仍失败时只提供项目版本与错误类型。不要公开二维码图片、完整二维码 URL、二维码 key、Cookie、实际配置或 API key。本轮自动验证只使用合成数据，真实登录由操作者手动验收。

## 验证

CI 构建镜像、验证 Compose，以 `--network none` 实际运行容器，检查非 root、持久卷权限/跨容器写入、开发工具排除、无凭据启动失败、打包评测与双命名空间健康。CI 不发布镜像、不部署、不调用真实模型或写 B 站。验证范围与真实验收清单见 [验证矩阵](acceptance-matrix.md)。

参考官方 [uv Docker 指南](https://docs.astral.sh/uv/guides/integration/docker/)、[Dockerfile 参考](https://docs.docker.com/reference/dockerfile/) 和 [Compose services](https://docs.docker.com/reference/compose-file/services/)。
