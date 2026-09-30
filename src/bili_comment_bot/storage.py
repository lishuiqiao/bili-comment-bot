"""Transactional inbox, outbox and rolling reply quota, isolated by run mode."""

import asyncio
import json
import time
from collections.abc import Callable, Collection, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from .domain import ActionKind, ActionStatus, Channel, MessageEvent, PublishAction

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
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA busy_timeout=10000")
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=FULL")
        await self.db.executescript(SCHEMA)
        return self

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None

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

    async def cursor(self, stream: str) -> str | None:
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT value FROM cursors WHERE ns=? AND stream=?", (self.ns, stream)
                )
            ).fetchone()
            return row[0] if row else None

    async def pending_events(self, limit: int = 100) -> list[MessageEvent]:
        async with self.transaction() as db:
            rows = await (
                await db.execute(
                    "SELECT payload FROM inbox WHERE ns=? AND status='pending' "
                    "ORDER BY created,id LIMIT ?",
                    (self.ns, limit),
                )
            ).fetchall()
            return [MessageEvent.model_validate_json(row[0]) for row in rows]

    async def claim_event(self, event_id: str) -> bool:
        async with self.transaction() as db:
            result = await db.execute(
                "UPDATE inbox SET status='processing' WHERE ns=? AND id=? AND status='pending'",
                (self.ns, event_id),
            )
            return result.rowcount == 1

    async def finish_event(self, event_id: str, status: str = "done"):
        if status not in {"done", "pending", "blocked", "ignored"}:
            raise ValueError("invalid inbox state")
        async with self.transaction() as db:
            await db.execute(
                "UPDATE inbox SET status=? WHERE ns=? AND id=?", (status, self.ns, event_id)
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
        async with self.transaction() as db:
            rows = await (
                await db.execute(
                    "SELECT payload FROM actions WHERE ns=? AND status='pending' "
                    "ORDER BY updated,id LIMIT ?",
                    (self.ns, limit),
                )
            ).fetchall()
            return [PublishAction.model_validate_json(row[0]) for row in rows]

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

    async def resolve_uncertain(self, action_id: str, remote_id: str, evidence: str):
        """Operator supplies verified remote receipt; this API never blindly resends."""
        if not remote_id.strip() or not evidence.strip():
            raise ValueError("a verified receipt and audit explanation are required")
        async with self.transaction() as db:
            result = await db.execute(
                "UPDATE actions SET status='succeeded',remote_id=?,reason=?,updated=? "
                "WHERE ns=? AND id=? AND status='uncertain'",
                (remote_id, evidence, self.clock(), self.ns, action_id),
            )
            if result.rowcount != 1:
                raise ValueError("action is not uncertain")
            await db.execute(
                "UPDATE quota SET state='sent',sent_at=? WHERE ns=? AND action_id=?",
                (self.clock(), self.ns, action_id),
            )
            await db.execute(
                "INSERT INTO audit(ns,action_id,state,reason,created) VALUES(?,?,?,?,?)",
                (self.ns, action_id, "succeeded", evidence, self.clock()),
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
            await db.execute(
                "INSERT INTO cache VALUES(?,?,?,?) ON CONFLICT(ns,key) "
                "DO UPDATE SET payload=excluded.payload,expires=excluded.expires",
                (self.ns, key, json.dumps(payload), self.clock() + ttl),
            )
