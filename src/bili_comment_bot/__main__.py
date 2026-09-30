import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from .config import Settings, load_settings
from .dispatch import Dispatcher
from .domain import ActionKind, Decision, Mention, PublishAction, VideoScore
from .instance_lock import InstanceInUse, InstanceLock
from .policy import discovery_steps
from .storage import Store


async def demo():
    from .adapters.fake import FakePlatform

    settings = Settings.model_validate({"discovery": {"invite_uids": [123]}})
    score = VideoScore(
        heat=95, recommendation=95, absurdity=95, reasons=["离线指标", "离线内容", "离线评论样本"]
    )
    platform = FakePlatform()
    with tempfile.TemporaryDirectory(prefix="bili-bot-demo-") as folder:
        store = await Store(Path(folder) / "state.db", namespace="sim").open()
        try:
            dispatcher = Dispatcher(settings, store, platform)
            previous = None
            result = []
            for kind in discovery_steps(score):
                action = PublishAction(
                    id=f"demo:{kind}",
                    kind=kind,
                    aid=1,
                    dependency=previous,
                    text="" if kind == ActionKind.LIKE else "离线演示内容（不发送）",
                    mentions=[Mention(uid=123, name="演示用户")]
                    if kind == ActionKind.INVITE
                    else [],
                    input_decision=Decision.ALLOW,
                    output_safe=True,
                    evidence_usable=True,
                )
                result.append({"action": kind, "status": await dispatcher.execute(action)})
                previous = action.id
            print(
                json.dumps(
                    {
                        "mode": "offline simulation",
                        "actions": result,
                        "platform_write_calls": len(platform.calls),
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            await store.close()


async def auth_command(settings: Settings, command: str):
    with InstanceLock(settings.data_dir) as lock:
        normalized = settings.model_copy(update={"data_dir": lock.directory})
        await _auth_command_locked(normalized, command)


async def _auth_command_locked(settings: Settings, command: str):
    from .adapters.bilibili.auth import AuthManager, QRStatus
    from .adapters.bilibili.auth_state import CredentialFile
    from .adapters.bilibili.client import BilibiliClient
    from .adapters.bilibili.transport import BiliTransport

    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(settings.data_dir, 0o700)
    store = await Store(settings.data_dir / "state.db", namespace=settings.namespace).open()
    transport = BiliTransport(settings)
    try:
        auth = AuthManager(
            settings,
            transport,
            CredentialFile(settings.data_dir / "auth.json"),
            identity_guard=store.bind_account,
        )
        client = BilibiliClient(settings, transport, auth, store)
        if command == "auth-status":
            if auth.state is None:
                print("未登录；请运行 login 并扫码。")
            else:
                print(f"UID={auth.state.uid}; renewal_state={auth.state.phase}")
                if auth.state.phase == "stable":
                    await client.verify_identity()
                    print("登录身份已在线核实。")
                else:
                    print("续期尚未完成，暂停发布；refresh-auth 会恢复可确定的阶段。")
        elif command == "refresh-auth":
            refreshed = await auth.refresh()
            await client.verify_identity()
            print("Cookie 已续期并确认。" if refreshed else "Cookie 当前无需续期。")
        else:
            challenge = await auth.generate_qr()
            image = settings.data_dir / "login.png"
            await asyncio.to_thread(challenge.write_image, image)
            print(f"请用哔哩哔哩手机 App 扫码：{image.resolve()}", flush=True)
            try:
                async with asyncio.timeout(180):
                    previous = None
                    while True:
                        status = await auth.poll_qr(challenge)
                        if status != previous:
                            display = {
                                QRStatus.WAITING_SCAN: "等待扫码",
                                QRStatus.WAITING_CONFIRM: "等待手机确认",
                                QRStatus.EXPIRED: "二维码已过期，请重新运行 login",
                                QRStatus.SUCCEEDED: "扫码成功",
                            }
                            print(f"登录状态：{display[status]}", flush=True)
                            previous = status
                        if status == QRStatus.EXPIRED:
                            raise RuntimeError("二维码已过期，请重新运行 login。")
                        if status == QRStatus.SUCCEEDED:
                            uid = await client.verify_identity()
                            print(f"登录完成，UID={uid}；凭据已私密保存。")
                            break
                        await asyncio.sleep(3)
            finally:
                image.unlink(missing_ok=True)
    finally:
        await transport.close()
        await store.close()


def main():
    parser = argparse.ArgumentParser(prog="bili-comment-bot")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument(
        "command", choices=["config-check", "demo", "run", "login", "auth-status", "refresh-auth"]
    )
    args = parser.parse_args()
    if args.command == "demo":
        asyncio.run(demo())
        return
    try:
        settings = load_settings(args.config)
    except (OSError, ValueError) as error:
        # Validation data can contain a supplied API key. Never print input representations.
        parser.exit(
            2, f"Configuration error ({type(error).__name__}); check TOML and field bounds.\n"
        )
    if args.command == "config-check":
        print(f"Configuration valid; mode={settings.namespace}; bot={settings.persona.name}")
    elif args.command in {"login", "auth-status", "refresh-auth"}:
        from .adapters.bilibili.errors import (
            CaptchaRequired,
            IdentityMismatch,
            LoginExpired,
            ReauthenticationRequired,
        )

        try:
            asyncio.run(auth_command(settings, args.command))
        except InstanceInUse:
            parser.exit(2, "此数据目录正在使用；请先停止占用它的登录命令或 bot 服务。\n")
        except ReauthenticationRequired:
            parser.exit(2, "续期请求结果无法确定，已暂停发布；请重新运行 login 扫码。\n")
        except IdentityMismatch:
            parser.exit(2, "账号与配置或数据库绑定不符；使用正确账号或独立数据目录。\n")
        except CaptchaRequired:
            parser.exit(2, "B 站要求验证，已暂停；请在手机 App 完成验证后重新扫码。\n")
        except LoginExpired:
            parser.exit(2, "登录已失效或尚未登录；请运行 login 扫码。\n")
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            parser.exit(2, f"登录操作未完成（{type(error).__name__}）；检查配置和连接后重试。\n")
        except Exception as error:
            # Sanitized failure class only; response/request objects can contain secrets.
            parser.exit(
                2, f"平台操作未完成（{type(error).__name__}）；失效或验证码时请重新扫码。\n"
            )
    else:
        parser.exit(2, "Production platform/AI adapters are not connected in this milestone.\n")


if __name__ == "__main__":
    main()
