# 第三方来源与许可

本项目原创代码与文档采用 [MIT License](LICENSE)。第三方软件、平台内容及商标保留各自权利；本项目的 MIT 许可不替代它们的许可。

## 运行依赖

以下信息来自锁定版本的发行包元数据与许可文件。完整依赖及版本见 `uv.lock`，具体条款以各发行包随附的许可为准。wheel 不内嵌第三方源码；Docker 镜像安装依赖并保留其发行包中的许可文件。

| 直接依赖 | 许可 | 上游 |
| --- | --- | --- |
| aiosqlite | MIT | [omnilib/aiosqlite](https://github.com/omnilib/aiosqlite) |
| HTTPX | BSD-3-Clause | [encode/httpx](https://github.com/encode/httpx) |
| Pydantic | MIT | [pydantic/pydantic](https://github.com/pydantic/pydantic) |
| cryptography | Apache-2.0 OR BSD-3-Clause | [pyca/cryptography](https://github.com/pyca/cryptography) |
| qrcode | BSD；其可选资源有独立许可 | [lincolnloop/python-qrcode](https://github.com/lincolnloop/python-qrcode) |
| Pillow（二维码图像依赖） | MIT-CMU | [python-pillow/Pillow](https://github.com/python-pillow/Pillow) |

## 接口研究参考

平台适配器依据公开接口事实独立实现，未将下列项目作为运行依赖，也未移植其 GPL 业务代码。公开 RSA 公钥、WBI 排列表和签名测试向量是协议材料，不是账号凭据。具体参考范围、资料冲突和实现差异见 [接口核对记录](docs/bilibili-interface-notes.md)。

- [bilibili-api-python 17.4.2](https://pypi.org/project/bilibili-api-python/17.4.2/)：GPL-3.0 发行包，仅用于核对接口与协议。
- [bilibili-API-collect](https://github.com/pskdje/bilibili-API-collect)：接口研究文档，保留原项目的许可。
- [BiliInsight](https://github.com/Shanoa2/BiliInsight)：通知字段与解析行为参考。
- [BiliCommentBot](https://github.com/Janson20/BiliCommentBot)：MIT 项目，功能及登录流程对照。
- [OpenCLI](https://github.com/jackwener/opencli)：评论提及参数参考。
- [Bilibili_PrivateMessage_Bot](https://github.com/7Hello80/Bilibili_PrivateMessage_Bot)：关注关系方向对照。

哔哩哔哩及 Bilibili 名称属于其权利人。本项目是独立的社区项目。
