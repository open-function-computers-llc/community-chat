"""Channel Summary digests: a nightly and a weekly (Saturday) recap of
activity in each channel the user opted into summaries for.

Strictly opt-in: recipients come from the per-channel ChannelNotification
matrix (nightly_email/push, weekly_email/push). Only channels with actual
new messages in the window produce sections, and only channels the user is
still a member of (rooms: their family attached; group: everyone active).

Mirrors the email/push task split: collect_summaries() does ALL database
work and returns plain send instructions; deliver_summaries() is pure
network and touches no session. The scheduler runs the collect phase on
the event-loop thread (sync DB code there is atomic between awaits) and
hands the send phase to a worker thread.

Operational knobs (env): SUMMARY_ENABLED (0 disables), SUMMARY_NIGHTLY_UTC
(HH:MM, default 02:00), SUMMARY_WEEKLY_UTC (default 02:30),
SUMMARY_WEEKLY_WEEKDAY (Python weekday, default 5 = Saturday). A manual
run is available via `manage.py run-summaries <scope>`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from .core.config import (
    summary_enabled,
    summary_nightly_time,
    summary_weekly_time,
    summary_weekly_weekday,
)
from .db import SessionLocal
from .models import (
    ChannelNotification,
    DigestRun,
    DmRoom,
    GroupMessage,
    PushSubscription,
    RoomMessage,
    User,
)
from .routers.email import _deliver as _deliver_email_batch
from .routers.email import _esc, summary_html
from .routers.email import TEXT_SUMMARY
from .routers.push import _deliver as _push_one
from .routers.push import _prune_dead

logger = logging.getLogger("chat.summary")

SCOPES = ("nightly", "weekly")
_MAX_LINES = 8  # preview lines rendered per channel section

_SCOPE_TITLE = {"nightly": "Nightly summary", "weekly": "Weekly summary"}
_SCOPE_INTRO = {
    "nightly": "Here's what happened in your channels in the last 24 hours:",
    "weekly": "Here's what happened in your channels in the last week:",
}
_SCOPE_SUBJECT = {
    "nightly": "Your Community Chat daily summary",
    "weekly": "Your Community Chat weekly summary",
}


def _window_start(scope: str, now: datetime) -> datetime:
    return now - (timedelta(days=1) if scope == "nightly" else timedelta(days=7))


def _name(user: User | None) -> str:
    if user is None:
        return "Someone"
    return user.display_name or user.handle


def _snippet(text: str | None, file_name: str | None) -> str:
    body = (text or "").strip()
    if not body:
        return f"(attachment: {file_name})" if file_name else "(attachment)"
    return body[:120] + "…" if len(body) > 120 else body


def _sections_html(sections: list[tuple[str, int, list[tuple[str, str]]]]) -> str:
    parts: list[str] = []
    for label, count, lines in sections:
        parts.append(
            f'<p style="margin:16px 0 4px;font-weight:bold;">{_esc(label)}'
            f' <span style="color:#9aa0a6;font-weight:normal;">({count} new)</span></p>'
        )
        for sender, snippet in lines:
            parts.append(
                f'<p style="margin:2px 0;font-size:14px;"><strong>{_esc(sender)}:</strong> '
                f"{_esc(snippet)}</p>"
            )
    return "".join(parts)


def _room_name(room: DmRoom) -> str:
    names = [f.name for f in room.families]
    return " & ".join(names) if names else "Family chat"


def collect_summaries(
    db, scope: str, now: datetime | None = None
) -> tuple[list[tuple[str, str, str, str]], list[tuple[int, int, dict, dict]]]:
    """Read-only phase. Returns (email_batch, push_batch):
    email_batch: [(subject, html, addr, text)]
    push_batch:  [(sub_id, user_id, subscription_info, payload)]
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown summary scope {scope!r}")
    now = now or datetime.now(timezone.utc)
    cutoff = _window_start(scope, now)
    flag_email = "nightly_email" if scope == "nightly" else "weekly_email"
    flag_push = "nightly_push" if scope == "nightly" else "weekly_push"

    rows = db.execute(
        select(ChannelNotification).where(
            or_(
                getattr(ChannelNotification, flag_email) == True,  # noqa: E712
                getattr(ChannelNotification, flag_push) == True,  # noqa: E712
            )
        )
    ).scalars().all()

    # (user_id) -> {user, sections[], wants_email, wants_push}
    per_user: dict[int, dict] = {}

    def entry(uid: int, user: User, wants_email: bool, wants_push: bool) -> dict:
        e = per_user.setdefault(uid, {"user": user, "sections": [], "email": False, "push": False})
        e["email"] = e["email"] or wants_email
        e["push"] = e["push"] or wants_push
        return e

    for row in rows:
        user = db.get(User, row.user_id)
        if user is None or not user.is_active:
            continue
        wants_email = bool(getattr(row, flag_email)) and bool(user.email and "@" in user.email)
        wants_push = bool(getattr(row, flag_push))
        if not wants_email and not wants_push:
            continue

        if row.room_id is None:
            messages = db.execute(
                select(GroupMessage)
                .where(GroupMessage.created_at >= cutoff)
                .order_by(GroupMessage.created_at)
            ).scalars().all()
            label = "Group Chat"
        else:
            room = db.get(DmRoom, row.room_id)
            if room is None:
                continue
            if user.family_id is None or not any(f.id == user.family_id for f in room.families):
                continue  # no longer a member of this room
            messages = db.execute(
                select(RoomMessage)
                .where(RoomMessage.room_id == room.id, RoomMessage.created_at >= cutoff)
                .order_by(RoomMessage.created_at)
            ).scalars().all()
            label = _room_name(room)

        if not messages:
            continue  # nothing happened — no section, and maybe no digest at all
        lines = [(_name(m.author if row.room_id is None else m.sender), _snippet(m.text, m.file_name)) for m in messages]
        entry(row.user_id, user, wants_email, wants_push)["sections"].append(
            (label, len(messages), lines[:_MAX_LINES])
        )

    email_batch: list[tuple[str, str, str, str]] = []
    push_batch: list[tuple[int, int, dict, dict]] = []
    for uid, e in per_user.items():
        if not e["sections"]:
            continue
        title = _SCOPE_TITLE[scope]
        if e["email"]:
            email_batch.append(
                (
                    _SCOPE_SUBJECT[scope],
                    summary_html(
                        e["user"].display_name or e["user"].handle,
                        title,
                        _SCOPE_INTRO[scope],
                        _sections_html(e["sections"]),
                    ),
                    e["user"].email,
                    TEXT_SUMMARY,
                )
            )
        if e["push"]:
            summary_line = " · ".join(f"{label}: {count}" for label, count, _ in e["sections"])
            payload = {
                "title": title,
                "body": summary_line[:200],
                "tag": f"summary-{scope}-{now.strftime('%Y%m%d')}-{uid}",
                "icon": "/icon-192.png",
                "url": "/",
            }
            subs = db.execute(
                select(PushSubscription).where(PushSubscription.user_id == uid)
            ).scalars().all()
            for sub in subs:
                push_batch.append(
                    (sub.id, uid, {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}, payload)
                )
    return email_batch, push_batch


def deliver_summaries(
    email_batch: list[tuple[str, str, str, str]],
    push_batch: list[tuple[int, int, dict, dict]],
) -> dict[str, int]:
    """Network phase (runs off the event loop; holds no session)."""
    sent = _deliver_email_batch(email_batch)
    dead: list[int] = []
    delivered = 0
    for sub_id, user_id, info, payload in push_batch:
        outcome = _push_one(info, user_id, payload)
        if outcome == "ok":
            delivered += 1
        elif outcome == "dead":
            dead.append(sub_id)
    _prune_dead(dead)
    return {"emails_sent": sent, "pushes_delivered": delivered, "pushes_total": len(push_batch)}


def run_summary(db, scope: str, now: datetime | None = None) -> dict[str, int]:
    """Synchronous collect + deliver (CLI / tests; scheduler uses the split
    phases so the DB work stays on the event-loop thread)."""
    email_batch, push_batch = collect_summaries(db, scope, now)
    return deliver_summaries(email_batch, push_batch)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def due_scopes(
    now: datetime,
    last_runs: dict[str, str | None],
) -> list[str]:
    """Pure decision: which summary scopes are due at `now` (UTC), given the
    ISO-date strings of their last successful run. Runs fire once per
    calendar day once their HH:MM has passed."""
    if not summary_enabled():
        return []
    due: list[str] = []
    today = now.date().isoformat()

    nh, nm = summary_nightly_time()
    if last_runs.get("nightly") != today and (now.hour, now.minute) >= (nh, nm):
        due.append("nightly")

    wh, wm = summary_weekly_time()
    if (
        now.weekday() == summary_weekly_weekday()
        and last_runs.get("weekly") != today
        and (now.hour, now.minute) >= (wh, wm)
    ):
        due.append("weekly")
    return due


def _load_last_runs(db) -> dict[str, str | None]:
    rows = db.execute(select(DigestRun)).scalars().all()
    return {scope: None for scope in SCOPES} | {r.scope: r.last_run_date for r in rows}


def _mark_run(db, scope: str, date_str: str) -> None:
    row = db.get(DigestRun, scope)
    if row is None:
        db.add(DigestRun(scope=scope, last_run_date=date_str))
    else:
        row.last_run_date = date_str
    db.commit()


async def summary_scheduler(tick_seconds: int = 900) -> None:
    """Long-lived loop started from lifespan. Each tick checks which scopes
    are due; the collect phase runs inline on the event-loop thread (sync DB
    code is atomic between awaits, so it never interleaves with request
    sessions on the shared SQLite connection) and only the sends go to a
    worker thread. The run marker persists, so restarts never double-send."""
    logger.info(
        "Channel Summary scheduler awake (nightly %02d:%02d UTC, weekly %s %02d:%02d UTC)",
        *summary_nightly_time(),
        ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][summary_weekly_weekday()],
        *summary_weekly_time(),
    )
    while True:
        try:
            now = datetime.now(timezone.utc)
            db = SessionLocal()
            try:
                due = due_scopes(now, _load_last_runs(db))
            finally:
                db.close()
            for scope in due:
                db = SessionLocal()
                try:
                    email_batch, push_batch = collect_summaries(db, scope, now)
                finally:
                    db.close()
                stats = await asyncio.to_thread(deliver_summaries, email_batch, push_batch)
                db = SessionLocal()
                try:
                    _mark_run(db, scope, now.date().isoformat())
                finally:
                    db.close()
                logger.info("Summary %s done: %s", scope, stats)
        except Exception:  # noqa: BLE001 - the loop must survive any single tick
            logger.exception("summary scheduler tick failed")
        await asyncio.sleep(tick_seconds)
