# AI 与业务安全约定

提示版本 `bili-comment-bot-v3`，策略版本 `companion-video-v1`。所有六种用途共用人格与情感参数：私信陪伴、字幕/转写总结、安全/用途拒绝、证据不足说明、鼓励和邀请。persona.warmth/humor/empathy 是表达风格，ai.temperature 是采样，两者独立；人格不改变安全边界。正文不展示技术参数。

系统规则与用户/视频资料分开，后者全部通过 JSON 数据传入。模型没有平台工具，只能返回类型化文本、安全结论、内容评分和引用；不能指定 UID、楼层、视频或动作。配置用户 UID 与平台核对名字后由应用构造真实 @。

私信必须对方关注 bot，只允许日常陪伴，拒绝编程等专业任务；谈论工作造成的情绪仍属于陪伴。评论先做无需视频证据的安全初筛，再加载对应视频，检查来源和视频相关性，不加关注门槛。长度、Unicode 规范化的配置非安全词、明确注入/有害/用途模式与模型判断共同检查；规则表不声称能识别所有攻击。判断不确定或检查失败不批准。

确定拒绝仍智能生成，生成器只接收拒绝类别与渠道，不接收原攻击文本，也不会复述危险细节。视频证据不足用独立用途生成说明，不指责用户违法。输出再次通过确定性长度/非安全词/@ 门禁及独立模型检查；模型在生成回复时自报安全无效，输出失败不会无限重写。拒绝与正常回复使用同一 Dispatcher/额度，超额没有额外提示。

只有完整范围证据可总结或评分。引用须来自本次 sources，整部总结覆盖全部分 P；应用依据实际来源附加字幕/音频转写/混合说明，启用视觉时明确仅分析抽样画面，否则说明未分析画面。缺少任一分 P 的有效证据、假引用或少于 3 条评论样本不产生推荐资格。转写来源与字幕一样经过安全检查，缓存和预算见 [转写说明](transcription.md)。

事件与动作同事务保存完成，再派发；持久化 payload 不覆盖。结果不确定的写入冻结依赖，点赞成功后才生成鼓励、鼓励成功后才生成邀请。模型或安全失败保留事件/工作流，持久退避 30 秒起，最高 1800 秒，不在单轮里无限重试。平台写入继续不自动重试。运行器负责各渠道调度、持久退避与聚合健康状态。

`evals/safety-v1.jsonl` 为自写版本化测试集，expected_rule 仅表示确定性规则的预期，null 不等于安全；expected_decision 用于真实模型评测。MockTransport 用预置答案验证协议、流程和门禁，不能证明真实模型识别率、事实准确性或人格效果。真实模型效果须使用部署者配置单独评估误拒绝、漏检、来源忠实性和六种用途的人格表达。

## 兼容接口

按 [官方 Chat Completions 契约](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)使用固定配置 base_url、Bearer 鉴权与非流式 choices/message；仅接受单个 assistant、finish_reason=stop、非空内容、无工具调用/模型 refusal，输出通过严格 Pydantic 校验。字段或数值错误不能从任意文本中提取 JSON “修复”。

[官方结构化输出说明](https://developers.openai.com/api/docs/guides/structured-outputs)区分 JSON 格式与 schema 保证。可显式配置 json_object、json_schema 或仅提示的 prompt 模式，均需本地契约校验；供应商不支持时不自动降级。token_parameter 支持现代 max_completion_tokens 与兼容供应商的 max_tokens；不支持 temperature 的模型可关闭 send_temperature。

API key 仅环境配置，不传入提示或日志；用户消息/完整请求响应也不记录。HTTP 默认要求 HTTPS，不跟重定向，私有部署 HTTP 需显式 allow_insecure_http。总超时、并发、输入字符、输出 token、响应字节和每分钟调用数有限；默认零重试，可显式开启最多两次对 429/503 的重试。重试可能产生额外模型调用成本，不能当作平台写入的重试策略。
