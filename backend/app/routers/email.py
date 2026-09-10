"""Transactional email (SMTP).

Users opt in (Settings → "Email notifications"). When they have both a stored
email address and the opt-in flag set, the app sends them notification email
through a transactional provider (Resend, Mailgun, …) over SMTP. In local dev
that SMTP server is a Mailpit container; in production it's the provider.

Mirrors the web-push fan-out in push.py: routers fire a best-effort background
task right after the request commits; each opens its OWN session and does the
blocking SMTP send in a worker thread so the event loop stays responsive.
Failure to send an email is logged and never affects the message delivery.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
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
            server.starttls()
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
# Rendering helpers
# ---------------------------------------------------------------------------
def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


_BASE_CSS = (
    '<html><head><meta name="x-apple-disable-message-reformatting"></head>'
    '<body style="margin:0;padding:0;background:#f4f5f7;font-family:Arial,Helvetica,sans-serif;color:#1c1e21;">'
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 0;">'
    '<tr><td align="center"><table role="presentation" width="600" cellpadding="0" cellspacing="0" '
    'style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;">'
)
_FOOTER = (
    '<tr><td style="padding:20px;text-align:center;color:#9aa0a6;font-size:12px;">'
    "<p style=\"margin:0;\">Community Chat &middot; you're getting this because you turned on "
    'email notifications.<br>Manage or stop them in Settings.</p></td></tr></table></td></tr></table></body></html>'
)


def _header_html(title: str) -> str:
    return (
        '<tr><td style="padding:20px 28px 12px;"><div style="font-size:18px;font-weight:bold;">'
        f"{_esc(title)}</div></td></tr>"
    )


def _body_html(text: str) -> str:
    return f'<tr><td style="padding:0 28px 8px;font-size:15px;line-height:1.5;">{_esc(text)}</td></tr>'


def _button_html(label: str, url: str) -> str:
    return (
        '<tr><td style="padding:16px 28px;"><a href="'
        f'{_esc(url)}" target="_blank" style="display:inline-block;background:#4f46e5;color:#ffffff;'
        'text-decoration:none;padding:12px 22px;border-radius:8px;font-size:14px;font-weight:bold;">'
        f"{_esc(label)}</a></td></tr>"
    )


def _render(title: str, body: str, button_label: str | None = None, button_url: str | None = None) -> str:
    parts = [_BASE_CSS, _header_html(title), _body_html(body)]
    if button_label and button_url:
        parts.append(_button_html(button_label, button_url))
    parts.append(_FOOTER)
    return "".join(parts)


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
    snippet = (text or "").strip() or "(attachment)"
    if room_label:
        intro = f"<p style='margin:0 0 12px;color:#5f6368;font-size:13px;'>{_esc(room_label)}</p>"
    else:
        intro = ""
    body = (
        f"{intro}<p style='margin:0 0 4px;font-weight:bold;'>{_esc(sender_name)}</p>"
        f"<p style='margin:0;'>{_esc(snippet)}</p>"
    )
    return (
        _BASE_CSS
        + _header_html("New message")
        + f'<tr><td style="padding:0 28px 8px;">{body}</td></tr>'
        + _button_html("Open Community Chat", app_origin() + "/")
        + _FOOTER
    )


def invite_html(recipient_name: str, code: str, link: str, family_name: str | None) -> str:
    fam = f"<p style='margin:12px 0 0;color:#5f6368;font-size:13px;'>Family: {_esc(family_name)}</p>" if family_name else ""
    body = (
        f"<p style='margin:0;'>Hey {_esc(recipient_name)}, you've been invited to join Community Chat.</p>"
        f"<p style='margin:16px 0 4px;font-size:13px;color:#5f6368;'>Invite code</p>"
        f"<p style='margin:0 0 8px;font-family:monospace;font-size:20px;font-weight:bold;letter-spacing:0.1em;'>"
        f"{_esc(code)}</p>{fam}"
    )
    return (
        _BASE_CSS
        + _header_html("You're invited")
        + f'<tr><td style="padding:0 28px 8px;">{body}</td></tr>'
        + _button_html("Join Community Chat", link)
        + _FOOTER
    )


def password_reset_html(recipient_name: str, password: str, handle: str) -> str:
    body = (
        f"<p style='margin:0 0 12px;'>Hey {_esc(recipient_name)}, an administrator reset the password "
        f"for your Community Chat account ({_esc(handle)}). Sign in with your username and the "
        "temporary password below.</p>"
        f"<p style='margin:0 0 4px;font-size:13px;color:#5f6368;'>Temporary password</p>"
        f"<p style='margin:0 0 8px;font-family:monospace;font-size:20px;font-weight:bold;letter-spacing:0.05em;'>"
        f"{_esc(password)}</p>"
        f"<p style='margin:8px 0 0;font-size:13px;color:#5f6368;'>"
        "We recommend changing it to something you remember after you sign in.</p>"
    )
    return (
        _BASE_CSS
        + _header_html("Your Community Chat password was reset")
        + f'<tr><td style="padding:0 28px 8px;">{body}</td></tr>'
        + _button_html("Sign in", app_origin() + "/login")
        + _FOOTER
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
    body = f"<p style='margin:0;'>Welcome{f' {name}' if name else ''}! You've joined Community Chat.</p>"
    return (
        _BASE_CSS
        + _header_html("Welcome to Community Chat")
        + f'<tr><td style="padding:0 28px 8px;">{body}</td></tr>'
        + _button_html("Open the app", app_origin() + "/")
        + _FOOTER
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
