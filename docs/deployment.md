# Docker 部署

需要 Docker Engine 与 Compose v2。镜像固定 Python 3.11.14、uv 0.11.2，使用已有 `uv.lock` 的生产依赖，排除开发工具。默认 UID/GID 10001、只读根目录、持久卷、本地健康探针，不暴露端口。基础镜像标签可能被上游更新，不承诺逐字节相同镜像。

## 配置与构建

```sh
cp config.example.toml config.toml
cp .env.example .env
chmod 600 .env
# 编辑 config.toml：人格、模型接口、关键词、邀请用户数字 UID、额度
# 编辑 .env：BILI_BOT_AI_API_KEY、BILI_BOT_AI_MODEL；转写启用时填独立密钥
docker compose config --quiet
docker compose build
docker compose run --rm bot config-check
```

`.env` 由 Compose 注入，原生 CLI 不自动加载。配置只读挂载，密钥不写入 TOML 或镜像。Compose 强制 `/data` 保存认证、数据库/WAL、锁与状态；命名卷首次由镜像初始化为 10001 所有、0700。已有卷或绑定目录须由管理员设置正确所有权，禁止 chmod 777；使用本地文件系统。

`persona.name` 配置 bot 生成内容身份，不修改 B 站昵称。请手动设置账号显示名，召唤采集按实际账号 UID 核对。

## 首次扫码

```sh
docker compose stop bot
docker compose run --rm --name bili-bot-login bot login
```

显示等待扫码后，在另一终端复制二维码并用图片查看器打开：

```sh
docker cp bili-bot-login:/data/login.png ./login.png
```

用哔哩哔哩 App 扫码并确认，成功后删除宿主机 `login.png`；容器二维码退出时删除。不要公开二维码/凭据，扫码期间不能启动同一目录的 bot。

## 演练与常驻

```sh
docker compose run --rm bot run --once
docker compose up -d bot
docker compose logs --tail 100 bot
docker compose exec bot bili-comment-bot --config /app/config.toml status --namespace sim
docker compose stop bot
```

默认 `dry_run=true`、`publish_enabled=false`，评论/私信/点赞模拟；采集、模型和认证续期仍真实联网，模型可能计费。`--once` 处理一轮有限预算，失败/延后/新隔离记录非零退出，不保证排空历史。

完成演练和真实验收后，同时设置 `dry_run=false`、`publish_enabled=true`，重启进入 live。sim 不转换成 live 回执；修改其他配置也须重启。私信与评论分别每用户滚动一小时默认 5 条，白名单仅免额度。

## 健康、停止与恢复

探针只读配置及对应 sim/live 快照，不联网、不占锁。缺失/陈旧、首轮未完成、技术失败/重试、unknown/隔离、需扫码和停止均不健康。启动宽限 120 秒，较长首轮可能暂时不健康。健康不代表模型效果验收。

Compose `restart: "no"` 避免认证故障反复访问平台。失效/验证码先停止服务，在手机处理验证后重新 login，再由操作者启动。`init` 转发 SIGTERM，330 秒宽限覆盖最大 300 秒运行器等待；取消在途 POST 仍冻结 unknown，不自动重发。

```sh
docker compose stop bot
docker compose run --rm bot actions --namespace live --uncertain --limit 20
docker compose run --rm bot verify-action --namespace live --action-id '实际动作 ID' --remote-id 真实数字回执 --note '已核对对应账号与内容'
```

点赞核实和取消规则见 [运维手册](operations.md)。备份前停止服务，备份完整卷（认证、数据库/WAL及锁文件），恢复同一账号与正确所有权。`docker compose down -v` 会删除持久状态。备份含私密消息，需要限制读取。

## 验证

CI 构建镜像、验证 Compose，以 `--network none` 实际运行容器，检查非 root、持久卷权限/跨容器写入、开发工具排除、无凭据启动失败、打包评测与双命名空间健康。CI 不发布镜像、不部署、不调用真实模型或写 B 站。验证范围与真实验收清单见 [验证矩阵](acceptance-matrix.md)。

参考官方 [uv Docker 指南](https://docs.astral.sh/uv/guides/integration/docker/)、[Dockerfile 参考](https://docs.docker.com/reference/dockerfile/) 和 [Compose services](https://docs.docker.com/reference/compose-file/services/)。
