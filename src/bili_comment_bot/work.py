"""Typed business outcomes and immutable persisted discovery contract."""

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import Field, model_validator

from .domain import ActionStatus, Contract, VideoEvidence, VideoScore


class WorkState(StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    DEFERRED = "deferred"
    FAILED = "failed"
    ATTENTION = "attention"


@dataclass(frozen=True)
class WorkResult:
    state: WorkState
    value: object = None

    @classmethod
    def action(cls, status):
        state = {
            ActionStatus.SUCCEEDED: WorkState.COMPLETED,
            ActionStatus.SIMULATED: WorkState.COMPLETED,
            ActionStatus.FAILED: WorkState.FAILED,
            ActionStatus.UNCERTAIN: WorkState.ATTENTION,
        }.get(status, WorkState.SKIPPED)
        return cls(state, status)


@dataclass
class WorkBatch:
    counts: dict[str, int] = field(default_factory=lambda: {state.value: 0 for state in WorkState})

    def add(self, result: WorkResult):
        self.counts[result.state.value] += 1

    def merge(self, other):
        for key in self.counts:
            self.counts[key] += other.counts[key]
        return self

    @property
    def unhealthy(self):
        return any(self.counts[key] for key in ("deferred", "failed", "attention"))

    @property
    def total(self):
        return sum(self.counts.values())


@dataclass(frozen=True)
class Selection:
    items: list
    invalid: int = 0


class DiscoveryWorkflow(Contract):
    aid: int = Field(gt=0, strict=True)
    evidence: VideoEvidence
    score: VideoScore
    heat: dict
    invite_uids: list[int]
    policy_version: str
    prompt_version: str
    model: str

    @model_validator(mode="after")
    def evidence_binding(self):
        if self.aid != self.evidence.aid or not self.evidence.usable:
            raise ValueError("workflow evidence must be usable and bound to target")
        if self.score.heat != self.heat.get("score"):
            raise ValueError("heat must match the objective report")
        return self
