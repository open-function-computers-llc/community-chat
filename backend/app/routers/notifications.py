"""Channel Summary notification settings API.

Every user always has one channel — Group Chat — they can never leave; each
family room their family belongs to is another. For every channel the user
picks a matrix: immediate / nightly summary / weekly summary, each via
email and/or push. No stored row means the model defaults (push immediate
on, everything else off — digests are strictly opt-in).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..db import CHANNEL_DEFAULTS, CHANNEL_SETTING_FIELDS, get_db
from ..models import ChannelNotification, DmRoom, Family, User
from ..core.security import get_current_user

router = APIRouter()

GROUP_CHANNEL_NAME = "Group Chat"


class ChannelSettingsUpdate(BaseModel):
    room_id: int | None = None  # None = the group channel
    immediate_email: bool
    immediate_push: bool
    nightly_email: bool
    nightly_push: bool
    weekly_email: bool
    weekly_push: bool


def _get_row(db, user_id: int, room_id: int | None) -> ChannelNotification | None:
    cond = (
        ChannelNotification.room_id.is_(None)
        if room_id is None
        else ChannelNotification.room_id == room_id
    )
    return db.execute(
        select(ChannelNotification).where(
            ChannelNotification.user_id == user_id, cond
        )
    ).scalar_one_or_none()


def _settings_of(row: ChannelNotification | None) -> dict:
    return {
        field: (getattr(row, field) if row is not None else default)
        for field, default in CHANNEL_DEFAULTS.items()
    }


def _room_or_403(db, user: User, room_id: int) -> DmRoom:
    room = db.get(DmRoom, room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    if user.family_id is None or not any(f.id == user.family_id for f in room.families):
        raise HTTPException(status_code=403, detail="You are not a member of this room")
    return room


@router.get("/channels")
async def list_channels(user: User = Depends(get_current_user), db=Depends(get_db)):
    """The user's channels with their current (effective) settings: the
    group channel always, then every family room their family is part of —
    new rooms appear here automatically."""
    channels = [
        {
            "room_id": None,
            "name": GROUP_CHANNEL_NAME,
            "settings": _settings_of(_get_row(db, user.id, None)),
        }
    ]
    if user.family_id is not None:
        rooms = db.execute(
            select(DmRoom).join(DmRoom.families).where(Family.id == user.family_id)
        ).scalars().all()
        for room in rooms:
            names = [f.name for f in room.families]
            channels.append(
                {
                    "room_id": room.id,
                    "name": " & ".join(names) if names else "Family chat",
                    "settings": _settings_of(_get_row(db, user.id, room.id)),
                }
            )
    return channels


@router.put("/channels")
async def update_channel(
    req: ChannelSettingsUpdate,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    if req.room_id is not None:
        _room_or_403(db, user, req.room_id)
    row = _get_row(db, user.id, req.room_id)
    if row is None:
        row = ChannelNotification(user_id=user.id, room_id=req.room_id)
        db.add(row)
    for field in CHANNEL_SETTING_FIELDS:
        setattr(row, field, getattr(req, field))
    db.commit()
    return {"ok": True, "room_id": req.room_id, "settings": _settings_of(row)}
