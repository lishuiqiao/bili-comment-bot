"""Transactional inbox, outbox and rolling reply quota, isolated by run mode."""

import asyncio
import json
import re
import time
from collections.abc import Callable, Collection, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from .domain import (
    ActionKind,
    ActionStatus,
    Channel,
    LikeStateEvidence,
    MessageEvent,
    PublishAction,
)
from .work import DiscoveryWorkflow, Selection

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES (1);
CREATE TABLE IF NOT EXISTS inbox(
 ns TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL,
 PRIMARY KEY(ns,id));
CREATE TABLE IF NOT EXISTS cursors(
 ns TEXT NOT NULL, stream TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(ns,stream));
CREATE TABLE IF NOT EXISTS actions(
 ns TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, dependency TEXT,
 status TEXT NOT NULL DEFAULT 'pending', remote_id TEXT, reason TEXT,
 updated REAL NOT NULL, PRIMARY KEY(ns,id));
CREATE TABLE IF NOT EXISTS quota(
 ns TEXT NOT NULL, action_id TEXT NOT NULL, uid INTEGER NOT NULL, channel TEXT NOT NULL,
 state TEXT NOT NULL, reserved_at REAL NOT NULL, sent_at REAL,
 PRIMARY KEY(ns,action_id));
CREATE INDEX IF NOT EXISTS quota_user ON quota(ns,uid,channel,state,sent_at);
CREATE TABLE IF NOT EXISTS audit(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, ns TEXT NOT NULL, action_id TEXT NOT NULL,
 state TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cache(
 ns TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL,
 PRIMARY KEY(ns,key));
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO schema_version VALUES (2);
CREATE TABLE IF NOT EXISTS collection_jobs(
 ns TEXT NOT NULL, uid INTEGER NOT NULL, target INTEGER NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(ns,uid));
INSERT OR IGNORE INTO schema_version VALUES (3);
CREATE TABLE IF NOT EXISTS workflows(
 ns TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(ns,id));
CREATE TABLE IF NOT EXISTS work_retries(
 ns TEXT NOT NULL, id TEXT NOT NULL, attempts INTEGER NOT NULL, next_at REAL NOT NULL,
 PRIMARY KEY(ns,id));
INSERT OR IGNORE INTO schema_version VALUES (4);
CREATE TABLE IF NOT EXISTS discovery_jobs(
 ns TEXT NOT NULL, aid INTEGER NOT NULL, next_at REAL NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(ns,aid));
CREATE INDEX IF NOT EXISTS discovery_due ON discovery_jobs(ns,next_at,created);
CREATE TABLE IF NOT EXISTS workflow_states(
 ns TEXT NOT NULL, id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 PRIMARY KEY(ns,id));
INSERT OR IGNORE INTO workflow_states(ns,id) SELECT ns,id FROM workflows;
CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(ns,status,created,id);
CREATE INDEX IF NOT EXISTS actions_pending ON actions(ns,status,updated,id);
INSERT OR IGNORE INTO schema_version VALUES (5);
CREATE TABLE IF NOT EXISTS quarantined(
 ns TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
 reason TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(ns,kind,id));
INSERT OR IGNORE INTO schema_version VALUES (6);
CREATE INDEX IF NOT EXISTS cache_expiry ON cache(ns,expires,key);
INSERT OR IGNORE INTO schema_version VALUES (7);
"""


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    status: ActionStatus
    reason: str = ""


class Store:
    def __init__(
        self, path: Path | str, namespace: str = "live", clock: Callable[[], float] = time.time
    ):
        self.path = path
        self.ns = namespace
        self.clock = clock
        self._lock = asyncio.Lock()
        self.db: aiosqlite.Connection | None = None

    async def open(self):
        self.db = await aiosqlite.connect(self.path, isolation_level=None)
        try:
            self.db.row_factory = aiosqlite.Row
            await self.db.execute("PRAGMA busy_timeout=10000")
            await self.db.execute("PRAGMA journal_mode=WAL")
            await self.db.execute("PRAGMA synchronous=FULL")
            await self.db.executescript(SCHEMA)
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None

    async def bind_account(self, uid: int):
        if type(uid) is not int or uid <= 0:
            raise ValueError("verified account UID must be positive")
        async with self.transaction() as db:
            await db.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES('account_uid',?)", (str(uid),)
            )
            row = await (
                await db.execute("SELECT value FROM metadata WHERE key='account_uid'")
            ).fetchone()
            if row[0] != str(uid):
                raise ValueError("state belongs to another account; use a separate data directory")

    @asynccontextmanager
    async def transaction(self):
        if self.db is None:
            raise RuntimeError("store is not open")
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                await self.db.rollback()
                raise
            else:
                await self.db.commit()

    async def enqueue_batch(self, events: Iterable[MessageEvent], stream: str, cursor: str):
        await self.save_collection(events, stream, cursor)

    async def save_collection(
        self,
        events: Iterable[MessageEvent],
        stream: str,
        cursor: str,
        *,
        jobs: Iterable[tuple[int, int]] = (),
        completed_job: tuple[int, int] | None = None,
        touched_job: int | None = None,
        ignored: dict[str, int] | None = None,
    ):
        """Page, resume point and newly discovered tasks commit together; no network here."""
        async with self.transaction() as db:
            for event in events:
                await db.execute(
                    "INSERT OR IGNORE INTO inbox(ns,id,payload,created) VALUES(?,?,?,?)",
                    (self.ns, event.id, event.model_dump_json(), self.clock()),
                )
            await db.execute(
                "INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(ns,stream) "
                "DO UPDATE SET value=excluded.value",
                (self.ns, stream, cursor),
            )
            for uid, target in jobs:
                await db.execute(
                    "INSERT INTO collection_jobs SELECT ?,?,?,COALESCE(MAX(created),0)+1 "
                    "FROM collection_jobs WHERE ns=? ON CONFLICT(ns,uid) "
                    "DO UPDATE SET target=MAX(collection_jobs.target,excluded.target)",
                    (self.ns, uid, target, self.ns),
                )
            if completed_job:
                uid, covered = completed_job
                await db.execute(
                    "DELETE FROM collection_jobs WHERE ns=? AND uid=? AND target<=?",
                    (self.ns, uid, covered),
                )
            if touched_job is not None:
                await db.execute(
                    "UPDATE collection_jobs SET created=(SELECT COALESCE(MAX(created),0)+1 "
                    "FROM collection_jobs WHERE ns=?) WHERE ns=? AND uid=?",
                    (self.ns, self.ns, touched_job),
                )
            if ignored:
                await db.execute(
                    "INSERT INTO audit(ns,action_id,state,reason,created) VALUES(?,?,?,?,?)",
                    (self.ns, "collection:" + stream, "ignored", json.dumps(ignored), self.clock()),
                )

    async def dm_jobs(self, limit: int = 100) -> list[tuple[int, int]]:
        async with self.transaction() as db:
            rows = await (
                await db.execute(
                    "SELECT uid,target FROM collection_jobs WHERE ns=? "
                    "ORDER BY created,uid LIMIT ?",
                    (self.ns, limit),
                )
            ).fetchall()
            return [(row[0], row[1]) for row in rows]

    async def cursor(self, stream: str) -> str | None:
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT value FROM cursors WHERE ns=? AND stream=?", (self.ns, stream)
                )
            ).fetchone()
            return row[0] if row else None

    async def _validate_selection(self, db, rows, kind, decode):
        items, invalid = [], 0
        for row in rows:
            try:
                items.append(decode(row))
            except (ValueError, TypeError, KeyError, RecursionError):
                invalid += 1
                await db.execute(
                    "INSERT OR IGNORE INTO quarantined VALUES(?,?,?,?,?)",
                    (self.ns, kind, row["id"], "invalid_payload", self.clock()),
                )
                await db.execute(
                    "INSERT INTO audit(ns,action_id,state,reason,created) VALUES(?,?,?,?,?)",
                    (self.ns, row["id"], "quarantined", "invalid_payload", self.clock()),
                )
        return Selection(items, invalid)

    async def pending_events(self, limit: int = 100) -> list[MessageEvent]:
        return (await self.event_batch(limit)).items

    async def event_batch(self, limit: int) -> Selection:
        def decode(row):
            event = MessageEvent.model_validate_json(row["payload"])
            if event.id != row["id"]:
                raise ValueError("event binding mismatch")
            return event

        async with self.transaction() as db:
            rows = await (
                await db.execute(
                    "SELECT inbox.id,inbox.payload FROM inbox LEFT JOIN work_retries r "
                    "ON inbox.ns=r.ns AND inbox.id=r.id WHERE inbox.ns=? AND status='pending' "
                    "AND COALESCE(r.next_at,0)<=? AND NOT EXISTS(SELECT 1 FROM quarantined q "
                    "WHERE q.ns=inbox.ns AND q.kind='events' AND q.id=inbox.id) "
                    "ORDER BY inbox.created,inbox.id LIMIT ?",
                    (self.ns, self.clock(), limit),
                )
            ).fetchall()
            return await self._validate_selection(db, rows, "events", decode)

    async def event(self, event_id: str) -> MessageEvent | None:
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT payload FROM inbox WHERE ns=? AND id=?", (self.ns, event_id)
                )
            ).fetchone()
            return MessageEvent.model_validate_json(row[0]) if row else None

    async def claim_event(self, event_id: str) -> bool:
        async with self.transaction() as db:
            result = await db.execute(
                "UPDATE inbox SET status='processing' WHERE ns=? AND id=? AND status='pending' "
                "AND NOT EXISTS (SELECT 1 FROM work_retries r WHERE r.ns=inbox.ns "
                "AND r.id=inbox.id AND r.next_at>?)",
                (self.ns, event_id, self.clock()),
            )
            return result.rowcount == 1

    async def ready_work(self, key: str) -> bool:
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT next_at FROM work_retries WHERE ns=? AND id=?", (self.ns, key)
                )
            ).fetchone()
            return not row or row[0] <= self.clock()

    async def defer_work(self, key: str, delay: float = 30, *, event=False):
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT attempts FROM work_retries WHERE ns=? AND id=?", (self.ns, key)
                )
            ).fetchone()
            attempts = min((row[0] if row else 0) + 1, 1000000)
            next_at = self.clock() + min(delay * 2 ** min(attempts - 1, 5), 1800)
            await db.execute(
                "INSERT INTO work_retries VALUES(?,?,?,?) ON CONFLICT(ns,id) "
                "DO UPDATE SET attempts=excluded.attempts,next_at=excluded.next_at",
                (self.ns, key, attempts, next_at),
            )
            if event:
                await db.execute(
                    "UPDATE inbox SET status='pending' WHERE ns=? AND id=? AND status='processing'",
                    (self.ns, key),
                )

    async def complete_event(self, event_id: str, actions: Iterable[PublishAction]):
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT status FROM inbox WHERE ns=? AND id=?", (self.ns, event_id)
                )
            ).fetchone()
            if not row or row[0] != "processing":
                raise ValueError("event must be claimed")
            for action in actions:
                await db.execute(
                    "INSERT OR IGNORE INTO actions(ns,id,payload,dependency,updated) "
                    "VALUES(?,?,?,?,?)",
                    (self.ns, action.id, action.model_dump_json(), action.dependency, self.clock()),
                )
            await db.execute(
                "UPDATE inbox SET status='done' WHERE ns=? AND id=?", (self.ns, event_id)
            )
            await db.execute("DELETE FROM work_retries WHERE ns=? AND id=?", (self.ns, event_id))

    async def put_workflow(self, key: str, payload: str):
        async with self.transaction() as db:
            await db.execute(
                "INSERT OR IGNORE INTO workflows VALUES(?,?,?)", (self.ns, key, payload)
            )
            await db.execute(
                "INSERT OR IGNORE INTO workflow_states(ns,id) VALUES(?,?)", (self.ns, key)
            )
            await db.execute(
                "DELETE FROM discovery_jobs WHERE ns=? AND 'discovery:'||aid=?", (self.ns, key)
            )

    async def enqueue_candidates(self, aids: Iterable[int]):
        async with self.transaction() as db:
            for aid in aids:
                if type(aid) is not int or aid <= 0:
                    raise ValueError("candidate aid must be positive")
                await db.execute(
                    "INSERT OR IGNORE INTO discovery_jobs VALUES(?,?,?,?)",
                    (self.ns, aid, self.clock(), self.clock()),
                )

    async def due_candidates(self, limit: int):
        async with self.transaction() as db:
            rows = await (
                await db.execute(
                    "SELECT j.aid FROM discovery_jobs j LEFT JOIN work_retries r "
                    "ON r.ns=j.ns AND r.id='discovery:'||j.aid WHERE j.ns=? AND j.next_at<=? "
                    "AND COALESCE(r.next_at,0)<=? AND NOT EXISTS(SELECT 1 FROM workflows w "
                    "WHERE w.ns=j.ns AND w.id='discovery:'||j.aid) "
                    "ORDER BY j.next_at,j.created,j.aid LIMIT ?",
                    (self.ns, self.clock(), self.clock(), limit),
                )
            ).fetchall()
            return [row[0] for row in rows]

    async def due_workflows(self, limit: int):
        return (await self.workflow_batch(limit)).items

    async def workflow_batch(self, limit: int) -> Selection:
        def decode(row):
            flow = DiscoveryWorkflow.model_validate_json(row["payload"])
            if row["id"] != f"discovery:{flow.aid}":
                raise ValueError("workflow binding mismatch")
            return flow.aid

        async with self.transaction() as db:
            rows = await (
                await db.execute(
                    "SELECT w.id,w.payload FROM workflows w JOIN workflow_states s "
                    "ON w.ns=s.ns AND w.id=s.id "
                    "LEFT JOIN work_retries r ON w.ns=r.ns AND w.id=r.id "
                    "WHERE w.ns=? AND s.state!='done' AND COALESCE(r.next_at,0)<=? "
                    "AND NOT EXISTS(SELECT 1 FROM quarantined q WHERE q.ns=w.ns "
                    "AND q.kind='discovery' AND q.id=w.id) "
                    "AND NOT EXISTS(SELECT 1 FROM actions a WHERE a.ns=w.ns "
                    "AND a.id LIKE w.id||':%' "
                    "AND a.status IN ('uncertain','in_flight','failed','blocked')) "
                    "AND NOT EXISTS(SELECT 1 FROM actions a JOIN work_retries ar "
                    "ON a.ns=ar.ns AND ar.id='action:'||a.id WHERE a.ns=w.ns "
                    "AND a.id LIKE w.id||':%' AND a.status='pending' AND ar.next_at>?) "
                    "ORDER BY COALESCE(r.next_at,0),w.id LIMIT ?",
                    (self.ns, self.clock(), self.clock(), limit),
                )
            ).fetchall()
            return await self._validate_selection(db, rows, "discovery", decode)

    async def discovery_state(self, key: str, state: str):
        if state not in {"pending", "paused", "done"}:
            raise ValueError("invalid workflow state")
        async with self.transaction() as db:
            await db.execute(
                "UPDATE workflow_states SET state=? WHERE ns=? AND id=?", (state, self.ns, key)
            )
            await db.execute("DELETE FROM work_retries WHERE ns=? AND id=?", (self.ns, key))

    async def reevaluate_candidate(self, aid: int, interval: float):
        async with self.transaction() as db:
            await db.execute(
                "UPDATE discovery_jobs SET next_at=? WHERE ns=? AND aid=?",
                (self.clock() + interval, self.ns, aid),
            )
            await db.execute(
                "DELETE FROM work_retries WHERE ns=? AND id=?", (self.ns, f"discovery:{aid}")
            )

    async def workflow(self, key: str) -> str | None:
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT payload FROM workflows WHERE ns=? AND id=?", (self.ns, key)
                )
            ).fetchone()
            return row[0] if row else None

    async def quota_available(
        self, uid: int, channel: Channel, limit: int, whitelist: Collection[int]
    ) -> bool:
        if uid in whitelist:
            return True
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM quota WHERE ns=? AND uid=? AND channel=? AND "
                    "(state IN ('reserved','uncertain') OR "
                    "(state='sent' AND sent_at>? AND sent_at<=?))",
                    (self.ns, uid, channel, self.clock() - 3600, self.clock()),
                )
            ).fetchone()
            return row[0] < limit

    async def finish_event(self, event_id: str, status: str = "done"):
        if status not in {"done", "pending", "blocked", "ignored"}:
            raise ValueError("invalid inbox state")
        async with self.transaction() as db:
            await db.execute(
                "UPDATE inbox SET status=? WHERE ns=? AND id=?", (status, self.ns, event_id)
            )
            if status in {"done", "blocked", "ignored"}:
                await db.execute(
                    "DELETE FROM work_retries WHERE ns=? AND id=?", (self.ns, event_id)
                )

    async def put_actions(self, actions: Iterable[PublishAction]):
        async with self.transaction() as db:
            for action in actions:
                await db.execute(
                    "INSERT OR IGNORE INTO actions(ns,id,payload,dependency,updated) "
                    "VALUES(?,?,?,?,?)",
                    (self.ns, action.id, action.model_dump_json(), action.dependency, self.clock()),
                )

    async def action(self, action_id: str) -> dict | None:
        async with self.transaction() as db:
            row = await (
                await db.execute("SELECT * FROM actions WHERE ns=? AND id=?", (self.ns, action_id))
            ).fetchone()
            return dict(row) if row else None

    async def pending_actions(self, limit: int = 100) -> list[PublishAction]:
        return (await self.action_batch(limit)).items

    async def action_batch(self, limit: int) -> Selection:
        def decode(row):
            action = PublishAction.model_validate_json(row["payload"])
            if action.id != row["id"] or action.dependency != row["dependency"]:
                raise ValueError("action binding mismatch")
            return action

        async with self.transaction() as db:
            rows = await (
                await db.execute(
                    "SELECT a.id,a.payload,a.dependency FROM actions a LEFT JOIN work_retries r "
                    "ON a.ns=r.ns AND r.id='action:'||a.id WHERE a.ns=? AND a.status='pending' "
                    "AND COALESCE(r.next_at,0)<=? AND NOT EXISTS(SELECT 1 FROM quarantined q "
                    "WHERE q.ns=a.ns AND q.kind='actions' AND q.id=a.id) "
                    "AND (a.dependency IS NULL OR EXISTS(SELECT 1 FROM actions d "
                    "WHERE d.ns=a.ns AND d.id=a.dependency AND (d.status='succeeded' OR "
                    "(d.ns='sim' AND d.status='simulated')))) ORDER BY a.updated,a.id LIMIT ?",
                    (self.ns, self.clock(), limit),
                )
            ).fetchall()
            return await self._validate_selection(db, rows, "actions", decode)

    async def clear_work(self, key: str):
        async with self.transaction() as db:
            await db.execute("DELETE FROM work_retries WHERE ns=? AND id=?", (self.ns, key))

    async def claim_action(
        self,
        action_id: str,
        *,
        dm_limit: int = 5,
        comment_limit: int = 5,
        whitelist: Collection[int] = (),
    ) -> ClaimResult:
        """Claim and reserve together; quota identity comes only from the stored action."""
        if dm_limit < 0 or comment_limit < 0:
            raise ValueError("quota limits cannot be negative")
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT payload,dependency,status FROM actions WHERE ns=? AND id=?",
                    (self.ns, action_id),
                )
            ).fetchone()
            if not row:
                raise ValueError("action does not exist")
            if row[2] != ActionStatus.PENDING:
                return ClaimResult(False, ActionStatus(row[2]), "already handled")
            retry = await (
                await db.execute(
                    "SELECT next_at FROM work_retries WHERE ns=? AND id=?",
                    (self.ns, "action:" + action_id),
                )
            ).fetchone()
            if retry and retry[0] > self.clock():
                return ClaimResult(False, ActionStatus.PENDING, "waiting for retry")
            if row[1]:
                dependency = await (
                    await db.execute(
                        "SELECT status FROM actions WHERE ns=? AND id=?", (self.ns, row[1])
                    )
                ).fetchone()
                good = {ActionStatus.SUCCEEDED}
                if self.ns == "sim":
                    good.add(ActionStatus.SIMULATED)
                if not dependency or dependency[0] not in good:
                    return ClaimResult(False, ActionStatus.PENDING, "waiting for dependency")
            action = PublishAction.model_validate_json(row[0])
            now = self.clock()
            if action.kind == ActionKind.REPLY:
                if action.uid is None or action.channel is None:
                    raise ValueError("reply quota identity missing")
                if action.uid not in whitelist:
                    limit = dm_limit if action.channel == Channel.DM else comment_limit
                    existing = await (
                        await db.execute(
                            "SELECT uid,channel,state FROM quota WHERE ns=? AND action_id=?",
                            (self.ns, action_id),
                        )
                    ).fetchone()
                    if existing and (existing[0] != action.uid or existing[1] != action.channel):
                        raise RuntimeError("stored quota identity conflicts with action")
                    # Legacy pending reservations remain attached to their own action.
                    if not existing or existing[2] == "released":
                        count = await (
                            await db.execute(
                                "SELECT COUNT(*) FROM quota WHERE ns=? AND uid=? AND channel=? "
                                "AND (state IN ('reserved','uncertain') OR "
                                "(state='sent' AND sent_at>?))",
                                (self.ns, action.uid, action.channel, now - 3600),
                            )
                        ).fetchone()
                        if count[0] >= limit:
                            await self._block_pending(db, action_id, "quota")
                            return ClaimResult(False, ActionStatus.BLOCKED, "quota")
                        await db.execute(
                            "INSERT INTO quota VALUES(?,?,?,?,?,?,NULL) "
                            "ON CONFLICT(ns,action_id) DO UPDATE SET state='reserved', "
                            "reserved_at=excluded.reserved_at,sent_at=NULL",
                            (self.ns, action_id, action.uid, action.channel, "reserved", now),
                        )
                    elif existing[2] != "reserved":
                        raise RuntimeError("pending action has a terminal quota record")
            await db.execute(
                "UPDATE actions SET status='in_flight',updated=? WHERE ns=? AND id=?",
                (now, self.ns, action_id),
            )
            return ClaimResult(True, ActionStatus.IN_FLIGHT)

    async def _block_pending(self, db, action_id: str, reason: str) -> bool:
        now = self.clock()
        result = await db.execute(
            "UPDATE actions SET status='blocked',reason=?,updated=? "
            "WHERE ns=? AND id=? AND status='pending'",
            (reason, now, self.ns, action_id),
        )
        if result.rowcount != 1:
            return False
        await db.execute(
            "UPDATE quota SET state='released' WHERE ns=? AND action_id=? AND state='reserved'",
            (self.ns, action_id),
        )
        await db.execute(
            "INSERT INTO audit(ns,action_id,state,reason,created) VALUES(?,?,?,?,?)",
            (self.ns, action_id, ActionStatus.BLOCKED, reason, now),
        )
        await db.execute(
            "DELETE FROM work_retries WHERE ns=? AND id=?", (self.ns, "action:" + action_id)
        )
        return True

    async def block_pending(self, action_id: str, reason: str) -> ActionStatus:
        """A late safety result must not overwrite another worker's claimed action."""
        async with self.transaction() as db:
            await self._block_pending(db, action_id, reason)
            row = await (
                await db.execute(
                    "SELECT status FROM actions WHERE ns=? AND id=?", (self.ns, action_id)
                )
            ).fetchone()
            if not row:
                raise ValueError("action does not exist")
            return ActionStatus(row[0])

    async def cancel_pending(self, action_id: str) -> bool:
        """Cancel only before claim. In-flight/uncertain/sent reservations cannot be freed."""
        async with self.transaction() as db:
            return await self._block_pending(db, action_id, "cancelled before claim")

    async def finish_action(
        self, action_id: str, status: ActionStatus, remote_id: str | None = None, reason: str = ""
    ):
        if status not in {
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.UNCERTAIN,
            ActionStatus.SIMULATED,
        }:
            raise ValueError("invalid terminal action state")
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT status FROM actions WHERE ns=? AND id=?", (self.ns, action_id)
                )
            ).fetchone()
            if not row or row[0] != ActionStatus.IN_FLIGHT:
                raise RuntimeError("action must be claimed before completion")
            now = self.clock()
            await db.execute(
                "UPDATE actions SET status=?,remote_id=?,reason=?,updated=? WHERE ns=? AND id=?",
                (status, remote_id, reason, now, self.ns, action_id),
            )
            await db.execute(
                "DELETE FROM work_retries WHERE ns=? AND id=?", (self.ns, "action:" + action_id)
            )
            quota_state = {
                ActionStatus.SUCCEEDED: "sent",
                ActionStatus.SIMULATED: "sent",
                ActionStatus.FAILED: "released",
                ActionStatus.UNCERTAIN: "uncertain",
            }[status]
            await db.execute(
                "UPDATE quota SET state=?,sent_at=? WHERE ns=? AND action_id=?",
                (quota_state, now, self.ns, action_id),
            )
            await db.execute(
                "INSERT INTO audit(ns,action_id,state,reason,created) VALUES(?,?,?,?,?)",
                (self.ns, action_id, status, reason, now),
            )

    async def recover(self):
        """Call once before starting the single service instance, never during active writes."""
        async with self.transaction() as db:
            await db.execute(
                "UPDATE quota SET state='uncertain' WHERE ns=? AND action_id IN "
                "(SELECT id FROM actions WHERE ns=? AND status='in_flight')",
                (self.ns, self.ns),
            )
            await db.execute(
                "UPDATE actions SET status='uncertain',reason='startup recovery', "
                "updated=? WHERE ns=? AND status='in_flight'",
                (self.clock(), self.ns),
            )
            await db.execute(
                "UPDATE inbox SET status='pending' WHERE ns=? AND status='processing'", (self.ns,)
            )

    async def resolve_uncertain(
        self,
        action_id: str,
        remote_id: str | None,
        evidence: str,
        *,
        like_state: LikeStateEvidence | None = None,
    ):
        """Explicit operator verification, by stored action kind. Never sends a request."""
        if not evidence.strip() or len(evidence) > 1000:
            raise ValueError("a bounded audit explanation is required")
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT payload,status FROM actions WHERE ns=? AND id=?", (self.ns, action_id)
                )
            ).fetchone()
            if not row or row[1] != ActionStatus.UNCERTAIN:
                raise ValueError("action is not uncertain")
            action = PublishAction.model_validate_json(row[0])
            if action.kind == ActionKind.LIKE:
                account = await (
                    await db.execute("SELECT value FROM metadata WHERE key='account_uid'")
                ).fetchone()
                if (
                    remote_id is not None
                    or not isinstance(like_state, LikeStateEvidence)
                    or not like_state.liked
                    or action.aid != like_state.aid
                    or not account
                    or account[0] != str(like_state.account_uid)
                ):
                    raise ValueError(
                        "like evidence must match bound account and target liked state"
                    )
                reason = json.dumps(
                    {
                        "verification": "manual_target_state_check",
                        **like_state.model_dump(),
                        "note": evidence.strip(),
                    },
                    ensure_ascii=False,
                )
            else:
                if (
                    like_state is not None
                    or not isinstance(remote_id, str)
                    or not re.fullmatch(r"[1-9][0-9]{0,31}", remote_id)
                ):
                    raise ValueError("comment/DM recovery requires a verified numeric remote ID")
                reason = evidence.strip()
            now = self.clock()
            result = await db.execute(
                "UPDATE actions SET status='succeeded',remote_id=?,reason=?,updated=? "
                "WHERE ns=? AND id=? AND status='uncertain'",
                (remote_id, reason, now, self.ns, action_id),
            )
            if result.rowcount != 1:
                raise ValueError("action is not uncertain")
            await db.execute(
                "UPDATE quota SET state='sent',sent_at=? WHERE ns=? AND action_id=?",
                (now, self.ns, action_id),
            )
            await db.execute(
                "INSERT INTO audit(ns,action_id,state,reason,created) VALUES(?,?,?,?,?)",
                (self.ns, action_id, "succeeded", reason, now),
            )

    async def cache_get(self, key: str) -> dict | None:
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT payload FROM cache WHERE ns=? AND key=? AND expires>?",
                    (self.ns, key, self.clock()),
                )
            ).fetchone()
            return json.loads(row[0]) if row else None

    async def cache_put(self, key: str, payload: dict, ttl: int):
        async with self.transaction() as db:
            now = self.clock()
            # Bounded housekeeping; never touch another mode or durable action history.
            await db.execute(
                "DELETE FROM cache WHERE ns=? AND key IN ("
                "SELECT key FROM cache WHERE ns=? AND expires<=? ORDER BY expires,key LIMIT 100)",
                (self.ns, self.ns, now),
            )
            await db.execute(
                "INSERT INTO cache VALUES(?,?,?,?) ON CONFLICT(ns,key) "
                "DO UPDATE SET payload=excluded.payload,expires=excluded.expires",
                (self.ns, key, json.dumps(payload), now + ttl),
            )

    async def counts(self) -> dict:
        async with self.transaction() as db:
            result = {}
            for table, column in (
                ("inbox", "status"),
                ("actions", "status"),
                ("workflow_states", "state"),
            ):
                rows = await (
                    await db.execute(
                        f"SELECT {column},COUNT(*) FROM {table} WHERE ns=? GROUP BY {column}",
                        (self.ns,),
                    )
                ).fetchall()
                result[table] = {row[0]: row[1] for row in rows}
            for table in ("work_retries", "discovery_jobs", "collection_jobs", "quarantined"):
                result[table] = (
                    await (
                        await db.execute(f"SELECT COUNT(*) FROM {table} WHERE ns=?", (self.ns,))
                    ).fetchone()
                )[0]
            return result

    async def action_summaries(self, limit: int = 100, *, uncertain: bool = False) -> list[dict]:
        async with self.transaction() as db:
            rows = await (
                await db.execute(
                    "SELECT id,status,remote_id,payload FROM actions WHERE ns=? "
                    "AND (?=0 OR status='uncertain') ORDER BY updated,id LIMIT ?",
                    (self.ns, int(uncertain), limit),
                )
            ).fetchall()
            return [
                {
                    "id": row[0],
                    "status": row[1],
                    "remote_id": row[2],
                    "kind": self._summary_kind(row[3]),
                }
                for row in rows
            ]

    @staticmethod
    def _summary_kind(payload):
        try:
            return PublishAction.model_validate_json(payload).kind.value
        except (ValueError, RecursionError):
            return "invalid"
