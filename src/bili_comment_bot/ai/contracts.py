from typing import Literal

from pydantic import ConfigDict, Field, StrictBool

from ..domain import Contract, Decision


class AIContract(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


Category = Literal["allowed", "harmful", "injection", "out_of_scope", "unrelated", "unknown"]


class InputAssessment(AIContract):
    decision: Decision
    category: Category


class OutputAssessment(AIContract):
    safe: StrictBool
    category: Category


class GeneratedText(AIContract):
    text: str = Field(min_length=1, max_length=2000)
    citations: list[str] = Field(max_length=100)


class ContentRating(AIContract):
    recommendation: float = Field(ge=0, le=100, allow_inf_nan=False)
    absurdity: float = Field(ge=0, le=100, allow_inf_nan=False)
    recommendation_reason: str = Field(min_length=1, max_length=300)
    absurdity_reason: str = Field(min_length=1, max_length=300)
    citations: list[str] = Field(min_length=1, max_length=100)
