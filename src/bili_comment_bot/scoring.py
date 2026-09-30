"""Versioned objective heat; model judgements cannot replace platform measurements."""

import math

from .domain import VideoEvidence

HEAT_VERSION = "heat-v1"


def heat_report(evidence: VideoEvidence) -> dict:
    stats = evidence.stats
    required = {"view", "like", "coin", "favorite", "share", "reply"}
    if not required <= stats.keys() or evidence.snapshot_at <= 0 or evidence.published_at <= 0:
        raise ValueError("measured statistics and timestamps are required")
    if any(type(stats[key]) is not int or stats[key] < 0 for key in required):
        raise ValueError("invalid measurements")
    if evidence.published_at > evidence.snapshot_at + 300:
        raise ValueError("publication timestamp exceeds snapshot")
    views = stats["view"]
    hours = max((evidence.snapshot_at - evidence.published_at) / 3600, 1)
    volume = min(100, math.log10(views + 1) / 6 * 100)
    velocity = min(100, math.log10(views / hours + 1) / 4 * 100)
    engagement = min(
        100,
        (
            stats["like"]
            + 2 * stats["coin"]
            + 2 * stats["favorite"]
            + 3 * stats["share"]
            + stats["reply"]
        )
        / max(views, 1)
        / 0.2
        * 100,
    )
    contributions = {
        "volume": 0.5 * volume,
        "velocity": 0.3 * velocity,
        "engagement": 0.2 * engagement,
    }
    return {
        "version": HEAT_VERSION,
        "score": sum(contributions.values()),
        "contributions": contributions,
        "metrics": stats,
        "snapshot_at": evidence.snapshot_at,
        "age_hours": hours,
    }
