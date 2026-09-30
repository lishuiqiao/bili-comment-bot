"""Platform-independent contracts. Model output can never select side effects."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Channel(StrEnum):
    DM = "dm"
    COMMENT = "comment"


class FollowState(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class Decision(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"
    UNKNOWN = "unknown"


class SafetyVerdict(Contract):
    decision: Decision
    reason: str = Field(max_length=300)


class ReplyLocation(Contract):
    aid: int = Field(gt=0)
    root: int = Field(ge=0)
    parent: int = Field(gt=0)


class MessageEvent(Contract):
    id: str = Field(min_length=1)
    uid: int = Field(gt=0)
    channel: Channel
    text: str
    timestamp: float = Field(ge=0, allow_inf_nan=False)
    location: ReplyLocation | None = None

    @model_validator(mode="after")
    def comment_location(self):
        if self.channel == Channel.COMMENT and self.location is None:
            raise ValueError("comment summons require original reply location")
        return self


class VideoEvidence(Contract):
    aid: int = Field(gt=0)
    bvid: str
    title: str
    description: str = ""
    transcript: str = ""
    sources: list[str] = Field(default_factory=list)
    complete: bool = False
    duration: int = Field(default=0, ge=0)
    published_at: int = Field(default=0, ge=0)
    stats: dict[str, int] = Field(default_factory=dict)
    comments: list[str] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.complete and bool(self.transcript.strip()) and bool(self.sources)


class VideoScore(Contract):
    heat: float = Field(ge=0, le=100, allow_inf_nan=False, strict=True)
    recommendation: float = Field(ge=0, le=100, allow_inf_nan=False, strict=True)
    absurdity: float = Field(ge=0, le=100, allow_inf_nan=False, strict=True)
    reasons: list[str] = Field(min_length=3, max_length=3)

    @property
    def mean(self) -> float:
        return (self.heat + self.recommendation + self.absurdity) / 3


class ActionKind(StrEnum):
    LIKE = "like"
    ENCOURAGE = "encourage"
    INVITE = "invite"
    REPLY = "reply"


class ActionStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    SIMULATED = "simulated"
    BLOCKED = "blocked"


class Mention(Contract):
    uid: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=50)


class PublishAction(Contract):
    id: str
    kind: ActionKind
    aid: int | None = Field(default=None, gt=0)
    uid: int | None = Field(default=None, gt=0)
    channel: Channel | None = None
    text: str = ""
    location: ReplyLocation | None = None
    mentions: list[Mention] = Field(default_factory=list)
    dependency: str | None = None
    input_decision: Decision = Decision.UNKNOWN
    output_safe: bool = False
    evidence_usable: bool = False
    refusal: bool = False


class PublishReceipt(Contract):
    # Likes acknowledge a desired state without assigning a new remote resource ID.
    remote_id: str | None = Field(default=None, min_length=1)


class DefinitelyNotSent(Exception):
    """Adapter has proof no platform write succeeded; quota may be released."""


class UncertainWrite(Exception):
    """The platform might have accepted the write. Never automatically resend."""
