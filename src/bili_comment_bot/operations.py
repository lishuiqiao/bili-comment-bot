"""Explicit namespace, exclusive offline operator reconciliation."""

from .domain import LikeStateEvidence
from .instance_lock import InstanceLock
from .storage import Store


async def operate(
    settings,
    namespace,
    command,
    *,
    action_id=None,
    remote_id=None,
    note="",
    account_uid=None,
    aid=None,
    liked=False,
    uncertain=False,
    limit=100,
):
    if namespace not in {"sim", "live"}:
        raise ValueError("explicit sim/live namespace required")
    if not 1 <= limit <= 100:
        raise ValueError("bounded listing limit required")
    with InstanceLock(settings.data_dir) as lock:
        store = await Store(lock.directory / "state.db", namespace).open()
        try:
            if command == "actions":
                return {
                    "namespace": namespace,
                    "actions": await store.action_summaries(limit, uncertain=uncertain),
                }
            if not action_id:
                raise ValueError("action ID required")
            if command == "cancel-action":
                return {"namespace": namespace, "cancelled": await store.cancel_pending(action_id)}
            if command != "verify-action":
                raise ValueError("invalid operation")
            proof = None
            if account_uid is not None or aid is not None or liked:
                proof = LikeStateEvidence(account_uid=account_uid, aid=aid, liked=liked)
            await store.resolve_uncertain(action_id, remote_id, note, like_state=proof)
            return {"namespace": namespace, "id": action_id, "status": "succeeded"}
        finally:
            await store.close()
