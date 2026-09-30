# 测试资料性质

本轮 HTTP fixtures 位于 `test_bilibili_auth.py` 和 `test_bilibili_contracts.py` 的小型服务对象及 helper 中，全部为**依据原始协议字段合成的响应**。它们不是截取的真实 B 站响应，没有真实 Cookie、令牌、UID 账号或私信内容。

来源与冲突在 `docs/bilibili-interface-notes.md` 逐项记录。WBI 的公开固定向量来自原始研究；补充 UTF-8/空格向量以明确 canonical query 独立计算。HTTP 超时、断连、异常类型、字段缺失和持久化失败样例是人工故障注入。

后续如加入真实脱敏响应，必须单独标记采集时间、接口、脱敏字段和真实数据性质；不得将现有合成样例改标成 live acceptance。
