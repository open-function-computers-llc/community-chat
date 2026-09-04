import asyncio
import os
import secrets

import string
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File as FileField, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import delete, func, select

from .upload_utils import IMAGE_CONTENT_TYPES, delete_file, process_avatar, public_url

from ..db import SessionLocal, get_db
from ..models import (
    DMSettings,
    File,
    GroupMessage,
    Invite,
    Reaction,
    RoomMessage,
    User,
)
from ..core.security import create_access_token, get_current_user, hash_password, require_admin

router = APIRouter()


def public_user(user: User) -> dict:
    return {
        "id": user.id,
        "handle": user.handle,
        "display_name": user.display_name or user.handle,
        "email": user.email,
        "phone": user.phone,
        "avatar_url": user.avatar_url,
        "bio": user.bio,
        "family_id": user.family_id,
        "family_name": user.family.name if user.family else None,
        "is_admin": bool(user.is_admin),
        "created_at": user.created_at.isoformat(),
    }


class RegisterRequest(BaseModel):
    invite_code: str
    handle: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("handle")
    @classmethod
    def valid_handle(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or not all(c in string.ascii_letters + string.digits + "._-" for c in v):
            raise ValueError("handle may only contain letters, numbers, and . _ -")
        return v

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, v: str | None) -> str | None:
        return v.strip() if v else v


class LoginRequest(BaseModel):
    handle: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: dict
    is_new_user: bool = False


@router.post("/register", response_model=LoginResponse)
async def register(req: RegisterRequest, db=Depends(get_db)):
    if db.execute(select(User).where(User.handle == req.handle)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Handle already taken")

    invite = db.execute(select(Invite).where(Invite.code == req.invite_code.strip())).scalar_one_or_none()
    if invite is None or invite.used_at is not None or invite.times_used >= invite.max_uses:
        raise HTTPException(status_code=403, detail="Invalid or expired invite code")

    user = User(
        handle=req.handle,
        display_name=req.display_name or req.handle,
        email=req.email,
        phone=req.phone,
        is_active=True,
        password_hash=hash_password(req.password),
        family_id=invite.family_id,
        joined_via_invite_id=invite.id,
    )
    db.add(user)
    db.flush()

    invite.times_used += 1
    invite.created_by = invite.created_by or user.id
    if invite.times_used >= invite.max_uses:
        invite.used_at = datetime.now(timezone.utc)
    db.add(user)
    db.commit()

    # Best-effort: welcome the newcomer (their own address) and tell the
    # person who created the invite. Fire-and-forget; never blocks the response.
    from .email import fire_invite_email, fire_welcome_email
    from ..core.config import app_origin

    link = f"{app_origin()}/login?code={invite.code}"
    family_name = None
    if invite.family_id:
        from ..models import Family

        fam = db.get(Family, invite.family_id)
        family_name = fam.name if fam else None
    if req.email:
        fire_welcome_email(req.email, user.display_name or user.handle)
    if invite.created_by:
        creator = db.get(User, invite.created_by)
        if creator and creator.email and "@" in creator.email:
            fire_invite_email(
                creator.email,
                user.display_name or user.handle,
                req.handle,
                link,
                family_name,
            )

    token = create_access_token(user.id)
    return LoginResponse(token=token, user=public_user(user), is_new_user=True)


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, db=Depends(get_db)):
    user = db.execute(select(User).where(User.handle == req.handle.strip().lower())).scalar_one_or_none()
    if user is None or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid handle or password")
    from ..core.security import verify_password

    if not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid handle or password")
    token = create_access_token(user.id)
    return LoginResponse(token=token, user=public_user(user))


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return public_user(user)


@router.get("/users")
async def list_users(user: User = Depends(get_current_user), db=Depends(get_db)):
    rows = db.execute(select(User).where(User.is_active == True).order_by(User.display_name)).scalars().all()
    return [public_user(u) for u in rows]


@router.get("/users/{user_id}")
async def get_user(user_id: int, user: User = Depends(get_current_user), db=Depends(get_db)):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    return public_user(target)


@router.get("/admin/users")
async def admin_list_users(
    user: User = Depends(require_admin), db=Depends(get_db)
):
    """Admin view of members with activity stats, for managing the roster."""
    rows = (
        db.execute(select(User).where(User.is_active == True).order_by(User.display_name))
        .scalars()
        .all()
    )
    if not rows:
        return []

    uids = [u.id for u in rows]
    # Group-message counts and last activity, per author.
    group_stats = {
        r[0]: (r[1], r[2])
        for r in db.execute(
            select(
                GroupMessage.author_id,
                func.count(GroupMessage.id),
                func.max(GroupMessage.created_at),
            )
            .where(GroupMessage.author_id.in_(uids))
            .group_by(GroupMessage.author_id)
        ).all()
    }
    # Room messages sent, per sender.
    room_sent = {
        r[0]: r[1]
        for r in db.execute(
            select(RoomMessage.sender_id, func.count(RoomMessage.id))
            .where(RoomMessage.sender_id.in_(uids))
            .group_by(RoomMessage.sender_id)
        ).all()
    }
    last_room = {
        r[0]: r[1]
        for r in db.execute(
            select(RoomMessage.sender_id, func.max(RoomMessage.created_at))
            .where(RoomMessage.sender_id.in_(uids))
            .group_by(RoomMessage.sender_id)
        ).all()
    }

    out = []
    for u in rows:
        base = public_user(u)
        g_count, g_last = group_stats.get(u.id, (0, None))
        last_active = None
        for ts in (g_last, last_room.get(u.id)):
            if ts is not None and (last_active is None or ts > last_active):
                last_active = ts
        base["group_message_count"] = g_count
        base["room_message_count"] = room_sent.get(u.id, 0)
        base["last_active_at"] = last_active.isoformat() if last_active else None
        out.append(base)
    return out


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    user: User = Depends(require_admin),
    db=Depends(get_db),
):
    """Admin hard-deletes a member and their content.

    Removes their group messages, DMs (sent or received), reactions, and
    uploaded files, then the user row. Orphaned reply_to links and invites
    are cleared so nothing dangles. The admin cannot delete themselves.
    """
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")

    uid = target.id
    # Reactions they cast.
    db.execute(delete(Reaction).where(Reaction.user_id == uid))
    # Room messages they sent.
    db.execute(delete(RoomMessage).where(RoomMessage.sender_id == uid))
    # Group messages they wrote. Clear any inbound replies first so other
    # users' messages don't point at rows we're about to remove.
    from .upload_utils import delete_file

    their_msg_ids = [
        r[0]
        for r in db.execute(select(GroupMessage.id).where(GroupMessage.author_id == uid)).all()
    ]
    if their_msg_ids:
        for other in db.execute(
            select(GroupMessage).where(GroupMessage.reply_to_id.in_(their_msg_ids))
        ).scalars().all():
            other.reply_to_id = None
        db.execute(delete(GroupMessage).where(GroupMessage.author_id == uid))
    # Files they uploaded.
    files = db.execute(select(File).where(File.owner_id == uid)).scalars().all()

    for f in files:
        delete_file(f"/uploads/{f.storage_name}")
        db.delete(f)
    # DM settings.
    db.execute(delete(DMSettings).where(DMSettings.user_id == uid))
    # Clear invite links that point at them (keep the invites themselves).
    for inv in db.execute(select(Invite).where(Invite.created_by == uid)).scalars().all():
        inv.created_by = None
    # The user row.
    db.delete(target)
    db.commit()
    return {"ok": True}


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    bio: str | None = Field(default=None, max_length=500)
    avatar_url: str | None = Field(default=None, max_length=512)


@router.patch("/me/profile")
async def update_profile(req: ProfileUpdate, user: User = Depends(get_current_user), db=Depends(get_db)):
    for field_name in ("display_name", "email", "phone", "bio", "avatar_url"):
        value = getattr(req, field_name)
        if value is not None:
            setattr(user, field_name, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(user)
    return public_user(user)


@router.post("/me/avatar")
async def upload_my_avatar(
    file: UploadFile = FileField(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Upload (or replace) the current user's avatar.

    The image is center-cropped to a square, resized to 500x500, and stored
    as WebP (avatars are only shown as small circular previews). The previous
    file is deleted from disk so no full-resolution revision is retained.
    """
    if file.content_type not in IMAGE_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported file type")

    content = await file.read()
    storage_name, _size = await asyncio.to_thread(process_avatar, content, prefix=f"user{user.id}")
    # Replace in place: drop the old avatar file, point at the new one.
    delete_file(user.avatar_url)
    user.avatar_url = public_url(storage_name)
    db.commit()
    db.refresh(user)
    return public_user(user)


class PasswordUpdate(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6, max_length=128)


@router.post("/me/password")
async def change_password(req: PasswordUpdate, user: User = Depends(get_current_user), db=Depends(get_db)):
    from ..core.security import verify_password

    if not user.password_hash or not verify_password(req.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"ok": True}


@router.get("/me/settings")
async def get_settings(user: User = Depends(get_current_user), db=Depends(get_db)):
    settings = user.dm_settings
    if settings is None:
        settings = DMSettings(user_id=user.id)
        db.add(settings)
        db.commit()
    return {
        "do_not_disturb": settings.do_not_disturb,
        "notify_mentions": settings.notify_mentions,
        "notify_replies": settings.notify_replies,
        "email_notifications": settings.email_notifications,
    }


class SettingsUpdate(BaseModel):
    do_not_disturb: bool
    notify_mentions: bool
    notify_replies: bool
    email_notifications: bool


@router.put("/me/settings")
async def put_settings(req: SettingsUpdate, user: User = Depends(get_current_user), db=Depends(get_db)):
    settings = user.dm_settings
    if settings is None:
        settings = DMSettings(user_id=user.id)
        db.add(settings)
    settings.do_not_disturb = req.do_not_disturb
    settings.notify_mentions = req.notify_mentions
    settings.notify_replies = req.notify_replies
    settings.email_notifications = req.email_notifications
    db.commit()
    return {
        "do_not_disturb": settings.do_not_disturb,
        "notify_mentions": settings.notify_mentions,
        "notify_replies": settings.notify_replies,
        "email_notifications": settings.email_notifications,
    }


@router.post("/me/logout")
async def logout(user: User = Depends(get_current_user)):
    return {"ok": True}
