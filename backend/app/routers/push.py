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


def _send(db: Session, sub: PushSubscription, payload: dict[str, Any]) -> bool:
    """Send one push to one subscription, retrying transient failures.

    Returns True on delivery. 404/410 (endpoint revoked/expired) prunes the
    row immediately. Transient failures (connection errors, timeouts, 429,
    5xx) get a couple of retries with short backoff — one blip from the push
    service should not silently lose a notification. Any other error is
    logged and skipped so one bad subscription never blocks the rest.
    """
    from pywebpush import WebPushException, webpush

    last_exc: Exception | None = None
    for attempt in range(_PUSH_ATTEMPTS):
        try:
            webpush(
                subscription_info=_subscription_info(sub),
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=vapid_private_key(),
                vapid_claims={"sub": vapid_email()},
                timeout=5,
            )
            return True
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                # Endpoint gone (user unsubscribed elsewhere / revoked) — prune it.
                db.delete(sub)
                db.commit()
                logger.info("Pruned dead push endpoint for user %s", sub.user_id)
                return False
            last_exc = exc
            if status is not None and status not in _TRANSIENT_STATUSES:
                # A permanent 4xx (bad keys/payload): retrying cannot help.
                logger.warning("Push to user %s failed (HTTP %s): %s", sub.user_id, status, exc)
                return False
        except Exception as exc:  # noqa: BLE001 - never let push break message delivery
            last_exc = exc
        if attempt < _PUSH_ATTEMPTS - 1:
            time.sleep(0.5 * (attempt + 1))

    logger.warning(
        "Push to user %s failed after %d attempts: %s", sub.user_id, _PUSH_ATTEMPTS, last_exc
    )
    return False


def test_push(db: Session, user_id: int) -> list[dict[str, Any]]:
    """Diagnostic send for `manage.py test-push`: delivers a fixed test
    payload to every enabled subscription of a user and returns a per-device
    report. Dead endpoints are pruned by _send() as a side effect (reported
    back as ``pruned`` so the CLI can tell the operator the device will
    self-heal on its next app open)."""
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
    results = []
    for sub in subs:
        sub_id, endpoint = sub.id, sub.endpoint
        delivered = _send(db, sub, payload)
        results.append(
            {
                "endpoint": endpoint,
                "delivered": delivered,
                # False means _send() pruned the row (404/410 from the service).
                "pruned": db.get(PushSubscription, sub_id) is None,
            }
        )
    return results


def deliver_to_user(db: Session, user_id: int, payload: dict[str, Any]) -> int:
    """Push to all of a user's enabled subscriptions. Returns count delivered."""
    if not push_enabled():
        return 0
    subs = (
        db.execute(
            select(PushSubscription).where(
                PushSubscription.user_id == user_id,
                PushSubscription.enabled == True,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    sent = 0
    for sub in subs:
        if _send(db, sub, payload):
            sent += 1
    return sent


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
# request commits. Each opens its OWN database session (the request's session
# is closed by the time the task runs) and does the (blocking) HTTP push in a
# worker thread so the event loop stays responsive.
import asyncio

from ..db import SessionLocal


async def group_push_task(sender_id: int, payload: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        await asyncio.to_thread(notify_group, db, sender_id, payload)
    except Exception:  # noqa: BLE001
        logger.exception("group push fan-out failed")
    finally:
        db.close()


async def room_push_task(
    member_ids: set[int], sender_id: int, payload: dict[str, Any]
) -> None:
    db = SessionLocal()
    try:
        await asyncio.to_thread(notify_room, db, member_ids, sender_id, payload)
    except Exception:  # noqa: BLE001
        logger.exception("room push fan-out failed")
    finally:
        db.close()
