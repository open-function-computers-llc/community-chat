"""Transactional email (SMTP).

Users opt in (Settings → "Email notifications"). When they have both a stored
email address and the opt-in flag set, the app sends them notification email
through a transactional provider (Resend, Mailgun, …) over SMTP. In local dev
that SMTP server is a Mailpit container; in production it's the provider.

Mirrors the web-push fan-out in push.py: routers fire a best-effort background
task right after the request commits; each opens its OWN session and does the
blocking SMTP send in a worker thread so the event loop stays responsive.
Failure to send an email is logged and never affects the message delivery.

The HTML lives in app/templates/email/ as plain HTML files with %%MERGE_TAG%%
placeholders (base.html is the shared frame; each email has its own file, with
optional lines as partials/). _merge() fills the tags and escapes values by
default, so the templates are syntax-highlightable and safe.
"""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..core.config import (
    app_origin,
    email_enabled,
    smtp_from_address,
    smtp_from_name,
    smtp_host,
    smtp_password,
    smtp_port,
    smtp_timeout,
    smtp_username,
)
from ..db import SessionLocal, get_db
from ..models import ChannelNotification, User
from ..core.security import get_current_user

logger = logging.getLogger("chat.email")

router = APIRouter()


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------
def _build_message(subject: str, html: str, to_addr: str, text: str | None = None) -> EmailMessage:
    """Assemble the multipart/alternative message (text + html).

    The brand illustration is inlined when the HTML references it (the frame
    only emits the cid: reference if the file exists). Turning the html
    alternative into multipart/related keeps it self-contained: remote
    image URLs would just be blocked by clients' image privacy."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{smtp_from_name()} <{smtp_from_address()}>"
    msg["To"] = to_addr
    msg.set_content(text or TEXT_NOTIFICATIONS)
    msg.add_alternative(html, subtype="html")
    if BRAND_IMAGE.exists() and f"cid:{BRAND_CID}" in html:
        try:
            subtype = _IMAGE_SUBTYPES.get(BRAND_IMAGE.suffix.lower(), "png")
            msg.get_payload()[-1].add_related(
                BRAND_IMAGE.read_bytes(),
                maintype="image",
                subtype=subtype,
                cid=f"<{BRAND_CID}>",
            )
        except OSError:
            logger.warning("brand image unreadable at %s; sending without it", BRAND_IMAGE)
    return msg


def _send_smtp(subject: str, html: str, to_addr: str, text: str | None = None) -> None:
    """Send one email over SMTP. Raises on SMTP errors (caller decides)."""
    msg = _build_message(subject, html, to_addr, text)
    host, port = smtp_host(), smtp_port()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=smtp_timeout())
    else:
        server = smtplib.SMTP(host, port, timeout=smtp_timeout())
    try:
        if port != 465:
            # Opportunistic STARTTLS: upgrade when the server advertises it
            # (real providers do), but proceed in plaintext when it doesn't —
            # local dev sinks like Mailpit only speak plain SMTP, and an
            # unconditional starttls() would raise SMTPNotSupportedError.
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls()
                server.ehlo()  # re-identify over the upgraded connection
        if smtp_username():
            server.login(smtp_username(), smtp_password() or "")
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


def send_email(subject: str, html: str, to_addr: str, text: str | None = None) -> bool:
    """Best-effort send: returns True on success, False (with a log) on any
    error. Never raises. `text` is the text/plain part — pass the TEXT_*
    constant whose audience matches the email (defaults to the notification
    wording)."""
    if not email_enabled():
        logger.debug("email disabled (no SMTP_HOST/SMTP_FROM_ADDRESS); skipping")
        return False
    try:
        _send_smtp(subject, html, to_addr, text or TEXT_NOTIFICATIONS)
        logger.info("email sent to %s: %s", to_addr, subject)
        return True
    except Exception:  # noqa: BLE001 - an email failure must not break the app
        logger.exception("failed to send email to %s", to_addr)
        return False


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
# All email HTML lives in app/templates/email/ as plain HTML files with
# %%MERGE_TAG%% placeholders — readable with standard HTML/CSS syntax
# highlighting, no template engine. _merge() fills the tags and HTML-escapes
# every value by default (templates stay safe even if a call site forgets);
# fragments produced by _merge() are marked _Raw so they compose without
# double-escaping.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"

# Brand imagery shipped with the backend (backend/assets, /app/assets in the
# container). Referenced from the HTML by cid:<BRAND_CID> and attached inline
# by _send_smtp — remote URLs would be blocked by most clients' image
# privacy, an inline part always renders. Attribution for the unDraw art
# lives in the app's "About this app" modal (frontend).
_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"
BRAND_IMAGE = _ASSETS_DIR / "undraw_family_6gj8.png"
BRAND_CID = "undraw-family"
_IMAGE_SUBTYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif"}

# Plain-text (text/plain) counterpart for each email, matching its footer.
# The right one is passed to send_email() by the email's builder/caller.
TEXT_NOTIFICATIONS = (
    "You're receiving this because you enabled email notifications in "
    "Community Chat. To stop receiving them, open the app and turn the "
    "toggle off in Settings."
)
TEXT_INVITE = (
    "You're receiving this because you were invited to join the chat "
    "application for your community. Open the link in this email to join."
)
TEXT_INVITE_USED = (
    "You're receiving this because someone used an invite you created on "
    "Community Chat."
)
TEXT_PASSWORD_RESET = (
    "You're receiving this because an administrator reset your Community "
    "Chat password."
)
TEXT_WELCOME = (
    "You're receiving this because you created your Community Chat account."
)
TEXT_TEST = (
    "You're receiving this because someone ran the email delivery test on "
    "the Community Chat server."
)
TEXT_SUMMARY = (
    "You're receiving this because you turned on Channel Summaries in "
    "Community Chat. Change the cadence in Settings."
)


class _Raw(str):
    """A pre-rendered HTML fragment — injected into templates unescaped."""


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _merge(template: str, **tags: str) -> _Raw:
    """Render a template file from templates/email/ by replacing each
    %%TAG%% with its value. Values are escaped unless already _Raw. HTML
    comments are stripped first, so template comments are pure authoring
    documentation and never ship inside the email."""
    html = (_TEMPLATES_DIR / template).read_text(encoding="utf-8")
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    for tag, value in tags.items():
        if not isinstance(value, _Raw):
            value = _esc("" if value is None else str(value))
        html = html.replace(f"%%{tag}%%", value)
    return _Raw(html)


def _brand_masthead() -> _Raw:
    """The masthead <img> for the shared frame. Empty (the row collapses) if
    the asset file is missing, so a stripped deployment never shows a broken
    image."""
    if not BRAND_IMAGE.exists():
        return _Raw("")
    return _Raw(
        f'<img src="cid:{BRAND_CID}" width="176" alt="Community Chat" '
        'style="display: block; margin: 0 auto; padding: 22px 0 0;" />'
    )


def _frame(body: _Raw, footer: str = "partials/footer.html") -> _Raw:
    """Wrap an email body in the shared base frame. The footer partial is
    chosen per email to match its audience — not everyone who gets an email
    turned on notifications (invitees aren't members yet, a password reset
    isn't a subscription), so the default fits only opted-in members."""
    return _merge(
        "base.html", EMAIL_BODY=body, FOOTER=_merge(footer), BRAND_IMG=_brand_masthead()
    )


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------
def _email_recipients(db, user_ids: Iterable[int], room_id: int | None = None) -> list[tuple[int, str]]:
    """Return (user_id, email) for each member with a valid email address
    who turned on immediate email for this channel (ChannelNotification;
    room_id NULL = group chat). Strictly opt-in: no row means off."""
    ids = list(user_ids)
    if not ids:
        return []
    cond = (
        ChannelNotification.room_id.is_(None)
        if room_id is None
        else ChannelNotification.room_id == room_id
    )
    rows = db.execute(
        select(ChannelNotification.user_id, User.email)
        .join(User, User.id == ChannelNotification.user_id)
        .where(
            cond,
            ChannelNotification.user_id.in_(ids),
            ChannelNotification.immediate_email == True,  # noqa: E712
            User.is_active == True,  # noqa: E712
            User.email.isnot(None),
        )
    ).all()
    return [(uid, email) for uid, email in rows if email and "@" in email]


# ---------------------------------------------------------------------------
# Fan-out (sync; called from a worker thread)
# ---------------------------------------------------------------------------
# Fan-out is split in two: a TARGETS phase that reads the DB, and a DELIVER
# phase that only does network I/O. Background callers must finish the first
# phase and CLOSE their session before the second one starts — holding a
# session (and its open SQLite transaction) across a slow SMTP round-trip
# serializes every other request against the single-connection database, and
# the close()-rollback at task teardown can discard concurrent commits.
def _targets(
    db, user_ids: Iterable[int], subject: str, html: str, room_id: int | None = None,
    text: str = TEXT_NOTIFICATIONS,
) -> list[tuple[str, str, str, str]]:
    return [(subject, html, addr, text) for _uid, addr in _email_recipients(db, user_ids, room_id)]


def _deliver(batch: list[tuple[str, str, str, str]]) -> int:
    sent = 0
    for subject, html, addr, text in batch:
        if send_email(subject, html, addr, text=text):
            sent += 1
    return sent


def _group_targets(db, sender_id: int, sender_name: str, text: str) -> list[tuple[str, str, str]]:
    """Resolve (subject, html, address) triples for a new group message."""
    if not email_enabled():
        return []
    user_ids = {
        r[0] for r in db.query(User.id).filter(User.is_active == True).all()  # noqa: E712
    }
    user_ids.discard(sender_id)
    return _targets(db, user_ids, _group_subject(text), _group_html(sender_name, text), None)


def _room_targets(
    db, member_ids: set[int], sender_id: int, sender_name: str, text: str, room_label: str,
    room_id: int | None = None,
) -> list[tuple[str, str, str]]:
    """Resolve (subject, html, address) triples for a new family-room message."""
    if not email_enabled():
        return []
    ids = set(member_ids) - {sender_id}
    return _targets(
        db, ids, _group_subject(text), _group_html(sender_name, text, room_label), room_id
    )


def notify_group(db, sender_id: int, sender_name: str, text: str) -> int:
    """Email every opt-in user (except the sender) about a new group message."""
    return _deliver(_group_targets(db, sender_id, sender_name, text))


def notify_room(
    db, member_ids: set[int], sender_id: int, sender_name: str, text: str, room_label: str,
    room_id: int | None = None,
) -> int:
    """Email every room member with immediate email on for this room
    (except the sender) about a new message."""
    return _deliver(_room_targets(db, member_ids, sender_id, sender_name, text, room_label, room_id))


def _group_subject(text: str) -> str:
    snippet = (text or "").strip()
    snippet = snippet[:60] + "…" if len(snippet) > 60 else snippet
    return f"New message{f': {snippet}' if snippet else ''}"


def _group_html(sender_name: str, text: str, room_label: str | None = None) -> str:
    room_block = (
        _merge("partials/room_label.html", ROOM_LABEL=room_label) if room_label else _Raw("")
    )
    return _frame(
        _merge(
            "group_message.html",
            SENDER_NAME=sender_name,
            SNIPPET=(text or "").strip() or "(attachment)",
            ROOM_LABEL_BLOCK=room_block,
            BUTTON_URL=app_origin() + "/",
        )
    )


def invite_html(recipient_name: str, code: str, link: str, family_name: str | None) -> str:
    family_block = (
        _merge("partials/invite_family.html", FAMILY_NAME=family_name) if family_name else _Raw("")
    )
    return _frame(
        _merge(
            "invite.html",
            RECIPIENT_NAME=recipient_name,
            CODE=code,
            FAMILY_BLOCK=family_block,
            LINK=link,
        ),
        footer="partials/footer_invite.html",
    )


def invite_used_html(creator_name: str, joiner_name: str, family_name: str | None) -> str:
    family_block = (
        _merge("partials/invite_family.html", FAMILY_NAME=family_name) if family_name else _Raw("")
    )
    return _frame(
        _merge(
            "invite_used.html",
            CREATOR_NAME=creator_name,
            JOINER_NAME=joiner_name,
            FAMILY_BLOCK=family_block,
            APP_URL=app_origin() + "/",
        ),
        footer="partials/footer_invite_used.html",
    )


def password_reset_html(recipient_name: str, password: str, handle: str) -> str:
    return _frame(
        _merge(
            "password_reset.html",
            RECIPIENT_NAME=recipient_name,
            PASSWORD=password,
            HANDLE=handle,
            SIGNIN_URL=app_origin() + "/login",
        ),
        footer="partials/footer_password_reset.html",
    )


def send_password_reset_email(to_addr: str, recipient_name: str, password: str, handle: str) -> bool:
    """Send a password-reset email with the new (temporary) password."""
    return send_email(
        "Your Community Chat password was reset",
        password_reset_html(recipient_name, password, handle),
        to_addr,
        TEXT_PASSWORD_RESET,
    )


def welcome_html(display_name: str) -> str:
    name = (display_name or "").split()[0] if display_name else ""
    greeting = f"Welcome{f' {name}' if name else ''}! You've joined Community Chat."
    return _frame(
        _merge("welcome.html", GREETING=greeting, APP_URL=app_origin() + "/"),
        footer="partials/footer_welcome.html",
    )


def test_email_html() -> str:
    """A fixed, branded message used by the `manage.py test-email` command to
    verify the SMTP configuration is actually delivering mail."""
    return _frame(
        _merge("test_email.html", APP_URL=app_origin() + "/"),
        footer="partials/footer_test.html",
    )


def summary_html(display_name: str, scope_title: str, intro: str, sections_html: str) -> str:
    """A Channel Summary digest. sections_html must already be escaped
    (app/summary.py builds it from message data through _esc)."""
    name = (display_name or "").split()[0] if display_name else ""
    greeting = f"Hey{f' {name}' if name else ''},"
    return _frame(
        _merge(
            "channel_summary.html",
            SCOPE_TITLE=scope_title,
            GREETING=greeting,
            INTRO=intro,
            SECTIONS=_Raw(sections_html),
            APP_URL=app_origin() + "/",
        ),
        footer="partials/footer_summary.html",
    )


# ---------------------------------------------------------------------------
# Endpoints (admin can see config state; feature is opt-in via settings)
# ---------------------------------------------------------------------------
@router.get("/status")
async def status(user: User = Depends(get_current_user)):
    """Whether email notifications are available for the current user."""
    return {
        "enabled": email_enabled(),
        "has_email": bool(user.email and "@" in user.email),
    }


# ---------------------------------------------------------------------------
# Background task wrappers (mirror push.py)
# ---------------------------------------------------------------------------
# The DB (targets) phase runs on the event-loop thread: sync DB code there
# executes atomically between awaits, so a fan-out session never interleaves
# with request sessions on the single shared SQLite connection (two sessions
# sharing one StaticPool connection share one transaction context — whoever
# closes/rolls back first discards the other's pending writes). Only the
# network send goes to a worker thread, and it touches no session at all.
def _collect_group_batch(sender_id: int, sender_name: str, text: str) -> list[tuple[str, str, str]]:
    db = SessionLocal()
    try:
        return _group_targets(db, sender_id, sender_name, text)
    finally:
        db.close()


def _collect_room_batch(
    member_ids: set[int], sender_id: int, sender_name: str, text: str, room_label: str,
    room_id: int | None = None,
) -> list[tuple[str, str, str]]:
    db = SessionLocal()
    try:
        return _room_targets(db, member_ids, sender_id, sender_name, text, room_label, room_id)
    finally:
        db.close()


def _invite_used_email_task(
    to_addr: str, creator_name: str, joiner_name: str, family_name: str | None
) -> None:
    send_email(
        "Your Community Chat invite was used",
        invite_used_html(creator_name, joiner_name, family_name),
        to_addr,
        TEXT_INVITE_USED,
    )


def _welcome_email_task(to_addr: str, display_name: str) -> None:
    send_email(
        "Welcome to Community Chat",
        welcome_html(display_name),
        to_addr,
        TEXT_WELCOME,
    )


async def group_email_task(sender_id: int, sender_name: str, text: str) -> None:
    try:
        batch = _collect_group_batch(sender_id, sender_name, text)
    except Exception:  # noqa: BLE001
        logger.exception("group email fan-out failed")
        return
    await asyncio.to_thread(_deliver, batch)


async def room_email_task(
    member_ids: set[int], sender_id: int, sender_name: str, text: str, room_label: str,
    room_id: int | None = None,
) -> None:
    try:
        batch = _collect_room_batch(member_ids, sender_id, sender_name, text, room_label, room_id)
    except Exception:  # noqa: BLE001
        logger.exception("room email fan-out failed")
        return
    await asyncio.to_thread(_deliver, batch)


async def invite_used_email_task(
    to_addr: str, creator_name: str, joiner_name: str, family_name: str | None
) -> None:
    await asyncio.to_thread(
        _invite_used_email_task, to_addr, creator_name, joiner_name, family_name
    )


async def welcome_email_task(to_addr: str, display_name: str) -> None:
    await asyncio.to_thread(_welcome_email_task, to_addr, display_name)


def fire_invite_used_email(
    to_addr: str, creator_name: str, joiner_name: str, family_name: str | None
) -> None:
    """Fire-and-forget the "your invite was used" notice to the invite's
    creator when someone registers with their invite."""
    if not email_enabled():
        return
    asyncio.get_event_loop().create_task(
        invite_used_email_task(to_addr, creator_name, joiner_name, family_name)
    )


def fire_welcome_email(to_addr: str, display_name: str) -> None:
    """Fire-and-forget a welcome email at registration."""
    if not email_enabled():
        return
    asyncio.get_event_loop().create_task(welcome_email_task(to_addr, display_name))
