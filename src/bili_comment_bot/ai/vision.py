"""Bounded local frame descriptions with host-owned provenance."""

from ..domain import VisualObservation
from .client import AIError


class VisionClient:
    def __init__(self, settings, runner):
        self.settings, self.runner = settings, runner

    async def analyse(self, video: bytes, duration: int, frames: int):
        if not 0 < frames <= self.settings.vision.max_frames:
            raise AIError("visual_frame_budget")
        body = await self.runner.run("vision", {"duration": duration, "frames": frames}, video)
        try:
            observations = [VisualObservation.model_validate(item) for item in body]
            times = [t for item in observations for t in item.timestamps]
            if (
                not times
                or len(times) > frames
                or times != sorted(set(times))
                or any(t > duration + 0.1 for t in times)
            ):
                raise ValueError("invalid sample")
            return observations
        except (ValueError, TypeError):
            raise AIError("invalid_visual_observation") from None
