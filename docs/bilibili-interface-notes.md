# B 站接口核对记录

核对日期：2026-09-30。以下是接口实现依据，完整真实账号登录与续期的线上兼容性仍待验收。所有外部文字、代码和视频资料均按不可信输入处理。

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

二维码**展示地址**与生成、轮询的 **API 地址**分别校验。[二维码登录原始研究](https://github.com/pskdje/bilibili-API-collect/blob/master/docs/login/login_action/QR.md)的样例使用 `passport.bilibili.com`；兼容排查的脱敏诊断另确认了 HTTPS `account.bilibili.com` 返回地址。展示地址只允许这两个精确主机，省略端口或显式 `443`；拒绝用户信息、伪装域名、畸形地址、空白/控制字符、反斜杠及 fragment。路径与查询保持原样，不记录完整地址或二维码 key，也不请求展示地址。

生成、轮询仍请求 `passport.bilibili.com`，出站 API origins 不因新增展示主机而扩大。轮询成功 URL 的凭据解析规则也保持独立。这次兼容修复使用合成响应进行离线回归，脱敏域名诊断不能替代完整扫码、持久化及自动续期的真实验收。

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

## 认证与签名核对

- 二维码轮询内部 `data.code`：86101 待扫码、86090 待确认、86038 过期、0 成功；成功凭据来自 Set-Cookie，或仅在缺少必要 Cookie 时解析 passport HTTPS 返回 URL 的查询字段。不会访问返回 URL。来源为上述 SDK 登录实现及发布包。
- 续期使用服务器毫秒时间。公开 PEM 公钥与 SDK/刷新研究一致；算法测试以独立 RSA 私钥解密确认 `refresh_时间戳` 与 OAEP/SHA-256。确认接口只说明使旧 token 对应 Cookie 失效，**没有公开的重复确认幂等承诺**，因此 confirm_started 的未知结果不自动重发。
- [WBI 原始研究](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/misc/sign/wbi.md)提供固定向量：img `7cd084941338484aae1ad9425b84077c`、sub `4932caff0ff746eab6f01bf08b70ac45`、mixin `ea1db124af3c7062474693fa704f4ff8`；foo=114/bar=514/zab=1919810、wts=1702204169 的摘要为 `8f6f2b5b3d485fe1886cec6a0be8c5d4`。过滤 `!'()*`，排序，UTF-8 百分号编码，空格按 `%20`。研究中的部分 Python 示例仍用默认 urlencode（空格 `+`），与正文存在冲突，本实现按正文的规范编码。补充合成向量的明确 query 为 `space=one%20one&wts=1&%E4%B8%AD%E6%96%87=%E4%BA%94%E4%B8%80%E5%9B%9B`，拼接上述 mixin 后独立摘要为 `414570c2de009b0d0dd8b3e67ea7a314`。
- **关注方向的资料冲突**：[关系研究表格](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/user/relation.md)把 relation/be_relation 的方向文字写成与 [实际私信 bot 的 is_following_me 实现](https://github.com/7Hello80/Bilibili_PrivateMessage_Bot/blob/main/index.py)相反。研究示例中 relation.mid 是查询用户，be_relation.mid 是登录用户；原始 bot 用 be_relation.attribute 判断对方关注 bot。本实现验证两个 mid 所属后按实际 bot 的方向处理（属性 1/2/6 代表悄悄/单向/互相关注）。自动 fixture 是该约定的合成验证，**尚非本项目真实双账号单向关注实证**；上线验收仍需分别测试“只有 bot 关注用户”和“只有用户关注 bot”，不能把资料冲突写成已在线证明。
- [原始评论接口研究](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/comment/action.md)核对 root/parent、rpid、验证码与拒绝码；[OpenCLI 评论原始源码](https://github.com/jackwener/opencli/blob/main/clis/bilibili/comment.js)核对真实提及使用 `at_name_to_mid` 的 JSON 名称→UID 映射与 `@名称` 正文。项目从配置 UID 获取平台名字，发布前重新验证，名字改变不自动换目标。
- [原始私信研究](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/message/private_msg.md)核对 `msg[dev_id]` 为 UUIDv4、秒级 timestamp、JSON content、2000 字节限制、双 csrf 字段和 WBI 的 `w_sender_uid/w_receiver_id/w_dev_id` 参数。

固定向量为公开协议材料，非当前口令。其余测试响应由协议字段合成，未复制真实账号数据；错误与中断样例是故障注入。源码仅用于核对协议事实，生产代码为本项目独立实现，不调用上述项目，也不执行下载源码。

## 采集与字幕核对

以下补充材料来自联网读取原始仓库文档；其余优先沿用已经下载的本地发布包和研究源码。没有新增可证实的官方稳定性承诺，也没有真实账号请求。

- @ 接口路径及 Cookie 要求来自本地 SDK session.json/session.py；视频通知的 at_time、source_id、subject_id、root_id 来自 [BiliInsight 原始解析器](https://github.com/Shanoa2/BiliInsight/blob/main/src/bilichat/bot/models.py)与监听器。其已有“预算到达后直接更新最新水位”做法可能漏掉积压，本项目独立实现固定 head/完整 watermark/续点分离，没有移植 GPL 代码。[@/回复通知对象研究](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/message/msg.md)主要描述 reply feed 的 cursor.time/is_end 与 at_details；@ 的 at_time 使用 SDK/实际实现约定。认证 @ feed 本身是目标账号依据，非正文名字匹配；显式提及列表非空时再核对 bot UID。root_id=0 的顶层召唤以 source_id 为回复根，缺少 root_id 不猜测。
- [私信原始协议](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/message/private_msg.md)：get_sessions 的 begin_ts/end_ts 与 session_ts 是微秒，size 最大 100；session_type=4 包含全部类型，处理时仅取类型 1。is_follow 表示 bot 关注对方，不能用它取代发送者关注 bot 的门禁。消息列表倒序，begin_seqno/end_seqno 都不包括边界，size=0 或缺省只返回系统提示，本项目显式 100。receiver_type、双方 UID、msg_status 校验后仅转换有效文本；msg_key 以整数读取再构造字符串事件键，避免浮点精度损失。会话发现带目标序号，不能只回复 last_msg。SDK 简化注释“近三十条”与协议的 size 最大 2000 并不等价，本项目按精确协议的显式分页参数实现。
- 会话时间是否包含边界缺少一致说明，也没有已核对的 UID 次序游标；实现重叠查询并检查前进，相同时间密集边界无法排空时停止该页，不能自称解决任意碰撞。
- [视频详情原始研究](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/info.md)、[搜索研究](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/search/search_request.md)、[评论列表研究](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/comment/list.md)：view 的 pages 中 CID/page/duration/part 与 stat 各计数；视频分类搜索分页 result，aid 去重、标题 em 高亮转纯文本；reply 使用 type=1、sort=2、pn/ps，携带 page.count 和样本截断信息。仅采热门评论，不宣称总体舆情。
- [播放器原始研究](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/player.md)：player/wbi/v2 返回 aid/cid 和 subtitle.subtitles，轨道 lan/subtitle_url，示例资源主机 aisubtitle.hdslb.com。下载器只允许这个确切 HTTPS 主机；如果新增主机，须新增一手依据与目标限制测试。协议相对地址只补 HTTPS，不沿重定向扩展范围。字幕 body 的 from/to/content 时间片与 SDK 的字幕使用方式一致；全部选定分 P 的轨道取得并校验才标完整，没有替代视觉理解。

搜索、消息、元数据和字幕样例仍是合成契约测试；接口变化、目标路由、分页结束条件与双账号关系方向须列入 live acceptance。错误码通过脱敏 PlatformError.code 保留，与明确无字幕状态分开；未知结构不会当成没有内容。

## 音频取流核对

本地 SDK 发布包已给出 signed GET `/x/player/wbi/playurl`、avid/cid 和 DASH 音轨字段。缺少 CDN/完整结构时补充联网读取[播放流原始研究](https://raw.githubusercontent.com/pskdje/bilibili-API-collect/master/docs/video/videostream_url.md)。使用 fnval=16 请求普通 DASH，不申请会员绕过。baseUrl/base_url 和备用地址有别名；默认地址同时存在却不一致会拒绝。timelength 为毫秒，dash.duration 为秒。

取流前核实 aid 的分 P 成员，核对 CID 与时长，响应若提供 aid/cid 也须一致。playurl 不保证回显 ID，绑定依据为已核实分 P、签名请求与时长，不宣称每次响应回显。只选择 AAC mp4a.* 和最低 bandwidth。

download.py::AUDIO_HOSTS 允许研究示例中的 13 个确切 bilivideo.com 主机，未放开域名后缀。mcdn.bilivideo.cn 非标准端口不允许，可在传输前选择已核对的 HTTPS 443 备用主机；传输失败不再自动重试。未知目标、IP、用户信息、重定向和非预期端口拒绝，Cookie/Authorization 显式为空，签名 URL 不持久化。

dash.audio 明确 null/空列表才返回无音频；缺字段、未知音轨、目标/时长错误为 ProtocolFault。失效为 LoginExpired；访问限制保留 PlatformError.code 或 HTTPFault 状态，不当无音频，也不猜全部权限码含义。音频与字幕共享累计预算。测试均为合成边界，未验证真实 DASH 可由配置供应商解码。
