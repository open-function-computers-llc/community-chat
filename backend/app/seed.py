"""Idempotent database seeder + lightweight migrations.

Creates the initial admin user on first boot so the app can be joined via
invites only. Subsequent runs are no-ops (never overwrites an existing admin).

Also runs a small set of forward-migrations for existing SQLite databases so
new columns/tables are added without dropping data.

Configuration (all optional, via environment):
  ADMIN_HANDLE   -- admin username, default "admin"
  ADMIN_PASSWORD -- admin password. If unset, a random one is generated and
                    printed to the log exactly once (when the admin is created).
  ADMIN_NAME     -- admin display name, default "Admin"
"""
import logging
import os
import secrets
import string

from sqlalchemy import inspect, text

from .db import Base, SessionLocal, engine
from .core.security import hash_password
from .models import User

logger = logging.getLogger("chat.seed")

ALPHABET = string.ascii_uppercase + string.digits


def generate_password(length: int = 16) -> str:
    return "".join(secrets.choice(ALPHABET + string.ascii_lowercase) for _ in range(length))


def _migrate_sqlite() -> None:
    """Add any missing columns to existing tables (SQLite has no ALTER ADD IF
    NOT EXISTS, so we inspect first). No-op on a fresh DB.
    """
    if not engine.url.drivername.startswith("sqlite"):
        return
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    db = SessionLocal()
    try:
        # users.family_id
        if "users" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("users")}
            if "family_id" not in cols:
                # The families table already exists (create_all ran first), so the
                # inline FK reference is safe.
                db.execute(
                    text("ALTER TABLE users ADD COLUMN family_id INTEGER REFERENCES families(id)")
                )
                logger.info("Migration: added users.family_id")
        # invites.family_id
        if "invites" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("invites")}
            if "family_id" not in cols:
                db.execute(
                    text("ALTER TABLE invites ADD COLUMN family_id INTEGER REFERENCES families(id)")
                )
                logger.info("Migration: added invites.family_id")
        # families.avatar_url
        if "families" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("families")}
            if "avatar_url" not in cols:
                db.execute(text("ALTER TABLE families ADD COLUMN avatar_url TEXT"))
                logger.info("Migration: added families.avatar_url")
        # users.is_admin
        if "users" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("users")}
            if "is_admin" not in cols:
                db.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
                # Backfill: the seeded admin (by handle) becomes the admin.
                admin_handle = os.environ.get("ADMIN_HANDLE", "admin").strip().lower()
                db.execute(
                    text("UPDATE users SET is_admin = 1 WHERE lower(handle) = :h"),
                    {"h": admin_handle},
                )
                logger.info("Migration: added users.is_admin (backfilled admin '%s')", admin_handle)
            if "joined_via_invite_id" not in cols:
                db.execute(
                    text("ALTER TABLE users ADD COLUMN joined_via_invite_id INTEGER REFERENCES invites(id)")
                )
                logger.info("Migration: added users.joined_via_invite_id")
        # room_messages.edited_at
        if "room_messages" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("room_messages")}
            if "edited_at" not in cols:
                db.execute(text("ALTER TABLE room_messages ADD COLUMN edited_at DATETIME"))
                logger.info("Migration: added room_messages.edited_at")
        # reactions: swap the old user-to-user dm_message_id for room_message_id.
        # This is a clean break from member-to-member DMs to family rooms; old
        # dm reaction rows are orphaned and dropped.
        if "reactions" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("reactions")}
            if "room_message_id" not in cols:
                db.execute(
                    text("ALTER TABLE reactions ADD COLUMN room_message_id INTEGER REFERENCES room_messages(id)")
                )
                logger.info("Migration: added reactions.room_message_id")
            if "dm_message_id" in cols:
                db.execute(
                    text("DELETE FROM reactions WHERE dm_message_id IS NOT NULL")
                )
                # SQLite drops a column by rebuilding the table (3.35+).
                try:
                    db.execute(text("ALTER TABLE reactions DROP COLUMN dm_message_id"))
                    logger.info("Migration: dropped reactions.dm_message_id")
                except Exception:
                    logger.warning("Migration: could not drop reactions.dm_message_id (kept)")
        db.commit()
    finally:
        db.close()


def seed_admin() -> None:
    """Create tables + run migrations + ensure the admin user exists."""
    # create_all makes any missing tables (e.g. families) without touching
    # existing ones, so it's safe to run before the column migration.
    Base.metadata.create_all(engine)
    _migrate_sqlite()

    import os

    handle = os.environ.get("ADMIN_HANDLE", "admin").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    display_name = os.environ.get("ADMIN_NAME", "Admin").strip() or "Admin"
    generated = False
    if not password:
        password = generate_password()
        generated = True

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.handle == handle).first()
        if existing is not None:
            logger.info("Admin '%s' already exists, skipping seed", handle)
            return

        admin = User(
            handle=handle,
            password_hash=hash_password(password),
            display_name=display_name,
            is_active=True,
            is_admin=True,
        )
        db.add(admin)
        db.commit()
        logger.info("Seeded admin user '%s'", handle)
        if generated:
            logger.warning(
                "Generated admin password: %s (set ADMIN_PASSWORD to avoid this)",
                password,
            )
    finally:
        db.close()
