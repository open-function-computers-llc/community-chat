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
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

from fastapi import APIRouter, Depends

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
from ..models import DMSettings, User
from ..core.security import get_current_user

logger = logging.getLogger("chat.email")

router = APIRouter()


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------
def _send_smtp(subject: str, html: str, to_addr: str) -> None:
    """Send one email over SMTP. Raises on SMTP errors (caller decides)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{smtp_from_name()} <{smtp_from_address()}>"
    msg["To"] = to_addr
    msg.set_content(
        "You're receiving this because you enabled email notifications in "
        "Community Chat. To stop receiving them, open the app and turn the "
        "toggle off in Settings."
    )
    msg.add_alternative(html, subtype="html")

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


def send_email(subject: str, html: str, to_addr: str) -> bool:
    """Best-effort send: returns True on success, False (with a log) on any
    error. Never raises."""
    if not email_enabled():
        logger.debug("email disabled (no SMTP_HOST/SMTP_FROM_ADDRESS); skipping")
        return False
    try:
        _send_smtp(subject, html, to_addr)
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


class _Raw(str):
    """A pre-rendered HTML fragment — injected into templates unescaped."""


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _merge(template: str, **tags: str) -> _Raw:
    """Render a template file from templates/email/ by replacing each
    %%TAG%% with its value. Values are escaped unless already _Raw."""
    html = (_TEMPLATES_DIR / template).read_text(encoding="utf-8")
    for tag, value in tags.items():
        if not isinstance(value, _Raw):
            value = _esc("" if value is None else str(value))
        html = html.replace(f"%%{tag}%%", value)
    return _Raw(html)


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------
def _email_recipients(db, user_ids: Iterable[int]) -> list[tuple[int, str]]:
    """Return (user_id, email) for each user with a valid email AND email
    notifications enabled. Uses the user's stored email address directly."""
    ids = list(user_ids)
    if not ids:
        return []
    out = []
    for u in db.query(User).filter(User.id.in_(ids), User.email.isnot(None), User.is_active == True).all():  # noqa: E712
        settings = u.dm_settings
        if not settings or not settings.email_notifications:
            continue
        if not u.email or "@" not in u.email:
            continue
        out.append((u.id, u.email))
    return out


# ---------------------------------------------------------------------------
# Fan-out (sync; called from a worker thread)
# ---------------------------------------------------------------------------
def notify_group(db, sender_id: int, sender_name: str, text: str) -> int:
    """Email every opt-in user (except the sender) about a new group message."""
    if not email_enabled():
        return 0
    user_ids = {
        r[0] for r in db.query(User.id).filter(User.is_active == True).all()  # noqa: E712
    }
    user_ids.discard(sender_id)
    return _deliver_many(db, user_ids, _group_subject(text), _group_html(sender_name, text))


def notify_room(db, member_ids: set[int], sender_id: int, sender_name: str, text: str, room_label: str) -> int:
    """Email every opt-in room member (except the sender) about a new message."""
    if not email_enabled():
        return 0
    ids = set(member_ids) - {sender_id}
    return _deliver_many(db, ids, _group_subject(text), _group_html(sender_name, text, room_label))


def _deliver_many(db, user_ids, subject: str, html: str) -> int:
    recipients = _email_recipients(db, user_ids)
    sent = 0
    for _uid, addr in recipients:
        if send_email(subject, html, addr):
            sent += 1
    return sent


def _group_subject(text: str) -> str:
    snippet = (text or "").strip()
    snippet = snippet[:60] + "…" if len(snippet) > 60 else snippet
    return f"New message{f': {snippet}' if snippet else ''}"


def _group_html(sender_name: str, text: str, room_label: str | None = None) -> str:
    room_block = (
        _merge("partials/room_label.html", ROOM_LABEL=room_label) if room_label else _Raw("")
    )
    return _merge(
        "base.html",
        EMAIL_BODY=_merge(
            "group_message.html",
            SENDER_NAME=sender_name,
            SNIPPET=(text or "").strip() or "(attachment)",
            ROOM_LABEL_BLOCK=room_block,
            BUTTON_URL=app_origin() + "/",
        ),
    )


def invite_html(recipient_name: str, code: str, link: str, family_name: str | None) -> str:
    family_block = (
        _merge("partials/invite_family.html", FAMILY_NAME=family_name) if family_name else _Raw("")
    )
    return _merge(
        "base.html",
        EMAIL_BODY=_merge(
            "invite.html",
            RECIPIENT_NAME=recipient_name,
            CODE=code,
            FAMILY_BLOCK=family_block,
            LINK=link,
        ),
    )


def password_reset_html(recipient_name: str, password: str, handle: str) -> str:
    return _merge(
        "base.html",
        EMAIL_BODY=_merge(
            "password_reset.html",
            RECIPIENT_NAME=recipient_name,
            PASSWORD=password,
            HANDLE=handle,
            SIGNIN_URL=app_origin() + "/login",
        ),
    )


def send_password_reset_email(to_addr: str, recipient_name: str, password: str, handle: str) -> bool:
    """Send a password-reset email with the new (temporary) password."""
    return send_email(
        "Your Community Chat password was reset",
        password_reset_html(recipient_name, password, handle),
        to_addr,
    )


def welcome_html(display_name: str) -> str:
    name = (display_name or "").split()[0] if display_name else ""
    greeting = f"Welcome{f' {name}' if name else ''}! You've joined Community Chat."
    return _merge(
        "base.html",
        EMAIL_BODY=_merge("welcome.html", GREETING=greeting, APP_URL=app_origin() + "/"),
    )


def test_email_html() -> str:
    """A fixed, branded message used by the `manage.py test-email` command to
    verify the SMTP configuration is actually delivering mail."""
    return _merge(
        "base.html",
        EMAIL_BODY=_merge("test_email.html", APP_URL=app_origin() + "/"),
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
def _group_email_task(sender_id: int, sender_name: str, text: str) -> None:
    db = SessionLocal()
    try:
        notify_group(db, sender_id, sender_name, text)
    except Exception:  # noqa: BLE001
        logger.exception("group email fan-out failed")
    finally:
        db.close()


def _room_email_task(member_ids: set[int], sender_id: int, sender_name: str, text: str, room_label: str) -> None:
    db = SessionLocal()
    try:
        notify_room(db, member_ids, sender_id, sender_name, text, room_label)
    except Exception:  # noqa: BLE001
        logger.exception("room email fan-out failed")
    finally:
        db.close()


def _invite_email_task(to_addr: str, recipient_name: str, code: str, link: str, family_name: str | None) -> None:
    send_email(
        "You're invited to Community Chat",
        invite_html(recipient_name, code, link, family_name),
        to_addr,
    )


def _welcome_email_task(to_addr: str, display_name: str) -> None:
    send_email("Welcome to Community Chat", welcome_html(display_name), to_addr)


async def group_email_task(sender_id: int, sender_name: str, text: str) -> None:
    await asyncio.to_thread(_group_email_task, sender_id, sender_name, text)


async def room_email_task(
    member_ids: set[int], sender_id: int, sender_name: str, text: str, room_label: str
) -> None:
    await asyncio.to_thread(_room_email_task, member_ids, sender_id, sender_name, text, room_label)


async def invite_email_task(
    to_addr: str, recipient_name: str, code: str, link: str, family_name: str | None
) -> None:
    await asyncio.to_thread(_invite_email_task, to_addr, recipient_name, code, link, family_name)


async def welcome_email_task(to_addr: str, display_name: str) -> None:
    await asyncio.to_thread(_welcome_email_task, to_addr, display_name)


def fire_invite_email(
    to_addr: str, recipient_name: str, code: str, link: str, family_name: str | None
) -> None:
    """Fire-and-forget the invite email from within a request handler."""
    if not email_enabled():
        return
    asyncio.get_event_loop().create_task(
        invite_email_task(to_addr, recipient_name, code, link, family_name)
    )


def fire_welcome_email(to_addr: str, display_name: str) -> None:
    """Fire-and-forget a welcome email at registration."""
    if not email_enabled():
        return
    asyncio.get_event_loop().create_task(welcome_email_task(to_addr, display_name))
