from .domain import ActionKind, Channel, Decision, FollowState, MessageEvent, VideoScore


def discovery_steps(score: VideoScore) -> list[ActionKind]:
    if score.mean > 90:
        return [ActionKind.LIKE, ActionKind.ENCOURAGE, ActionKind.INVITE]
    if score.mean > 60:
        return [ActionKind.INVITE]
    return []


def eligible(event: MessageEvent, follows: FollowState) -> bool:
    return event.channel == Channel.COMMENT or follows == FollowState.YES


def may_generate(decision: Decision) -> bool:
    return decision != Decision.UNKNOWN
