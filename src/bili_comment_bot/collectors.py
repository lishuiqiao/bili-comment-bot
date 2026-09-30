"""Bounded, resumable reverse-page scans; complete watermarks never cross gaps."""

import time
from collections import Counter
from collections.abc import Callable

from pydantic import Field

from .adapters.bilibili.errors import PlatformError, ProtocolFault
from .config import Settings
from .domain import Contract
from .ports import CollectionPort
from .storage import Store


class AtProgress(Contract):
    watermark: tuple[int, int]
    head: tuple[int, int] | None = None
    older: tuple[int, int] | None = None


class SessionProgress(Contract):
    watermark_us: int = Field(ge=0, strict=True)
    history_since: int = Field(ge=0, strict=True)
    head_us: int | None = None
    end_us: int | None = None


class DmProgress(Contract):
    watermark: int = Field(default=0, ge=0, strict=True)
    head: int | None = None
    end: int | None = None


class Collectors:
    def __init__(
        self,
        settings: Settings,
        api: CollectionPort,
        store: Store,
        clock: Callable[[], float] = time.time,
    ):
        self.settings, self.api, self.store, self.clock = settings, api, store, clock

    async def _load(self, stream, model, initial):
        raw = await self.store.cursor(stream)
        if raw is None:
            state = initial()
            # Persist bootstrap cutoff before HTTP: a failed first request cannot move it.
            await self.store.enqueue_batch([], stream, state.model_dump_json())
            return state
        try:
            return model.model_validate_json(raw)
        except ValueError:
            raise ProtocolFault() from None

    async def collect_at(self) -> int:
        state = await self._load(
            "at",
            AtProgress,
            lambda: AtProgress(
                watermark=(
                    max(0, int(self.clock()) - self.settings.platform.history_lookback_seconds),
                    0,
                )
            ),
        )
        count = 0
        for _ in range(self.settings.platform.max_pages):
            page = await self.api.at_page(state.older)
            positions = [item.position for item in page.records]
            if positions != sorted(positions, reverse=True):
                raise ProtocolFault()
            head = state.head or max([state.watermark, *positions])
            done = page.is_end or any(pos <= state.watermark for pos in positions)
            if not done and (
                page.older is None
                or not positions
                or page.older > min(positions)
                or (state.older is not None and page.older >= state.older)
            ):
                raise ProtocolFault()
            eligible = [item for item in page.records if state.watermark < item.position <= head]
            events = [item.event for item in eligible if item.event is not None]
            ignored = dict(Counter(item.ignored for item in eligible if item.ignored))
            next_state = (
                AtProgress(watermark=head)
                if done
                else AtProgress(watermark=state.watermark, head=head, older=page.older)
            )
            await self.store.save_collection(
                events, "at", next_state.model_dump_json(), ignored=ignored
            )
            count += len(events)
            state = next_state
            if done:
                break
        return count

    async def discover_dms(self) -> SessionProgress:
        state = await self._load(
            "dm:sessions",
            SessionProgress,
            lambda: SessionProgress(
                watermark_us=max(
                    0, int(self.clock()) - self.settings.platform.history_lookback_seconds
                )
                * 1_000_000,
                history_since=max(
                    0, int(self.clock()) - self.settings.platform.history_lookback_seconds
                ),
            ),
        )
        for _ in range(self.settings.platform.max_pages):
            # Inclusive overlap protects equal timestamps. A boundary that cannot progress
            # stops the page rather than silently skipping an unknown number of sessions.
            page = await self.api.session_page(max(0, state.watermark_us - 1), state.end_us)
            stamps = [item.updated_us for item in page.records]
            if stamps != sorted(stamps, reverse=True) or (page.has_more and not stamps):
                raise ProtocolFault()
            head = state.head_us or max([state.watermark_us, *stamps])
            done = not page.has_more or any(stamp < state.watermark_us for stamp in stamps)
            end = min(stamps) + 1 if stamps else None
            if not done and state.end_us is not None and end >= state.end_us:
                raise ProtocolFault()
            jobs, ignored = [], Counter()
            for item in page.records:
                if not state.watermark_us <= item.updated_us <= head:
                    continue
                if not item.supported or not item.latest_seq:
                    ignored["unsupported_session"] += 1
                    continue
                raw = await self.store.cursor(f"dm:{item.uid}")
                if raw is None or DmProgress.model_validate_json(raw).watermark < item.latest_seq:
                    jobs.append((item.uid, item.latest_seq))
            next_state = SessionProgress(
                watermark_us=head if done else state.watermark_us,
                history_since=state.history_since,
                head_us=None if done else head,
                end_us=None if done else end,
            )
            await self.store.save_collection(
                [], "dm:sessions", next_state.model_dump_json(), jobs=jobs, ignored=dict(ignored)
            )
            state = next_state
            if done:
                break
        return state

    async def collect_dms(self) -> int:
        # Discovery and message pages have separate finite budgets so a discovery backlog
        # cannot starve already durable jobs. Total DM requests <= 2 * max_pages per call.
        errors = []
        try:
            state = await self.discover_dms()
        except PlatformError as error:
            errors.append(error)
            # Bootstrap is already durable. Discovery failure must not starve known jobs.
            raw = await self.store.cursor("dm:sessions")
            if raw is None:
                raise
            state = SessionProgress.model_validate_json(raw)
        count = 0
        for uid, target in await self.store.dm_jobs(self.settings.platform.max_pages):
            stream = f"dm:{uid}"
            progress = await self._load(stream, DmProgress, DmProgress)
            try:
                count += await self._dm_job(state, uid, target, progress)
            except PlatformError as error:
                errors.append(error)
                # Preserve the gap and rotate even a poison page; other conversations run.
                await self.store.save_collection(
                    [],
                    stream,
                    progress.model_dump_json(),
                    touched_job=uid,
                    ignored={"page_error:" + type(error).__name__: 1},
                )
        if errors:
            raise errors[0]
        return count

    async def _dm_job(
        self, state: SessionProgress, uid: int, target: int, progress: DmProgress
    ) -> int:
        page = await self.api.dm_page(uid, progress.watermark, progress.end)
        seqs = [item.seq for item in page.records]
        if seqs != sorted(seqs, reverse=True):
            raise ProtocolFault()
        head = progress.head or max([progress.watermark, *seqs])
        done = not page.has_more or any(seq <= progress.watermark for seq in seqs)
        end = min(seqs) if seqs else None
        if not done and (end is None or (progress.end is not None and end >= progress.end)):
            raise ProtocolFault()
        if done and not seqs and target > head:
            raise ProtocolFault()
        eligible = [item for item in page.records if progress.watermark < item.seq <= head]
        events = [
            item.event
            for item in eligible
            if item.event is not None and item.event.timestamp >= state.history_since
        ]
        ignored = Counter(item.ignored for item in eligible if item.ignored)
        ignored["before_initial_history"] += sum(
            item.event is not None and item.event.timestamp < state.history_since
            for item in eligible
        )
        next_state = (
            DmProgress(watermark=head)
            if done
            else DmProgress(watermark=progress.watermark, head=head, end=end)
        )
        await self.store.save_collection(
            events,
            f"dm:{uid}",
            next_state.model_dump_json(),
            completed_job=(uid, head) if done else None,
            touched_job=uid,
            ignored={key: value for key, value in ignored.items() if value},
        )
        return len(events)
