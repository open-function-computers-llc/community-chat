"""Database engine, session factory, and ORM models. Single SQLite file by default.

Set DATABASE_URL=sqlite:///:memory: for throwaway in-memory databases (tests).
"""
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    ForeignKeyConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR / 'chat.db'}")

_IS_MEMORY = DATABASE_URL in ("sqlite:///:memory:", "sqlite://")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
_engine_kwargs = {"connect_args": connect_args}
if _IS_MEMORY:
    # A single shared connection so every session sees the same in-memory DB.
    _engine_kwargs["poolclass"] = StaticPool
engine = create_engine(DATABASE_URL, **_engine_kwargs)
# expire_on_commit=False: our request handlers own the rows they write within
# a request, so re-SELECTing every committed object back (SQLAlchemy's
# default) is pure overhead — and on a single-connection SQLite pool it also
# adds a needless post-commit read window. Attribute values set/flushed
# before the commit stay populated and are returned straight from the
# identity map.
SessionLocal = sessionmaker(
    autocommit=False, autoflush=False, expire_on_commit=False, bind=engine
)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    """Serialize a datetime as an ISO-8601 UTC string ending in 'Z'.

    Timestamps are stored UTC (tz-aware via utcnow). We emit an explicit Z
    suffix so the browser always parses the value as UTC and can localize it
    to the viewer's timezone, rather than guessing. A naive datetime is
    coerced to UTC first so the output is unambiguous.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    handle: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    family_id: Mapped[int | None] = mapped_column(
        ForeignKey("families.id", use_alter=True), nullable=True
    )
    joined_via_invite_id: Mapped[int | None] = mapped_column(
        ForeignKey("invites.id", use_alter=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    family: Mapped["Family | None"] = relationship("Family", foreign_keys=[family_id], back_populates="members")
    invites_used: Mapped[list["Invite"]] = relationship("Invite", foreign_keys="Invite.user_id")
    push_subscriptions: Mapped[list["PushSubscription"]] = relationship(
        "PushSubscription", foreign_keys="PushSubscription.user_id", cascade="all, delete-orphan"
    )
    channel_settings: Mapped[list["ChannelNotification"]] = relationship(
        "ChannelNotification", cascade="all, delete-orphan"
    )


class Family(Base):
    __tablename__ = "families"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(280), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    members: Mapped[list["User"]] = relationship("User", foreign_keys="User.family_id", back_populates="family")
    rooms: Mapped[list["DmRoom"]] = relationship(
        "DmRoom", secondary="dm_room_families", back_populates="families"
    )


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    family_id: Mapped[int | None] = mapped_column(ForeignKey("families.id"), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    family: Mapped["Family | None"] = relationship("Family")


class PushSubscription(Base):
    """A Web Push (VAPID) subscription for one user, per device/browser.

    ``endpoint`` is the push-service URL (unique per subscription). ``keys``
    holds the user's public key + auth secret (from pushManager.subscribe).
    A user can have several (phone + laptop); we fan a push out to all of
    them and drop any whose endpoint the push service rejects (404/410).
    """

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id], back_populates="push_subscriptions")


class ChannelNotification(Base):
    """Per-user, per-channel notification matrix ("Channel Summary").

    room_id NULL = the Group Chat channel (every member is in it); otherwise
    a family room. A row exists only once the user has saved settings for
    that channel — absence means the defaults below, so new channels (and
    existing users) start: immediate push on (subject to having a subscribed
    device), immediate email off, and all digests off (summaries are
    strictly opt-in).
    """
    __tablename__ = "channel_notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "room_id", name="unique_user_channel_room"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    room_id: Mapped[int | None] = mapped_column(
        ForeignKey("dm_rooms.id", ondelete="CASCADE"), nullable=True
    )

    immediate_email: Mapped[bool] = mapped_column(Boolean, default=False)
    immediate_push: Mapped[bool] = mapped_column(Boolean, default=True)
    nightly_email: Mapped[bool] = mapped_column(Boolean, default=False)
    nightly_push: Mapped[bool] = mapped_column(Boolean, default=False)
    weekly_email: Mapped[bool] = mapped_column(Boolean, default=False)
    weekly_push: Mapped[bool] = mapped_column(Boolean, default=False)


CHANNEL_SETTING_FIELDS = (
    "immediate_email",
    "immediate_push",
    "nightly_email",
    "nightly_push",
    "weekly_email",
    "weekly_push",
)

# What a user gets on a channel they've never configured: immediate pushes
# on (for devices they registered), all email and all digests off.
CHANNEL_DEFAULTS = {
    "immediate_email": False,
    "immediate_push": True,
    "nightly_email": False,
    "nightly_push": False,
    "weekly_email": False,
    "weekly_push": False,
}


class DigestRun(Base):
    """Last-run marker for the Channel Summary scheduler (restart-safe: a
    process that comes back after its slot must not double-send)."""
    __tablename__ = "digest_runs"

    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_run_date: Mapped[str] = mapped_column(String(10), nullable=False)


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    owner: Mapped["User"] = relationship("User", foreign_keys=[owner_id])


class GroupMessage(Base):
    __tablename__ = "group_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reply_to_id: Mapped[int | None] = mapped_column(ForeignKey("group_messages.id"), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    author: Mapped["User"] = relationship("User", foreign_keys=[author_id])
    reply_to: Mapped["GroupMessage | None"] = relationship("GroupMessage", remote_side=[id], foreign_keys=[reply_to_id])


class DmRoom(Base):
    """A private chat shared between two or more families. Members of any
    family attached to the room can read and write messages in it."""
    __tablename__ = "dm_rooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    families: Mapped[list["Family"]] = relationship(
        "Family",
        secondary="dm_room_families",
        back_populates="rooms",
    )
    messages: Mapped[list["RoomMessage"]] = relationship(
        "RoomMessage", back_populates="room", cascade="all, delete-orphan"
    )


class DmRoomFamily(Base):
    """Pivot: which families belong to a room."""
    __tablename__ = "dm_room_families"
    __table_args__ = (
        UniqueConstraint("room_id", "family_id", name="unique_room_family"),
    )

    room_id: Mapped[int] = mapped_column(
        ForeignKey("dm_rooms.id", ondelete="CASCADE"), primary_key=True
    )
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.id", ondelete="CASCADE"), primary_key=True
    )


class RoomMessage(Base):
    __tablename__ = "room_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("dm_rooms.id"), nullable=False)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    room: Mapped["DmRoom"] = relationship("DmRoom", back_populates="messages")
    sender: Mapped["User"] = relationship("User", foreign_keys=[sender_id])


class Reaction(Base):
    __tablename__ = "reactions"
    __table_args__ = (
        UniqueConstraint("emoji", "group_message_id", "user_id", name="unique_reaction_group"),
        UniqueConstraint("emoji", "room_message_id", "user_id", name="unique_reaction_room"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    emoji: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    group_message_id: Mapped[int | None] = mapped_column(ForeignKey("group_messages.id"), nullable=True)
    room_message_id: Mapped[int | None] = mapped_column(ForeignKey("room_messages.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship("User")


def create_all() -> None:
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
