# B 站接口核对记录

核对日期：2026-09-30。以下是接口实现依据，尚未用真实账号验证线上兼容性。所有外部文字、代码和视频资料均按不可信输入处理。

## 来源与限制

本地初始没有 `wiki/` 或 `raw/`，因此联网核对公开项目的原始源码与发布包。

- [bilibili-api-python 17.4.2 发布包](https://pypi.org/project/bilibili-api-python/17.4.2/)：只读取包内 `data/api/{login,credential,session,user,video,search,common}.json`，没有安装或执行源项目。
- [SDK 登录实现](https://github.com/inorilzy/bilibili-api/blob/main/bilibili_api/login_v2.py)、[私信实现](https://github.com/inorilzy/bilibili-api/blob/main/bilibili_api/session.py)、[凭据实现](https://github.com/inorilzy/bilibili-api/blob/main/bilibili_api/utils/network.py)：核对参数、分页及刷新步骤。该 fork 不能视作已证实维护中的依赖。
- [Cookie 刷新研究](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/login/cookie_refresh.md)：研究者直接核对网页使用的 RSA-OAEP SHA-256 流程。
- [BiliCommentBot 源码](https://github.com/Janson20/BiliCommentBot/blob/main/bot.py)：作为功能参考；其 `get_refresh_csrf` 与完整 RSA 刷新流程不一致，且未完成旧刷新凭据确认，不直接照搬。

这些是社区对 B 站网页接口的原始研究与实现，不是 B 站承诺稳定的公共开发者接口。线上变化须由独立适配层处理。

## 登录与续期

| 操作 | 方法与接口路径 | 关键约束 |
| --- | --- | --- |
| 获取二维码 | GET passport.bilibili.com/x/passport-login/web/qrcode/generate | 返回 URL 与 qrcode_key；不记录携带凭据的 URL |
| 检查扫码 | GET passport.bilibili.com/x/passport-login/web/qrcode/poll | qrcode_key；区分待扫描、待确认、过期、成功 |
| 检查账号 | GET api.bilibili.com/x/web-interface/nav | 登录有效性、UID、WBI key 来源 |
| 检查续期 | GET passport.bilibili.com/x/passport-login/web/cookie/info | 使用当前 Cookie；返回 refresh 与服务器时间戳 |
| 取实时刷新令牌 | GET www.bilibili.com/correspond/1/{path} | path 为 refresh_时间戳 经公开 RSA key、OAEP SHA-256 加密的小写十六进制；HTML div#1-name |
| 换新 Cookie | POST passport.bilibili.com/x/passport-login/web/cookie/refresh | 旧 csrf、refresh_csrf、旧 refresh_token、source=main_web |
| 确认续期 | POST passport.bilibili.com/x/passport-login/web/confirm/refresh | **新 Cookie 的 csrf + 旧 refresh_token**，不可传新 refresh_token |

扫码成功同时持久化 Cookie 与 refresh_token。续期产生新凭据后先原子保存，再确认旧凭据失效；中断时应保留待确认状态以便恢复。账号完全失效和验证码要求重新扫码，不能循环伪装成功。

## 评论、私信和视频

| 能力 | 接口 | 核对要点 |
| --- | --- | --- |
| @ 消息 | GET api.bilibili.com/x/msgfeed/at | 游标 id、at_time；只接收视频评论，识别原评论及根楼层 |
| 评论列表 | GET api.bilibili.com/x/v2/reply | oid=视频 aid、type=1；样本有采样偏差，不能宣称全站舆情 |
| 发评论 | POST api.bilibili.com/x/v2/reply/add | 视频 type=1；子回复使用正确 root/parent；@ 的名字和 UID 必须对应 |
| 私信会话 | GET api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions | 会话列表分页；不能仅处理最后一条消息 |
| 私信增量 | GET api.vc.bilibili.com/session_svr/v1/session_svr/new_sessions | begin_ts 为微秒时间戳 |
| 会话消息 | GET api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs | talker_id、session_type=1、size、begin_seqno；持久化消息身份和游标 |
| 发私信 | POST api.vc.bilibili.com/web_im/v1/web_im/send_msg | msg[content] 为 JSON 文本；sender_uid 取当前登录账号；过滤自己发出的消息 |
| 关系 | GET api.bilibili.com/x/space/wbi/acc/relation | 核对**对方是否关注当前账号**，不能把 bot 关注对方当作资格 |
| 视频详情 | GET api.bilibili.com/x/web-interface/view | aid/BVID、分 P CID、发布时间、播放互动指标 |
| 字幕 | GET api.bilibili.com/x/player/wbi/v2 | aid、CID、WBI；后续字幕 URL 必须限制域名/协议/大小 |
| 音频地址 | GET api.bilibili.com/x/player/wbi/playurl | 无字幕时才进入有预算的转写流程；限制时长与下载范围 |
| 关键词搜索 | GET api.bilibili.com/x/web-interface/wbi/search/type | search_type=video、keyword、page；需要 WBI 签名 |
| 点赞 | POST api.bilibili.com/x/web-interface/archive/like | aid、like=1、csrf；90 分以上流程先点赞，再鼓励，再邀请 |

## 实现时须验证

自动测试应覆盖 Cookie 刷新中断恢复、关注关系方向、真实 @ 定位、分页与游标、超时导致的发布结果不确定、重启去重、60/90 严格阈值和回复分渠道额度。网络失败不能当作空列表、未关注或发布成功。

发布类请求不得盲目自动重试。平台没有通用幂等键；遇到结果不确定时，应冻结该动作并检查实际发布记录，不能声称实现了平台级 exactly-once。
