import asyncio
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from ..db import SessionLocal, get_db, iso_utc
from ..models import Family, Invite, User
from ..core.config import app_origin, email_enabled
from ..core.security import get_current_user, require_admin

router = APIRouter()

ALPHABET = string.ascii_uppercase + string.digits
CODE_LENGTH = 8


class InviteCreateRequest(BaseModel):
    max_uses: int = Field(default=1, ge=1, le=100)
    note: str | None = Field(default=None, max_length=200)
    family_id: int | None = Field(default=None)


class InviteEmailRequest(BaseModel):
    emails: list[EmailStr] = Field(min_length=1, max_length=20)


class InviteOut(BaseModel):
    id: int
    code: str
    max_uses: int
    times_used: int
    is_active: bool
    note: str | None
    family_id: int | None = None
    family_name: str | None = None
    created_at: str
    used_at: str | None


def generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def serialize(invite: Invite, db) -> InviteOut:
    family_name = None
    if invite.family_id is not None:
        fam = db.get(Family, invite.family_id)
        family_name = fam.name if fam else None
    return InviteOut(
        id=invite.id,
        code=invite.code,
        max_uses=invite.max_uses,
        times_used=invite.times_used,
        is_active=invite.used_at is None and invite.times_used < invite.max_uses,
        note=invite.note,
        family_id=invite.family_id,
        family_name=family_name,
        created_at=iso_utc(invite.created_at),
        used_at=iso_utc(invite.used_at),
    )


@router.get("", response_model=list[InviteOut])
async def list_invites(user: User = Depends(get_current_user), db=Depends(get_db)):
    if user.is_admin:
        rows = db.execute(select(Invite).order_by(Invite.created_at.desc())).scalars().all()
    else:
        # Non-admins only see the invite that brought them in.
        rows = [
            db.get(Invite, user.joined_via_invite_id)
            if user.joined_via_invite_id is not None
            else None
        ]
        rows = [r for r in rows if r is not None]
    return [serialize(r, db) for r in rows]


@router.post("", response_model=InviteOut, status_code=201)
async def create_invite(
    req: InviteCreateRequest, user: User = Depends(require_admin), db=Depends(get_db)
):
    # If a family was specified, it must exist.
    family_id = None
    if req.family_id is not None:
        family = db.get(Family, req.family_id)
        if family is None:
            raise HTTPException(status_code=404, detail="Family not found")
        family_id = family.id

    for _ in range(25):
        code = generate_code()
        existing = db.execute(select(Invite).where(Invite.code == code)).scalar_one_or_none()
        if existing is None:
            break
    else:
        raise HTTPException(status_code=500, detail="Could not generate unique invite code")

    invite = Invite(
        code=code,
        max_uses=req.max_uses,
        times_used=0,
        created_by=user.id,
        note=req.note,
        family_id=family_id,
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return serialize(invite, db)


@router.post("/{invite_id}/email")
async def email_invite(
    invite_id: int,
    req: InviteEmailRequest,
    user: User = Depends(require_admin),
    db=Depends(get_db),
):
    """Admin emails the invite's join link to one or more addresses using the
    standard invite email template (same one sent when an invite is created
    with a recipient). Duplicate addresses are collapsed; each send is
    best-effort, and per-address failures are reported back so the admin can
    retry or hand-deliver those."""
    if not email_enabled():
        raise HTTPException(
            status_code=400,
            detail=(
                "Email is not configured — set SMTP_HOST and SMTP_FROM_ADDRESS "
                "in the server's env file (verify with: manage.py test-email)."
            ),
        )

    invite = db.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.used_at is not None or invite.times_used >= invite.max_uses:
        raise HTTPException(
            status_code=400,
            detail="This invite has already been used up — create a new one to email it.",
        )

    from .email import invite_html, send_email

    link = f"{app_origin()}/login?code={invite.code}"
    family_name = None
    if invite.family_id:
        fam = db.get(Family, invite.family_id)
        family_name = fam.name if fam else None
    html = invite_html("there", invite.code, link, family_name)

    # De-dupe while preserving order.
    addrs = list(dict.fromkeys(req.emails))
    sent: list[str] = []
    failed: list[str] = []
    for addr in addrs:
        # send_email is blocking + best-effort (never raises); run it off the
        # event loop so a slow SMTP round-trip doesn't stall the server.
        ok = await asyncio.to_thread(
            send_email, "You're invited to Community Chat", html, addr
        )
        (sent if ok else failed).append(addr)
    return {"ok": not failed, "sent": len(sent), "failed": failed, "total": len(addrs)}


@router.delete("/{invite_id}")
async def revoke_invite(
    invite_id: int, user: User = Depends(require_admin), db=Depends(get_db)
):
    invite = db.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.times_used >= invite.max_uses or invite.used_at is not None:
        db.delete(invite)
    else:
        invite.max_uses = 0
        invite.used_at = invite.created_at
    db.commit()
    return {"ok": True}
