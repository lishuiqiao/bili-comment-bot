"""Single-instance production lifecycle and bounded, independently scheduled work."""

import asyncio
import os
import signal
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass

from .adapters.bilibili.auth import AuthManager
from .adapters.bilibili.auth_state import CredentialFile
from .adapters.bilibili.client import BilibiliClient
from .adapters.bilibili.collection import CollectionAPI
from .adapters.bilibili.download import Downloader
from .adapters.bilibili.errors import AUTH_FAILURES, AuthFault
from .adapters.bilibili.transport import BiliTransport
from .adapters.bilibili.video import VideoAPI
from .ai.client import AIClient
from .ai.service import AIService
from .ai.transcription import TranscriptionClient
from .collectors import Collectors
from .evidence import EvidenceService
from .instance_lock import InstanceLock
from .observability import log_result, write_private
from .safety import SafetyService
from .service import BusinessService
from .storage import Store
from .work import WorkBatch, WorkResult, WorkState


@dataclass
class RuntimeIO:
    """Only HTTP transports/clocks are injectable; production wiring stays identical."""

    platform: object = None
    model: object = None
    download: object = None
    transcription: object = None
    read_wait: object = None
    write_wait: object = None
    monotonic: object = time.monotonic
    wall_clock: object = time.time
    wait: object = None


async def wait_for_stop(stop, delay):
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


class Scheduler:
    """One loop per job, finite batches and a shared bounded business work pool."""

    def __init__(
        self,
        settings,
        store,
        collectors,
        video,
        business,
        auth,
        fault,
        *,
        io=None,
        stop=None,
        snapshot=None,
    ):
        self.settings, self.store = settings, store
        self.collectors, self.video, self.business, self.auth = collectors, video, business, auth
        self.fault, self.io = fault, io or RuntimeIO()
        self.stop = stop or asyncio.Event()
        self.snapshot = snapshot
        self.pool = asyncio.Semaphore(settings.limits.concurrency)
        self.last_success = {}
        self.last_attempt = {}
        self.batches = {}
        self.failures = {}
        self.tasks = []

    def check(self):
        self.fault.check()

    async def _map(self, values, callback, *, job="events"):
        def identity(value):
            if job == "actions":
                return "action:" + value.id
            return f"discovery:{value}" if job == "discovery" else value

        async def one(value):
            async with self.pool:
                if self.stop.is_set():
                    return WorkResult(WorkState.SKIPPED)
                self.check()
                key = identity(value)
                try:
                    result = await callback(value)
                    if not isinstance(result, WorkResult):
                        raise RuntimeError("untyped work outcome")
                    self.check()
                    if result.state in {WorkState.COMPLETED, WorkState.FAILED, WorkState.ATTENTION}:
                        await self.store.clear_work(key)
                    return result
                except asyncio.CancelledError:
                    raise
                except AUTH_FAILURES as error:
                    self.fault.notify(error)
                    raise
                except Exception as error:
                    self.check()
                    await self.store.defer_work(key, event=job == "events")
                    log_result(job, "failed", self.store.ns, error=error)
                    return WorkResult(WorkState.FAILED)

        children = [asyncio.create_task(one(value)) for value in values]
        try:
            results = await asyncio.gather(*children)
            batch = WorkBatch()
            for result in results:
                batch.add(result)
            return batch
        finally:
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)

    async def events(self):
        selection = await self.store.event_batch(self.settings.runtime.batch_size)
        batch = await self._map(
            [value.id for value in selection.items],
            self.business.process_event_result,
            job="events",
        )
        batch.counts["attention"] += selection.invalid
        return batch

    async def actions(self):
        selection = await self.store.action_batch(self.settings.runtime.batch_size)

        async def execute(action):
            return WorkResult.action(await self.business.dispatcher.execute(action))

        batch = await self._map(selection.items, execute, job="actions")
        batch.counts["attention"] += selection.invalid
        return batch

    async def search(self):
        if self.settings.discovery.keywords and self.settings.discovery.invite_uids:
            candidates = await self.video.search_candidates()
            await self.store.enqueue_candidates(candidate.aid for candidate in candidates)

    async def discovery(self):
        if not self.settings.discovery.invite_uids:
            return WorkBatch()
        limit = self.settings.runtime.batch_size
        flows = await self.store.workflow_batch(limit)
        candidates = await self.store.due_candidates(limit)
        batch = await self._map(flows.items, self.business.discover_video_result, job="discovery")
        batch.merge(
            await self._map(candidates, self.business.discover_video_result, job="discovery")
        )
        batch.counts["attention"] += flows.invalid
        return batch

    async def refresh(self):
        await self.auth.refresh()

    def jobs(self):
        settings = self.settings
        jobs = {
            "at": (self.collectors.collect_at, settings.platform.poll_interval),
            "dm": (self.collectors.collect_dms, settings.platform.poll_interval),
            "search": (self.search, settings.discovery.interval),
            "events": (self.events, settings.runtime.worker_interval),
            "actions": (self.actions, settings.runtime.worker_interval),
            "discovery": (self.discovery, settings.runtime.worker_interval),
            "refresh": (self.refresh, settings.platform.refresh_interval),
        }

        if not (settings.discovery.keywords and settings.discovery.invite_uids):
            jobs.pop("search")
        if not settings.discovery.invite_uids:
            jobs.pop("discovery")
        return jobs

    def health(self, counts, *, alive, ready):
        if self.fault.event.is_set():
            return "needs_login", ["authentication"]
        if not alive:
            return "stopped", ["process_stopped"]
        if counts.get("quarantined", 0) or counts.get("actions", {}).get("uncertain", 0):
            return "attention", [
                "quarantined_records" if counts.get("quarantined", 0) else "uncertain_actions"
            ]
        reasons = [name + "_failed" for name in self.jobs() if self.failures.get(name, 0)]
        if counts.get("work_retries", 0):
            reasons.append("pending_retries")
        if reasons:
            return "degraded", reasons
        if not ready or any(name not in self.last_success for name in self.jobs()):
            return "starting", ["initial_checks"]
        return "normal", []

    async def step(self, name, callback):
        self.check()
        started = self.io.monotonic()
        self.last_attempt[name] = self.io.wall_clock()
        batch = None
        error = None
        try:
            result = await callback()
            self.check()
            if isinstance(result, WorkBatch):
                batch = result
                self.batches[name] = dict(batch.counts)
                if not batch.unhealthy:
                    if batch.total:
                        self.failures[name] = 0
                        self.last_success[name] = self.io.wall_clock()
                    elif name not in self.last_success and not self.failures.get(name, 0):
                        # First empty poll checks the store successfully, but a later
                        # empty batch cannot clear an earlier processing failure.
                        self.last_success[name] = self.io.wall_clock()
                    log_result(
                        name,
                        "success",
                        self.store.ns,
                        duration=self.io.monotonic() - started,
                        counts=batch.counts,
                    )
                    return 0
            else:
                self.failures[name] = 0
                self.last_success[name] = self.io.wall_clock()
                log_result(name, "success", self.store.ns, duration=self.io.monotonic() - started)
                return 0
        except asyncio.CancelledError:
            raise
        except Exception as caught:
            error = caught
            self.check()
        attempt = min(self.failures.get(name, 0) + 1, 6)
        self.failures[name] = attempt
        backoff = min(5 * 2 ** (attempt - 1), 300)
        log_result(
            name,
            "failed",
            self.store.ns,
            error=error,
            duration=self.io.monotonic() - started,
            backoff=backoff,
            counts=batch.counts if batch else None,
        )
        return backoff

    async def _loop(self, name, callback, interval):
        next_at = self.io.monotonic() + (interval if name == "refresh" else 0)
        wait = self.io.wait or wait_for_stop
        while not self.stop.is_set() and not self.fault.event.is_set():
            delay = max(0, next_at - self.io.monotonic())
            if delay:
                await wait(self.stop, delay)
            if self.stop.is_set() or self.fault.event.is_set():
                break
            try:
                backoff = await self.step(name, callback)
            except Exception:
                self.stop.set()
                break
            next_at = self.io.monotonic() + max(interval, backoff)

    async def run(self, once=False):
        if once:
            # One collection/search/refresh pass and one bounded processing pass each.
            # Auth refresh is first; all other failures remain independent.
            for name in ("at", "dm", "search", "events", "actions", "discovery"):
                if self.stop.is_set():
                    break
                if name in self.jobs():
                    await self.step(name, self.jobs()[name][0])
            if self.snapshot:
                await self.snapshot()
            if any(self.failures.values()):
                raise RuntimeError("one or more bounded passes failed")
            return
        self.tasks = [
            asyncio.create_task(self._loop(name, *job)) for name, job in self.jobs().items()
        ]
        if self.snapshot:
            self.tasks.append(
                asyncio.create_task(
                    self._loop("status", self.snapshot, self.settings.runtime.status_interval)
                )
            )
        stop_wait = asyncio.create_task(self.stop.wait())
        fault_wait = asyncio.create_task(self.fault.event.wait())
        try:
            await asyncio.wait([stop_wait, fault_wait], return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.stop.set()
            for waiter in (stop_wait, fault_wait):
                waiter.cancel()
            await asyncio.gather(stop_wait, fault_wait, return_exceptions=True)
            _, pending = await asyncio.wait(
                self.tasks, timeout=self.settings.runtime.shutdown_timeout
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.check()


async def run_bot(settings, *, once=False, io=None, stop=None, install_signals=True):
    io = io or RuntimeIO()
    stop = stop or asyncio.Event()
    with InstanceLock(settings.data_dir) as lock:
        settings = settings.model_copy(update={"data_dir": lock.directory})
        async with AsyncExitStack() as resources:
            # Validate providers before contacting Bilibili. No clients survive partial init.
            model = AIClient(settings, io.model)
            resources.push_async_callback(model.close)
            transcriber = None
            if settings.evidence.transcription_enabled:
                transcriber = TranscriptionClient(settings, io.transcription)
                resources.push_async_callback(transcriber.close)
            db_path = settings.data_dir / "state.db"
            descriptor = os.open(db_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
            store = Store(db_path, settings.namespace, clock=io.wall_clock)
            resources.push_async_callback(store.close)
            await store.open()
            for name in ("state.db", "state.db-wal", "state.db-shm"):
                path = settings.data_dir / name
                if path.exists():
                    os.chmod(path, 0o600)
            fault = AuthFault()
            transport = BiliTransport(
                settings,
                io.platform,
                read_wait=io.read_wait,
                write_wait=io.write_wait,
                auth_fault=fault,
            )
            resources.push_async_callback(transport.close)
            auth = AuthManager(
                settings,
                transport,
                CredentialFile(settings.data_dir / "auth.json"),
                store.bind_account,
            )
            platform = BilibiliClient(settings, transport, auth, store)
            downloader = Downloader(
                settings.platform.request_timeout,
                settings.evidence.max_download_mb * 1024 * 1024,
                io.download,
            )
            resources.push_async_callback(downloader.close)
            evidence = EvidenceService(settings, VideoAPI(platform), downloader, store, transcriber)
            resources.push_async_callback(evidence.close)
            scheduler = None
            alive, ready = True, False

            async def snapshot():
                counts = await store.counts()
                health, reasons = (
                    scheduler.health(counts, alive=alive, ready=ready)
                    if scheduler
                    else (
                        ("needs_login", ["authentication"])
                        if fault.event.is_set()
                        else ("starting" if alive else "stopped", ["initial_checks"])
                    )
                )
                write_private(
                    settings.data_dir / f"status-{store.ns}.json",
                    {
                        "mode": store.ns,
                        "updated_at": io.wall_clock(),
                        "stale_after": settings.runtime.status_interval * 3 + 5,
                        "alive": alive,
                        "ready": ready and not fault.event.is_set(),
                        "needs_login": fault.event.is_set(),
                        "auth_error": fault.kind.__name__ if fault.kind else None,
                        "counts": counts,
                        "business_health": health,
                        "degraded_reasons": reasons,
                        "last_attempt": scheduler.last_attempt if scheduler else {},
                        "batches": scheduler.batches if scheduler else {},
                        "last_success": scheduler.last_success if scheduler else {},
                        "job_failures": scheduler.failures if scheduler else {},
                        "ai": model.metrics,
                        "transcription": transcriber.metrics if transcriber else {},
                        "evidence": evidence.metrics,
                    },
                )

            loop = asyncio.get_running_loop()
            registered = []
            try:
                await snapshot()
                if install_signals:
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        loop.add_signal_handler(sig, stop.set)
                        registered.append(sig)
                # confirm_pending resumes; ambiguous phases require QR. Recover only once.
                await auth.refresh()
                await platform.verify_identity()
                await store.recover()
                ready = True
                scheduler = Scheduler(
                    settings,
                    store,
                    Collectors(settings, CollectionAPI(platform), store, clock=io.wall_clock),
                    VideoAPI(platform),
                    BusinessService(
                        settings,
                        store,
                        platform,
                        evidence,
                        SafetyService(settings, model),
                        AIService(settings, model),
                    ),
                    auth,
                    fault,
                    io=io,
                    stop=stop,
                    snapshot=snapshot,
                )
                scheduler.last_success["refresh"] = io.wall_clock()
                scheduler.last_attempt["refresh"] = io.wall_clock()
                await snapshot()
                await scheduler.run(once=once)
            finally:
                alive, ready = False, False
                for sig in registered:
                    loop.remove_signal_handler(sig)
                await snapshot()
                log_result("runtime", "stopped", store.ns)
