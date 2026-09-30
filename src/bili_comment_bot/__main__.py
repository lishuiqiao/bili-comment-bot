import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from .config import Settings, load_settings
from .dispatch import Dispatcher
from .domain import ActionKind, Decision, Mention, PublishAction, VideoScore
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


def main():
    parser = argparse.ArgumentParser(prog="bili-comment-bot")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("command", choices=["config-check", "demo", "run"])
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
    else:
        parser.exit(2, "Production platform/AI adapters are not connected in this milestone.\n")


if __name__ == "__main__":
    main()
