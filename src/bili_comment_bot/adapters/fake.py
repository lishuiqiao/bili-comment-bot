"""Offline fixture adapter. Never imported by the production service."""

from ..domain import FollowState, PublishAction, PublishReceipt


class FakePlatform:
    def __init__(
        self, outcomes: dict[str, Exception] | None = None, follows: FollowState = FollowState.YES
    ):
        self.calls: list[PublishAction] = []
        self.outcomes = outcomes or {}
        self.follows = follows

    async def sender_follows_bot(self, uid: int) -> FollowState:
        return self.follows

    async def publish(self, action: PublishAction) -> PublishReceipt:
        self.calls.append(action)
        if action.id in self.outcomes:
            raise self.outcomes[action.id]
        return PublishReceipt(remote_id=f"fake-{len(self.calls)}")
