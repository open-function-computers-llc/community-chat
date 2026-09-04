import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from ..db import get_db
from ..models import DmRoom, Family, Reaction, RoomMessage, User
from ..core.security import get_current_user
from ..ws import hub
from .push import room_push_task
from .email import room_email_task

router = APIRouter()


def _family_public(db, f: Family) -> dict:
    member_count = db.query(User).filter(User.family_id == f.id, User.is_active == True).count()
    return {
        "id": f.id,
        "name": f.name,
        "avatar_url": f.avatar_url,
        "member_count": member_count,
    }


def _is_member(db, user: User, room: DmRoom) -> bool:
    if user.family_id is None:
        return False
    return any(f.id == user.family_id for f in room.families)


def _room_member_ids(db, room: DmRoom) -> set[int]:
    """All active user ids belonging to any family attached to this room."""
    fam_ids = [f.id for f in room.families]
    if not fam_ids:
        return set()
    return {
        r[0]
        for r in db.execute(
            select(User.id).where(User.family_id.in_(fam_ids), User.is_active == True)
        ).all()
    }


def _message_payload(db, msg: RoomMessage) -> dict:
    return {
        "id": msg.id,
        "room_id": msg.room_id,
        "sender": {
            "id": msg.sender.id,
            "handle": msg.sender.handle,
            "display_name": msg.sender.display_name or msg.sender.handle,
            "avatar_url": msg.sender.avatar_url,
            "family_id": msg.sender.family_id,
        },
        "text": msg.text,
        "file_url": msg.file_url,
        "file_name": msg.file_name,
        "file_content_type": msg.file_content_type,
        "created_at": msg.created_at.isoformat(),
        "reactions": _reactions(db, msg.id),
    }


def _reactions(db, room_message_id: int) -> list[dict]:
    rows = (
        db.execute(
            select(Reaction, User)
            .where(Reaction.room_message_id == room_message_id)
            .join(User, Reaction.user_id == User.id)
        )
        .all()
    )
    grouped: dict[str, list[int]] = {}
    for reaction, author in rows:
        grouped.setdefault(reaction.emoji, []).append(author.id)
    return [
        {"emoji": emoji, "user_ids": uids, "count": len(uids)}
        for emoji, uids in grouped.items()
    ]


def _get_room_or_404(db, room_id: int) -> DmRoom:
    room = db.get(DmRoom, room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


@router.get("/rooms")
async def list_rooms(user: User = Depends(get_current_user), db=Depends(get_db)):
    """List rooms that include the user's family (rooms they can read/write)."""
    if user.family_id is None:
        return []

    rooms = db.execute(
        select(DmRoom).join(DmRoom.families).where(Family.id == user.family_id)
    ).scalars().all()

    last_q = (
        select(RoomMessage.room_id, func.max(RoomMessage.id))
        .group_by(RoomMessage.room_id)
        .subquery()
    )
    last_ids = dict(db.execute(select(last_q)).all())

    result = []
    for room in rooms:
        families = [_family_public(db, f) for f in room.families]
        last_msg = db.get(RoomMessage, last_ids.get(room.id)) if last_ids.get(room.id) else None
        other_families = [f for f in families if f["id"] != user.family_id]
        result.append(
            {
                "id": room.id,
                "families": other_families,
                "families_all": families,
                "last_message": _message_payload(db, last_msg) if last_msg else None,
                "last_at": last_msg.created_at.isoformat() if last_msg else None,
            }
        )
    result.sort(key=lambda c: c["last_at"] or "", reverse=True)
    return result


@router.post("/rooms", status_code=201)
async def open_room(
    data: dict,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Open (or create) the room between the user's family and another family.

    If a room already exists containing both families, it is returned as-is;
    otherwise a new 2-family room is created. Implicit, idempotent.
    """
    if user.family_id is None:
        raise HTTPException(status_code=400, detail="You are not in a family")
    other_family_id = data.get("family_id")
    if other_family_id is None or int(other_family_id) == user.family_id:
        raise HTTPException(status_code=400, detail="Pick a family that is not your own")
    other_family = db.get(Family, int(other_family_id))
    if other_family is None:
        raise HTTPException(status_code=404, detail="Family not found")

    # Find an existing room that contains both families.
    existing = (
        db.execute(
            select(DmRoom)
            .join(DmRoom.families)
            .where(Family.id.in_([user.family_id, other_family.id]))
        )
        .scalars()
        .all()
    )
    room = None
    for candidate in existing:
        fam_ids = {f.id for f in candidate.families}
        if user.family_id in fam_ids and other_family.id in fam_ids and len(candidate.families) == 2:
            room = candidate
            break

    if room is None:
        room = DmRoom(created_by=user.id)
        db.add(room)
        db.commit()
        db.refresh(room)
        from ..db import DmRoomFamily

        db.add(DmRoomFamily(room_id=room.id, family_id=user.family_id))
        db.add(DmRoomFamily(room_id=room.id, family_id=other_family.id))
        db.commit()
        db.refresh(room)

    return {
        "id": room.id,
        "families": [_family_public(db, f) for f in room.families],
    }


@router.get("/rooms/{room_id}")
async def room_history(
    room_id: int,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    room = _get_room_or_404(db, room_id)
    if not _is_member(db, user, room):
        raise HTTPException(status_code=403, detail="Not a member of this room")

    query = select(RoomMessage).where(RoomMessage.room_id == room_id).order_by(RoomMessage.id.desc())
    if before_id is not None:
        query = query.where(RoomMessage.id < before_id)
    rows = db.execute(query.limit(limit)).scalars().all()
    messages = [_message_payload(db, m) for m in rows]
    messages.reverse()
    return {
        "id": room.id,
        "families": [_family_public(db, f) for f in room.families],
        "messages": messages,
    }


@router.post("/rooms/{room_id}", status_code=201)
async def send_room_message(
    room_id: int,
    data: dict,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    room = _get_room_or_404(db, room_id)
    if not _is_member(db, user, room):
        raise HTTPException(status_code=403, detail="Not a member of this room")

    text = (data.get("text") or "").strip()
    if not text and not data.get("file_url"):
        raise HTTPException(status_code=400, detail="Message must contain text or a file")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="Message too long (max 4000 chars)")

    msg = RoomMessage(
        room_id=room_id,
        sender_id=user.id,
        text=text or None,
        file_url=data.get("file_url"),
        file_name=data.get("file_name"),
        file_content_type=data.get("file_content_type"),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    payload = _message_payload(db, msg)

    member_ids = _room_member_ids(db, room)
    asyncio.get_event_loop().create_task(
        hub.send_to_many(member_ids, {"type": "dm.new", "channel": "room", "message": payload}, exclude=user.id)
    )

    # Push notification to the other families in the room (best-effort).
    other_fams = [f.name for f in room.families if f.id != user.family_id]
    fam_label = " & ".join(other_fams) if other_fams else "Family chat"
    push_payload = {
        "title": f"Message from {fam_label}",
        "body": (user.display_name or user.handle) + (f": {text[:140]}" if text else " (attachment)"),
        "tag": f"room-{room.id}-{msg.id}",
        "icon": "/icon-192.png",
        "url": f"/room/{room.id}",
        "channel": "room",
        "room_id": room.id,
    }
    asyncio.get_event_loop().create_task(
        room_push_task(member_ids=member_ids, sender_id=user.id, payload=push_payload)
    )
    # Email notification to opt-in room members (best-effort, background).
    asyncio.get_event_loop().create_task(
        room_email_task(
            member_ids=member_ids,
            sender_id=user.id,
            sender_name=(user.display_name or user.handle),
            text=text,
            room_label=fam_label,
        )
    )
    return payload


def _get_room_message_or_403(db, room_id: int, msg_id: int, user: User) -> RoomMessage:
    room = _get_room_or_404(db, room_id)
    if not _is_member(db, user, room):
        raise HTTPException(status_code=403, detail="Not a member of this room")
    msg = db.get(RoomMessage, msg_id)
    if msg is None or msg.room_id != room_id:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


@router.post("/rooms/{room_id}/messages/{msg_id}/reactions/{emoji}")
async def add_room_reaction(
    room_id: int,
    msg_id: int,
    emoji: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    from .messages import REACTION_EMOJIS

    if emoji not in REACTION_EMOJIS:
        raise HTTPException(status_code=400, detail="Unsupported emoji")
    _get_room_message_or_403(db, room_id, msg_id, user)

    existing = db.execute(
        select(Reaction).where(
            Reaction.room_message_id == msg_id,
            Reaction.user_id == user.id,
            Reaction.emoji == emoji,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(Reaction(emoji=emoji, user_id=user.id, room_message_id=msg_id))
        db.commit()
    user_ids = [
        r[0]
        for r in db.execute(
            select(Reaction.user_id).where(
                Reaction.room_message_id == msg_id, Reaction.emoji == emoji
            )
        ).all()
    ]
    payload = {
        "type": "reaction.changed",
        "channel": "room",
        "room_id": room_id,
        "message_id": msg_id,
        "emoji": emoji,
        "user_ids": user_ids,
        "count": len(user_ids),
    }
    asyncio.get_event_loop().create_task(hub.broadcast(payload))
    return {"emoji": emoji, "user_ids": user_ids, "count": len(user_ids)}


@router.delete("/rooms/{room_id}/messages/{msg_id}/reactions/{emoji}")
async def remove_room_reaction(
    room_id: int,
    msg_id: int,
    emoji: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    _get_room_message_or_403(db, room_id, msg_id, user)
    existing = db.execute(
        select(Reaction).where(
            Reaction.room_message_id == msg_id,
            Reaction.user_id == user.id,
            Reaction.emoji == emoji,
        )
    ).scalar_one_or_none()
    if existing:
        db.delete(existing)
        db.commit()
    user_ids = [
        r[0]
        for r in db.execute(
            select(Reaction.user_id).where(
                Reaction.room_message_id == msg_id, Reaction.emoji == emoji
            )
        ).all()
    ]
    payload = {
        "type": "reaction.changed",
        "channel": "room",
        "room_id": room_id,
        "message_id": msg_id,
        "emoji": emoji,
        "user_ids": user_ids,
        "count": len(user_ids),
    }
    asyncio.get_event_loop().create_task(hub.broadcast(payload))
    return {"emoji": emoji, "removed": existing is not None}
