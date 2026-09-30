# 测试资料性质

本轮 HTTP fixtures 位于 `test_bilibili_auth.py` 和 `test_bilibili_contracts.py` 的小型服务对象及 helper 中，全部为**依据原始协议字段合成的响应**。它们不是截取的真实 B 站响应，没有真实 Cookie、令牌、UID 账号或私信内容。

第 5 轮补充 `bili_read_fixtures.py`、`test_collectors.py`、`test_video_evidence.py`：评论/会话/消息、搜索/分 P/指标、热门评论/字幕轨道与字幕时间片均依照公开研究字段合成；分页循环、事务触发器失败、断连、缓存损坏和下载目标是人工故障注入。中文内容是测试自写短句，非真实用户消息或视频逐字稿。测试绑定 UID 42 仅为合成身份。

来源与冲突在 `docs/bilibili-interface-notes.md` 逐项记录。WBI 的公开固定向量来自原始研究；补充 UTF-8/空格向量以明确 canonical query 独立计算。HTTP 超时、断连、异常类型、字段缺失和持久化失败样例是人工故障注入。

第 6 轮 `ai_fixtures.py` 预置结构化模型答案和完整视频证据，`test_business_flows.py` 将其接入真实平台适配器的 MockTransport；没有访问模型或 B 站。`evals/safety-v1.jsonl` 的消息是自写用例，规则回归只验证 expected_rule，不能将预置 expected_decision 视为实际模型结果。

后续如加入真实脱敏响应，必须单独标记采集时间、接口、脱敏字段和真实数据性质；不得将现有合成样例改标成 live acceptance。

第 7 轮 transcription_fixtures.py 的 MP4 顶层盒是人工协议样例，内容不可播放；转写答案与时间戳也是假设数据。测试证明上传/响应契约、范围/来源与故障状态，不证明媒体解码、语言质量或真实供应商兼容。
