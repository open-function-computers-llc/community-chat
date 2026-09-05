import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select

from ..db import get_db, iso_utc
from ..models import GroupMessage, Reaction, User
from ..core.security import get_current_user
from ..ws import hub
from .push import group_push_task
from .email import group_email_task

router = APIRouter()

REACTION_EMOJIS = {
    "👍", "❤️", "😂", "😮", "😢", "🙏", "🎉", "👀",
    "🥳", "😍", "🤔", "👎", "🔥", "✨", "💪", "🥰",
}


def reaction_payloads(db, group_message_id: int) -> list[dict]:
    rows = (
        db.execute(
            select(Reaction, User)
            .where(Reaction.group_message_id == group_message_id)
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


def message_payload(db, msg: GroupMessage) -> dict:
    payload = {
        "id": msg.id,
        "author": {
            "id": msg.author.id,
            "handle": msg.author.handle,
            "display_name": msg.author.display_name or msg.author.handle,
            "avatar_url": msg.author.avatar_url,
        },
        "text": msg.text,
        "file_url": msg.file_url,
        "file_name": msg.file_name,
        "file_content_type": msg.file_content_type,
        "reply_to_id": msg.reply_to_id,
        "edited_at": iso_utc(msg.edited_at),
        "created_at": iso_utc(msg.created_at),
        "reactions": reaction_payloads(db, msg.id),
    }
    if msg.reply_to_id:
        original = db.get(GroupMessage, msg.reply_to_id)
        if original is not None:
            payload["reply_to"] = {
                "id": original.id,
                "author": {
                    "id": original.author.id,
                    "handle": original.author.handle,
                    "display_name": original.author.display_name or original.author.handle,
                    "avatar_url": original.author.avatar_url,
                },
                    "text": (original.text or "")[:140],
                    "created_at": iso_utc(original.created_at),
                }
    return payload


@router.get("")
async def list_messages(
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    _user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    query = select(GroupMessage).order_by(desc(GroupMessage.id))
    if before_id is not None:
        query = query.where(GroupMessage.id < before_id)
    rows = db.execute(query.limit(limit)).scalars().all()
    messages = [message_payload(db, msg) for msg in rows]
    messages.reverse()
    return messages


@router.post("", status_code=201)
async def send_message(
    data: dict,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    text = (data.get("text") or "").strip()
    file_url = data.get("file_url")
    reply_to_id = data.get("reply_to_id")

    if not text and not file_url:
        raise HTTPException(status_code=400, detail="Message must contain text or a file")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="Message too long (max 4000 chars)")
    if reply_to_id:
        if db.get(GroupMessage, int(reply_to_id)) is None:
            raise HTTPException(status_code=404, detail="Original message not found")

    msg = GroupMessage(
        author_id=user.id,
        text=text or None,
        file_url=file_url,
        file_name=data.get("file_name"),
        file_content_type=data.get("file_content_type"),
        reply_to_id=int(reply_to_id) if reply_to_id else None,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    payload = message_payload(db, msg)
    asyncio.get_event_loop().create_task(
        hub.broadcast({"type": "message.new", "channel": "group", "message": payload})
    )
    # Push notification to everyone else (best-effort, background).
    push_payload = {
        "title": "New message",
        "body": (user.display_name or user.handle) + (f": {text[:140]}" if text else " (attachment)"),
        "tag": f"group-{msg.id}",
        "icon": "/icon-192.png",
        "url": "/",
        "channel": "group",
    }
    asyncio.get_event_loop().create_task(
        group_push_task(sender_id=user.id, payload=push_payload)
    )
    # Email notification to opt-in users (best-effort, background).
    asyncio.get_event_loop().create_task(
        group_email_task(sender_id=user.id, sender_name=(user.display_name or user.handle), text=text)
    )
    return payload


@router.patch("/{message_id}")
async def edit_message(
    message_id: int,
    data: dict,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    msg = db.get(GroupMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.author_id != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own messages")

    new_text = (data.get("text") or "").strip()
    if not new_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(new_text) > 4000:
        raise HTTPException(status_code=400, detail="Message too long (max 4000 chars)")
    msg.text = new_text
    msg.edited_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    payload = message_payload(db, msg)
    asyncio.get_event_loop().create_task(
        hub.broadcast({"type": "message.edited", "channel": "group", "message": payload})
    )
    return payload


@router.delete("/{message_id}")
async def delete_message(
    message_id: int,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    msg = db.get(GroupMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.author_id != user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own messages")
    message_id = msg.id
    db.delete(msg)
    db.commit()
    asyncio.get_event_loop().create_task(
        hub.broadcast({"type": "message.deleted", "channel": "group", "message_id": message_id})
    )
    return {"ok": True}


@router.post("/{message_id}/reactions/{emoji}")
async def add_reaction(
    message_id: int,
    emoji: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    if emoji not in REACTION_EMOJIS:
        raise HTTPException(status_code=400, detail="Unsupported emoji")
    if db.get(GroupMessage, message_id) is None:
        raise HTTPException(status_code=404, detail="Message not found")
    existing = db.execute(
        select(Reaction).where(
            Reaction.group_message_id == message_id,
            Reaction.user_id == user.id,
            Reaction.emoji == emoji,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(Reaction(emoji=emoji, user_id=user.id, group_message_id=message_id))
        db.commit()
    user_ids = [
        r[0]
        for r in db.execute(
            select(Reaction.user_id).where(
                Reaction.group_message_id == message_id, Reaction.emoji == emoji
            )
        ).all()
    ]
    asyncio.get_event_loop().create_task(
        hub.broadcast(
            {
                "type": "reaction.changed",
                "channel": "group",
                "message_id": message_id,
                "emoji": emoji,
                "user_ids": user_ids,
                "count": len(user_ids),
            }
        )
    )
    return {"emoji": emoji, "user_ids": user_ids, "count": len(user_ids)}


@router.delete("/{message_id}/reactions/{emoji}")
async def remove_reaction(
    message_id: int,
    emoji: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    existing = db.execute(
        select(Reaction).where(
            Reaction.group_message_id == message_id,
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
                Reaction.group_message_id == message_id, Reaction.emoji == emoji
            )
        ).all()
    ]
    asyncio.get_event_loop().create_task(
        hub.broadcast(
            {
                "type": "reaction.changed",
                "channel": "group",
                "message_id": message_id,
                "emoji": emoji,
                "user_ids": user_ids,
                "count": len(user_ids),
            }
        )
    )
    return {"emoji": emoji, "removed": existing is not None}
