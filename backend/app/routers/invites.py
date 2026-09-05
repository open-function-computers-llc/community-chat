import secrets
import string

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import SessionLocal, get_db, iso_utc
from ..models import Family, Invite, User
from ..core.security import get_current_user, require_admin

router = APIRouter()

ALPHABET = string.ascii_uppercase + string.digits
CODE_LENGTH = 8


class InviteCreateRequest(BaseModel):
    max_uses: int = Field(default=1, ge=1, le=100)
    note: str | None = Field(default=None, max_length=200)
    family_id: int | None = Field(default=None)


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
