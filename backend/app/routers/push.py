"""Web Push (VAPID) — subscribe/unsubscribe + fan-out delivery.

A user opts in (Settings → Notifications). The browser sends its push
subscription here; we store one row per device. When a new group or family-room
message arrives, the routers call notify_group / notify_room, which push a
small payload to every *other* opted-in subscriber. The service worker in the
browser shows the notification and deep-links into the app on tap.

Delivery is best-effort: a dead endpoint (404/410) is pruned, any other error
is logged and skipped so one bad subscription never blocks the rest.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import app_origin, push_enabled, vapid_email, vapid_private_key, vapid_public_key
from ..db import get_db
from ..models import PushSubscription, User
from ..core.security import get_current_user

logger = logging.getLogger("chat.push")

router = APIRouter()


class SubscribeRequest(BaseModel):
    endpoint: str
    keys: dict[str, str]  # {"p256dh": "...", "auth": "..."}


def _subscription_info(sub: PushSubscription) -> dict:
    return {
        "endpoint": sub.endpoint,
        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
    }


# ---------------------------------------------------------------------------
# Subscription management
# ---------------------------------------------------------------------------
@router.get("/key")
async def get_key(user: User = Depends(get_current_user)):
    """Public VAPID key the browser needs to create a subscription."""
    if not push_enabled():
        raise HTTPException(status_code=503, detail="Push notifications are not configured")
    return {"public_key": vapid_public_key(), "origin": app_origin()}


@router.post("/subscribe", status_code=201)
async def subscribe(
    data: SubscribeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not push_enabled():
        raise HTTPException(status_code=503, detail="Push notifications are not configured")
    p256dh = (data.keys or {}).get("p256dh")
    auth = (data.keys or {}).get("auth")
    if not data.endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Invalid push subscription")

    existing = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == data.endpoint)
    ).scalar_one_or_none()
    if existing:
        # Same endpoint: refresh the keys + re-enable (idempotent re-subscribe).
        existing.p256dh = p256dh
        existing.auth = auth
        existing.enabled = True
        existing.user_id = user.id
        db.commit()
        return {"ok": True, "subscribed": True}

    db.add(
        PushSubscription(
            user_id=user.id,
            endpoint=data.endpoint,
            p256dh=p256dh,
            auth=auth,
            enabled=True,
        )
    )
    db.commit()
    logger.info("Push subscription added for user %s", user.id)
    return {"ok": True, "subscribed": True}


@router.delete("/subscribe")
async def unsubscribe(
    endpoint: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.execute(
        select(PushSubscription).where(
            PushSubscription.endpoint == endpoint,
            PushSubscription.user_id == user.id,
        )
    ).scalar_one_or_none()
    if row is None:
        return {"ok": True, "removed": False}
    db.delete(row)
    db.commit()
    return {"ok": True, "removed": True}


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
# Transient push-service failures worth retrying (timeouts surface as
# connection errors, which are retried too). Anything else 4xx is permanent.
_TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
_PUSH_ATTEMPTS = 3


def _deliver(info: dict[str, Any], user_id: int, payload: dict[str, Any]) -> str:
    """Send one push to one subscription (pure network — never touches the
    DB), retrying transient failures.

    Returns "ok" on delivery, "dead" for 404/410 (endpoint revoked/expired —
    the caller prunes the row) and "failed" for everything else, which is
    logged and skipped so one bad subscription never blocks the rest.
    """
    from pywebpush import WebPushException, webpush

    last_exc: Exception | None = None
    for attempt in range(_PUSH_ATTEMPTS):
        try:
            webpush(
                subscription_info=info,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=vapid_private_key(),
                vapid_claims={"sub": vapid_email()},
                timeout=5,
            )
            return "ok"
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                logger.info("Dead push endpoint for user %s (HTTP %s)", user_id, status)
                return "dead"
            last_exc = exc
            if status is not None and status not in _TRANSIENT_STATUSES:
                # A permanent 4xx (bad keys/payload): retrying cannot help.
                logger.warning("Push to user %s failed (HTTP %s): %s", user_id, status, exc)
                return "failed"
        except Exception as exc:  # noqa: BLE001 - never let push break message delivery
            last_exc = exc
        if attempt < _PUSH_ATTEMPTS - 1:
            time.sleep(0.5 * (attempt + 1))

    logger.warning(
        "Push to user %s failed after %d attempts: %s", user_id, _PUSH_ATTEMPTS, last_exc
    )
    return "failed"


def _push_batch(db: Session, subs: list[PushSubscription], payload: dict[str, Any]) -> int:
    """Push to a snapshot of subscriptions, keeping the DB out of the network
    phase: all reads finish (and the read transaction is committed) before
    the first HTTP call, and dead endpoints are pruned in one short write
    afterwards. A session — and with it an SQLite transaction — must never
    be held open across push-service round-trips (retries included)."""
    batch = [(s.id, s.user_id, _subscription_info(s)) for s in subs]
    db.commit()  # end the read transaction before going to the network
    results = [_deliver(info, user_id, payload) for _id, user_id, info in batch]
    dead = [sub_id for (sub_id, _u, _i), r in zip(batch, results) if r == "dead"]
    if dead:
        db.query(PushSubscription).filter(
            PushSubscription.id.in_(dead)
        ).delete(synchronize_session=False)
        db.commit()
        logger.info("Pruned %d dead push endpoint(s)", len(dead))
    return sum(1 for r in results if r == "ok")


def test_push(db: Session, user_id: int) -> list[dict[str, Any]]:
    """Diagnostic send for `manage.py test-push`: delivers a fixed test
    payload to every enabled subscription of a user and returns a per-device
    report. Dead endpoints are pruned and reported back as ``pruned`` so the
    CLI can tell the operator the device will self-heal on its next app
    open."""
    payload = {
        "title": "Community Chat — test notification",
        "body": "If you can see this on this device, push notifications work end to end.",
        "tag": f"test-push-{int(time.time())}",
        "icon": "/icon-192.png",
        "url": "/",
    }
    subs = list(
        db.execute(
            select(PushSubscription).where(
                PushSubscription.user_id == user_id,
                PushSubscription.enabled == True,  # noqa: E712
            )
        ).scalars().all()
    )
    batch = [(s.id, s.user_id, _subscription_info(s), s.endpoint) for s in subs]
    db.commit()  # end the read transaction before going to the network
    results = []
    for sub_id, sub_user_id, info, endpoint in batch:
        result = _deliver(info, sub_user_id, payload)
        if result == "dead":
            row = db.get(PushSubscription, sub_id)
            if row is not None:
                db.delete(row)
                db.commit()
        results.append(
            {"endpoint": endpoint, "delivered": result == "ok", "pruned": result == "dead"}
        )
    return results


def deliver_to_user(db: Session, user_id: int, payload: dict[str, Any]) -> int:
    """Push to all of a user's enabled subscriptions. Returns count delivered."""
    if not push_enabled():
        return 0
    subs = list(
        db.execute(
            select(PushSubscription).where(
                PushSubscription.user_id == user_id,
                PushSubscription.enabled == True,  # noqa: E712
            )
        ).scalars().all()
    )
    return _push_batch(db, subs, payload)


def notify_group(db: Session, sender_id: int, payload: dict[str, Any]) -> int:
    """Notify every active user except the sender. Best-effort fan-out."""
    if not push_enabled():
        return 0
    user_ids = {
        r[0]
        for r in db.execute(
            select(User.id).where(User.is_active == True)  # noqa: E712
        ).all()
    }
    db.commit()  # read phase done
    user_ids.discard(sender_id)
    return sum(deliver_to_user(db, uid, payload) for uid in user_ids)


def notify_room(db: Session, member_ids: set[int], sender_id: int, payload: dict[str, Any]) -> int:
    """Notify every member of a family room except the sender."""
    if not push_enabled():
        return 0
    ids = set(member_ids) - {sender_id}
    return sum(deliver_to_user(db, uid, payload) for uid in ids)


# ---------------------------------------------------------------------------
# Background task wrappers
# ---------------------------------------------------------------------------
# The routers fire these as fire-and-forget asyncio tasks right after the
# request commits. The DB phases (subscription snapshot, prune) run on the
# event-loop thread with short-lived sessions — sync DB code there executes
# atomically between awaits, so they never interleave with request sessions
# on the single shared SQLite connection. Only the blocking push-service
# HTTP (retries included) runs on a worker thread, and it holds no session.
import asyncio

from ..db import SessionLocal


def _subs_snapshot(db: Session, user_ids: set[int]) -> list[tuple[int, int, dict[str, Any]]]:
    """(sub_id, user_id, subscription_info) for every enabled subscription."""
    if not user_ids:
        return []
    subs = db.execute(
        select(PushSubscription).where(
            PushSubscription.user_id.in_(user_ids),
            PushSubscription.enabled == True,  # noqa: E712
        )
    ).scalars().all()
    return [(s.id, s.user_id, _subscription_info(s)) for s in subs]


def _collect_group_batch(sender_id: int) -> list[tuple[int, int, dict[str, Any]]]:
    db = SessionLocal()
    try:
        user_ids = {
            r[0]
            for r in db.execute(
                select(User.id).where(User.is_active == True)  # noqa: E712
            ).all()
        }
        user_ids.discard(sender_id)
        return _subs_snapshot(db, user_ids)
    finally:
        db.close()


def _collect_room_batch(member_ids: set[int], sender_id: int) -> list[tuple[int, int, dict[str, Any]]]:
    db = SessionLocal()
    try:
        return _subs_snapshot(db, set(member_ids) - {sender_id})
    finally:
        db.close()


def _deliver_all(batch: list[tuple[int, int, dict[str, Any]]], payload: dict[str, Any]) -> list[tuple[int, str]]:
    """Pure network phase (runs on a worker thread; no DB touched)."""
    return [(sub_id, _deliver(info, user_id, payload)) for sub_id, user_id, info in batch]


def _prune_dead(dead_ids: list[int]) -> None:
    if not dead_ids:
        return
    db = SessionLocal()
    try:
        db.query(PushSubscription).filter(
            PushSubscription.id.in_(dead_ids)
        ).delete(synchronize_session=False)
        db.commit()
        logger.info("Pruned %d dead push endpoint(s)", len(dead_ids))
    finally:
        db.close()


async def _fan_out(batch: list[tuple[int, int, dict[str, Any]]], payload: dict[str, Any]) -> int:
    if not batch:
        return 0
    results = await asyncio.to_thread(_deliver_all, batch, payload)
    _prune_dead([sub_id for sub_id, outcome in results if outcome == "dead"])
    return sum(1 for _id, outcome in results if outcome == "ok")


async def group_push_task(sender_id: int, payload: dict[str, Any]) -> None:
    if not push_enabled():
        return
    try:
        batch = _collect_group_batch(sender_id)
        await _fan_out(batch, payload)
    except Exception:  # noqa: BLE001
        logger.exception("group push fan-out failed")


async def room_push_task(
    member_ids: set[int], sender_id: int, payload: dict[str, Any]
) -> None:
    if not push_enabled():
        return
    try:
        batch = _collect_room_batch(member_ids, sender_id)
        await _fan_out(batch, payload)
    except Exception:  # noqa: BLE001
        logger.exception("room push fan-out failed")
