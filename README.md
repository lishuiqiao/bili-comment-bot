<div align="center">
  <img src="assets/banner.svg" alt="bili-comment-bot — Videos, with a little personality." width="100%">

  <h1>bili-comment-bot</h1>
  <p><strong>看懂视频，也接住你的日常。</strong></p>
  <p>一个有性格、会总结、能发现有趣视频的 B 站陪伴机器人。</p>

  <p>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-a78bfa?style=flat-square" alt="MIT License"></a>
    <img src="https://img.shields.io/badge/Python-3.11%2B-60a5fa?style=flat-square" alt="Python 3.11+">
    <img src="https://img.shields.io/badge/deploy-Docker-38bdf8?style=flat-square" alt="Docker deployment">
    <img src="https://img.shields.io/badge/AI-OpenAI_compatible-f9a8d4?style=flat-square" alt="OpenAI-compatible API">
  </p>

  <p>
    <a href="#快速开始">快速开始</a> ·
    <a href="#给机器人一点性格">人格配置</a> ·
    <a href="docs/README.md">文档中心</a> ·
    <a href="CONTRIBUTING.md">参与开发</a>
  </p>
</div>

---

## 能做什么

| 能力 | 行为 |
| --- | --- |
| 🎬 评论召唤 | 在视频评论区 `@机器人账号`，依据全部分 P 的字幕或可选音频转写生成总结，回复原楼层；无需关注。 |
| 💬 私信陪伴 | 关注机器人后发送私信，得到带有人格与情感的日常陪伴回复。 |
| 🔎 发现视频 | 按关键词搜索，结合视频内容、播放互动指标与热门评论样本评分，邀请配置的用户观看。 |
| 🎭 可调性格 | 配置名字、性格、温暖程度、幽默感与共情程度；总结、陪伴、邀请、鼓励和拒绝共用人格。 |
| 🔐 登录与续期 | 首次手机扫码，随后自动维护 Cookie；失效或需要验证时停止运行并提示重新扫码。 |
| 🧰 持续运行 | Docker / Compose、异步任务、有限并发、SQLite 去重、内容缓存、健康检查与人工恢复工具。 |

私信与评论召唤分别按用户计数，默认各为**滚动一小时 5 条**；可配置白名单豁免额度。输入、视频资料与输出均经过安全检查，评论只讨论对应视频，私信服务于日常陪伴。

> [!NOTE]
> 项目提供离线测试、模型评测入口和容器 CI。真实账号接口兼容性、模型效果及实际媒体质量仍需部署后验收，详情见 [验证与验收](docs/acceptance-matrix.md)。视频理解基于口述证据，尚未分析画面。

## 快速开始

需要 **Docker Engine + Compose v2**。克隆仓库后，在项目根目录操作。

**1 · 准备配置与模型**

```sh
cp config.example.toml config.toml
cp .env.example .env
chmod 600 .env config.toml
```

在 `.env` 填写 `BILI_BOT_AI_API_KEY` 与 `BILI_BOT_AI_MODEL`；使用其他兼容供应商时修改 `BILI_BOT_AI_BASE_URL`。在 `config.toml` 设置人格、搜索关键词、邀请用户数字 UID 与额度。完整示例见 [config.example.toml](config.example.toml)。

```sh
docker compose build
docker compose run --rm bot config-check
```

**2 · 扫码登录**

```sh
docker compose run --rm --name bili-bot-login bot login
```

出现等待扫码提示后，在另一终端复制二维码并打开图片：

```sh
docker cp bili-bot-login:/data/login.png ./login.png
```

用哔哩哔哩手机 App 扫码并确认。登录成功后删除宿主机的 `login.png`；凭据保存在私密持久卷中。

**3 · 演练并启动**

```sh
docker compose run --rm bot run --once
docker compose up -d bot
docker compose logs --tail 100 bot
```

> [!IMPORTANT]
> 默认 `dry_run=true`、`publish_enabled=false`，评论、私信和点赞均为模拟。采集、模型调用与 Cookie 续期仍真实联网，模型可能计费。完成验收后，将两个开关同时改为 `dry_run=false`、`publish_enabled=true`，再重启服务启用真实发布。

详细部署、停止、备份与认证故障处理见 [Docker 部署](docs/deployment.md)。一个账号与数据目录只运行一个实例。

## 给机器人一点性格

下面是 `config.toml` 中的配置片段；各情感值范围为 `0–1`。

```toml
[persona]
name = "bili-comment-bot"
personality = "温柔、真诚，有一点俏皮的日常陪伴者"
warmth = 0.8
humor = 0.5
empathy = 0.8

[limits]
dm_per_hour = 5
comment_per_hour = 5
whitelist = []

[discovery]
keywords = ["日常", "搞笑"]
invite_uids = [] # 填写希望邀请的用户数字 UID
```

`persona.name` 决定回复中的身份表达，B 站账号昵称需要手动设置。情感参数控制表达风格，模型的 `ai.temperature` 控制采样，两者独立。白名单仍须通过安全检查，私信仍要求发送者关注机器人。

## 视频如何被推荐

**均分 =（热度 + 推荐度 + 抽象度）÷ 3**，三项均为 `0–100` 分。

- **热度**：播放量、单位时间播放和互动指标，由确定性公式计算。
- **推荐度**：模型根据视频证据与热门评论样本判断。
- **抽象度 / 狗屎度**：越抽象、越有趣，分数越高。

| 均分 | 动作 |
| --- | --- |
| ≤ 60 | 不主动发布推荐 |
| > 60 且 ≤ 90 | 生成邀请评论，真实 @ 配置的用户 |
| > 90 | 点赞 → 生成鼓励评论 → 生成邀请评论并 @ 用户 |

前一步成功后才执行下一步；发布结果不确定时暂停，等待人工核实。热门评论样本不代表全部观众。公式、证据条件与恢复规则见 [评分说明](docs/scoring.md)。

## 本地开发

需要 Python 3.11+ 与 [uv](https://docs.astral.sh/uv/)；原生运行器支持 macOS / Linux。

```sh
uv sync --locked
uv run --locked bili-comment-bot --config config.example.toml config-check
uv run --locked bili-comment-bot demo
uv run --locked pytest -q
uv run --locked bili-comment-bot --config config.example.toml evaluate
python3 scripts/check_public_tree.py
```

`demo` 与默认离线 `evaluate` 不调用 B 站或模型服务。原生 CLI 不自动加载 `.env`，运行时需要通过环境变量注入密钥。代码规范、接口测试与贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 文档导航

| 想了解什么 | 文档 |
| --- | --- |
| 部署、配置与扫码 | [Docker 部署](docs/deployment.md) |
| 运行状态、备份与未知结果恢复 | [运维手册](docs/operations.md) |
| 数据流、额度与持久化设计 | [架构说明](docs/architecture.md) |
| 安全边界、提示与模型兼容 | [AI 安全约定](docs/ai-policy.md) |
| 无字幕视频与音频转写 | [转写说明](docs/transcription.md) |
| 模型评测与真实验收 | [评测手册](docs/evaluation.md) · [验证矩阵](docs/acceptance-matrix.md) |
| 隐私与安全问题 | [SECURITY.md](SECURITY.md) |

## 开源许可

采用 [MIT License](LICENSE)，可使用、修改与再分发，须保留版权与许可声明。第三方依赖及接口研究来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
