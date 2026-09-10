"""Admin CLI for the community-chat backend.

Run inside the app (or container) with the same environment the server uses
(DATA_DIR / DATABASE_URL). It opens the real database via app.db, so it points
at the exact SQLite file the running app uses. This is a maintenance tool for
the same operations the admin UI performs (member/invite/family management),
usable without a browser or a known admin password.

Subcommands:
    list-members          roster + activity stats (+ --all for inactive)
    delete-member <id>    hard-delete a member and their content (--yes to skip confirm)
    set-admin <id|handle> grant/revoke admin (--role admin|member, --yes)
    create-invite         print a shareable invite code (+ --max-uses --note --family)
    list-invites          existing invites (+ --all for expired)
    revoke-invite <id>    revoke/delete an invite (--yes)
    list-families         families with member counts
    create-family <name>  create a family (+ --description)
    assign-member <handle> <family>  put a user in a family (0 to remove)
    reset-admin-password  recover the admin password (--handle --password --generate)
    reset-password [<id|handle>]  reset any user's password (auto-generated;
                                  omit the target to list members + usage)

Every subcommand accepts --json to emit machine-readable output.
"""
import argparse
import getpass
import json
import os
import secrets
import string
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select

# Ensure the database location is settled *before* importing app.db (which
# reads DATA_DIR/DATABASE_URL into module globals at import time). Running this
# directly from the backend dir defaults to ./data/chat.db, matching the repo
# layout; in the container DATA_DIR is already /app/data.
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite:///{Path(__file__).resolve().parent / 'data' / 'chat.db'}",
)

from app.db import Base, SessionLocal, engine, iso_utc  # noqa: E402
from app.core.config import email_enabled  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.routers.email import send_password_reset_email  # noqa: E402
from app.models import (  # noqa: E402
    DMSettings,
    Family,
    File,
    GroupMessage,
    Invite,
    Reaction,
    RoomMessage,
    User,
)

ALPHABET = string.ascii_uppercase + string.digits + string.ascii_lowercase
INVITE_ALPHABET = string.ascii_uppercase + string.digits
INVITE_CODE_LENGTH = 8


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _emit(args, data, pretty) -> int:
    if args.json:
        print(json.dumps(pretty, indent=2))
    else:
        print(data)
    return 0


def _open():
    Base.metadata.create_all(engine)
    return SessionLocal()


def _confirm(prompt: str) -> bool:
    return getpass.getpass(prompt + " [y/N] ").strip().lower() in ("y", "yes")


def _find_user(db, handle_or_id: str) -> User | None:
    """Look a user up by numeric id or by handle."""
    if str(handle_or_id).isdigit():
        return db.get(User, int(handle_or_id))
    return db.query(User).filter(User.handle == str(handle_or_id).strip().lower()).first()


def _find_family(db, name_or_id: str) -> Family | None:
    if str(name_or_id).isdigit():
        return db.get(Family, int(name_or_id))
    return db.query(Family).filter(Family.name == str(name_or_id).strip()).first()


def _generate_password(length: int = 16) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def _gen_invite_code(db) -> str:
    for _ in range(25):
        code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))
        if db.execute(select(Invite).where(Invite.code == code)).scalar_one_or_none() is None:
            return code
    raise RuntimeError("Could not generate a unique invite code")


def _invite_active(inv: Invite) -> bool:
    return inv.used_at is None and inv.times_used < inv.max_uses


def _fmt_ts(dt) -> str | None:
    return iso_utc(dt)


def _public_user(u: User) -> dict:
    return {
        "id": u.id,
        "handle": u.handle,
        "display_name": u.display_name or u.handle,
        "email": u.email,
        "phone": u.phone,
        "is_active": u.is_active,
        "is_admin": bool(u.is_admin),
        "family_name": u.family.name if u.family else None,
        "created_at": _fmt_ts(u.created_at),
    }


def _user_stats(db, uids: list[int]) -> dict:
    """group/room message counts + last activity per user (mirrors the API)."""
    group = {
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
    room = {
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
    out = {}
    for uid in uids:
        g_count, g_last = group.get(uid, (0, None))
        last_active = None
        for ts in (g_last, last_room.get(uid)):
            if ts is not None and (last_active is None or ts > last_active):
                last_active = ts
        out[uid] = {
            "group_message_count": g_count,
            "room_message_count": room.get(uid, 0),
            "last_active_at": _fmt_ts(last_active),
        }
    return out


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def _members_rows(db, include_inactive: bool) -> list[dict]:
    """Member roster with activity stats (shared by list-members and usage hints)."""
    q = db.query(User).order_by(User.display_name, User.handle)
    if include_inactive:
        users = q.all()
    else:
        users = q.filter(User.is_active == True).all()  # noqa: E712
    uids = [u.id for u in users]
    stats = _user_stats(db, uids) if uids else {}
    return [{**_public_user(u), **stats.get(u.id, {})} for u in users]


def _render_members(pretty: list[dict]) -> str:
    if not pretty:
        return "(no members)"
    lines = []
    for row in pretty:
        lines.append(
            f"{row['id']:>3}  {row['handle']:<20} {row['display_name']:<18} "
            f"{'ADMIN' if row['is_admin'] else '     '}  "
            f"g={row['group_message_count']} r={row['room_message_count']} "
            f"last={row['last_active_at'] or '-'}  fam={row['family_name'] or '-'}"
        )
    return "\n".join(lines)


def _cmd_list_members(args) -> int:
    db = _open()
    try:
        pretty = _members_rows(db, include_inactive=args.all)
        if args.json:
            return _emit(args, pretty, pretty)
        return _emit(args, _render_members(pretty), pretty)
    finally:
        db.close()


def _cmd_delete_member(args) -> int:
    db = _open()
    try:
        target = _find_user(db, args.handle)
        if target is None:
            print(f"Error: no user '{args.handle}'.", file=sys.stderr)
            return 1
        if not args.yes and not _confirm(
            f"Delete {target.handle} (id {target.id}) and all their messages/reactions/files?"
        ):
            print("Aborted.")
            return 1

        uid = target.id
        db.execute(delete(Reaction).where(Reaction.user_id == uid))
        db.execute(delete(RoomMessage).where(RoomMessage.sender_id == uid))
        their_msg_ids = [
            r[0] for r in db.execute(select(GroupMessage.id).where(GroupMessage.author_id == uid)).all()
        ]
        if their_msg_ids:
            for other in db.execute(
                select(GroupMessage).where(GroupMessage.reply_to_id.in_(their_msg_ids))
            ).scalars().all():
                other.reply_to_id = None
            db.execute(delete(GroupMessage).where(GroupMessage.author_id == uid))
        from app.routers.upload_utils import delete_file

        for f in db.execute(select(File).where(File.owner_id == uid)).scalars().all():
            delete_file(f"/uploads/{f.storage_name}")
            db.delete(f)
        db.execute(delete(DMSettings).where(DMSettings.user_id == uid))
        for inv in db.execute(select(Invite).where(Invite.created_by == uid)).scalars().all():
            inv.created_by = None
        db.delete(target)
        db.commit()
        return _emit(args, f"Deleted {target.handle} (id {uid}).", {"ok": True, "deleted": uid})
    finally:
        db.close()


def _cmd_set_admin(args) -> int:
    db = _open()
    try:
        target = _find_user(db, args.handle)
        if target is None:
            print(f"Error: no user '{args.handle}'.", file=sys.stderr)
            return 1
        grant = args.role == "admin"
        new_state = grant and not target.is_admin
        if not args.yes and new_state and not _confirm(
            f"Grant admin to {target.handle} (id {target.id})?"
        ):
            print("Aborted.")
            return 1
        target.is_admin = grant
        db.commit()
        verb = "Granted" if grant else "Revoked"
        return _emit(
            args,
            f"{verb} admin on {target.handle} (id {target.id}).",
            {"ok": True, "id": target.id, "handle": target.handle, "is_admin": bool(grant)},
        )
    finally:
        db.close()


def _cmd_create_invite(args) -> int:
    db = _open()
    try:
        family_id = None
        if args.family is not None:
            fam = _find_family(db, args.family)
            if fam is None:
                print(f"Error: no family '{args.family}'.", file=sys.stderr)
                return 1
            family_id = fam.id
        admin = db.query(User).filter(User.is_admin == True).first()  # noqa: E712
        invite = Invite(
            code=_gen_invite_code(db),
            max_uses=args.max_uses,
            times_used=0,
            created_by=admin.id if admin else None,
            note=args.note,
            family_id=family_id,
        )
        db.add(invite)
        db.commit()
        db.refresh(invite)
        pretty = {
            "code": invite.code,
            "max_uses": invite.max_uses,
            "note": invite.note,
            "family_id": family_id,
        }
        if args.json:
            return _emit(args, pretty, pretty)
        out = f"Invite code: {invite.code}"
        if family_id:
            out += f"  (family id {family_id})"
        out += f"  -- uses: {invite.max_uses}"
        return _emit(args, out, pretty)
    finally:
        db.close()


def _cmd_list_invites(args) -> int:
    db = _open()
    try:
        rows = db.query(Invite).order_by(Invite.created_at.desc()).all()
        pretty = []
        lines = []
        for inv in rows:
            if not args.all and not _invite_active(inv):
                continue
            fam = db.get(Family, inv.family_id) if inv.family_id else None
            p = {
                "id": inv.id,
                "code": inv.code,
                "max_uses": inv.max_uses,
                "times_used": inv.times_used,
                "is_active": _invite_active(inv),
                "note": inv.note,
                "family_name": fam.name if fam else None,
                "created_at": _fmt_ts(inv.created_at),
            }
            pretty.append(p)
            lines.append(
                f"{inv.id:>3}  {inv.code}  {inv.times_used}/{inv.max_uses}  "
                f"{'ACTIVE' if p['is_active'] else 'USED' }  fam={p['family_name'] or '-'}  {p['note'] or ''}"
            )
        if args.json:
            return _emit(args, pretty, pretty)
        return _emit(args, "\n".join(lines) if lines else "(no invites)", pretty)
    finally:
        db.close()


def _cmd_revoke_invite(args) -> int:
    db = _open()
    try:
        invite = db.get(Invite, int(args.id))
        if invite is None:
            print(f"Error: no invite id {args.id}.", file=sys.stderr)
            return 1
        if not args.yes and not _confirm(f"Revoke invite {invite.code} (id {invite.id})?"):
            print("Aborted.")
            return 1
        if invite.times_used >= invite.max_uses or invite.used_at is not None:
            db.delete(invite)
        else:
            invite.max_uses = 0
            invite.used_at = invite.created_at
        db.commit()
        return _emit(args, f"Revoked invite {invite.code} (id {invite.id}).", {"ok": True, "id": invite.id})
    finally:
        db.close()


def _cmd_list_families(args) -> int:
    db = _open()
    try:
        rows = db.query(Family).order_by(Family.name).all()
        pretty = []
        lines = []
        for fam in rows:
            count = db.query(User).filter(User.family_id == fam.id, User.is_active == True).count()  # noqa: E712
            p = {"id": fam.id, "name": fam.name, "description": fam.description, "member_count": count}
            pretty.append(p)
            lines.append(f"{fam.id:>3}  {fam.name:<20} members={count:<3} {fam.description or ''}")
        if args.json:
            return _emit(args, pretty, pretty)
        return _emit(args, "\n".join(lines) if lines else "(no families)", pretty)
    finally:
        db.close()


def _cmd_create_family(args) -> int:
    db = _open()
    try:
        name = args.name.strip()
        if db.execute(select(Family).where(Family.name == name)).scalar_one_or_none() is not None:
            print(f"Error: a family named '{name}' already exists.", file=sys.stderr)
            return 1
        admin = db.query(User).filter(User.is_admin == True).first()  # noqa: E712
        fam = Family(name=name, description=args.description, created_by=admin.id if admin else None)
        db.add(fam)
        db.commit()
        db.refresh(fam)
        pretty = {"id": fam.id, "name": fam.name, "description": fam.description}
        return _emit(args, f"Created family '{fam.name}' (id {fam.id}).", pretty)
    finally:
        db.close()


def _cmd_assign_member(args) -> int:
    db = _open()
    try:
        user = _find_user(db, args.handle)
        if user is None:
            print(f"Error: no user '{args.handle}'.", file=sys.stderr)
            return 1
        if args.family == "0":
            user.family_id = None
            db.commit()
            return _emit(args, f"Removed {user.handle} from a family.", {"ok": True, "handle": user.handle, "family_id": None})
        fam = _find_family(db, args.family)
        if fam is None:
            print(f"Error: no family '{args.family}'.", file=sys.stderr)
            return 1
        user.family_id = fam.id
        db.commit()
        return _emit(
            args,
            f"Assigned {user.handle} to family '{fam.name}' (id {fam.id}).",
            {"ok": True, "handle": user.handle, "family_id": fam.id},
        )
    finally:
        db.close()


def _cmd_reset_admin_password(args) -> int:
    allow_fallback = args.handle is None
    handle = (args.handle or os.environ.get("ADMIN_HANDLE", "admin")).strip().lower()

    if args.password:
        password = args.password
    elif args.generate:
        password = _generate_password()
    else:
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Error: passwords do not match.", file=sys.stderr)
            return 1

    db = _open()
    try:
        user = db.query(User).filter(User.handle == handle).first()
        if user is None and allow_fallback:
            user = db.query(User).filter(User.is_admin == True).first()  # noqa: E712
        if user is None:
            print(
                f"Error: admin user with handle '{handle}' not found. "
                "Use --handle if the admin handle differs.",
                file=sys.stderr,
            )
            return 1

        user.password_hash = hash_password(password)
        user.is_admin = True
        user.is_active = True
        db.commit()

        if args.json:
            return _emit(args, f"Password for '{user.handle}' (admin) has been reset.", {"ok": True, "handle": user.handle})
        print(f"Password for '{user.handle}' (admin) has been reset.")
        if args.generate or not args.password:
            print(f"New password: {password}")
            print("(Save this now; it is not stored in plain text.)")
        return 0
    finally:
        db.close()


def _cmd_reset_password(args) -> int:
    db = _open()
    try:
        # No target given: show the roster + how to use this command.
        if args.handle is None:
            if args.json:
                print("Usage: manage.py reset-password <id|handle> [--password P | --generate] [--no-email]", file=sys.stderr)
                return 1
            pretty = _members_rows(db, include_inactive=False)
            print(_render_members(pretty))
            print()
            print("No user specified. To reset a password, run:")
            print("    manage.py reset-password <id|handle>            # auto-generated password")
            print("    manage.py reset-password <id|handle> --generate  # same (explicit)")
            print("    manage.py reset-password <id|handle> --password P  # set a specific password")
            print("    manage.py reset-password <id|handle> --no-email    # skip the email")
            print("If SMTP is enabled and the user has an email on file, the new password is emailed to them.")
            return 1

        target = _find_user(db, args.handle)
        if target is None:
            print(f"Error: no user '{args.handle}'.", file=sys.stderr)
            return 1

        if args.password:
            password = args.password
        elif args.generate:
            password = _generate_password()
        else:
            password = getpass.getpass(f"New password for {target.handle}: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                print("Error: passwords do not match.", file=sys.stderr)
                return 1

        if not args.yes and not _confirm(
            f"Reset the password for {target.handle} (id {target.id})?"
        ):
            print("Aborted.")
            return 1

        target.password_hash = hash_password(password)
        db.commit()

        # Best-effort email (only if SMTP is on, the user has an address, and
        # --no-email wasn't given). The password is never emailed in plain text
        # to a log; it's shown once on stdout and (optionally) in the email.
        email_sent = False
        if (
            not args.no_email
            and email_enabled()
            and target.email
            and "@" in target.email
        ):
            email_sent = send_password_reset_email(
                target.email,
                target.display_name or target.handle,
                password,
                target.handle,
            )

        if args.json:
            return _emit(
                args,
                f"Password for '{target.handle}' has been reset.",
                {"ok": True, "id": target.id, "handle": target.handle, "email_sent": email_sent},
            )
        print(f"Password for '{target.handle}' (id {target.id}) has been reset.")
        if args.generate or args.password is None:
            print(f"New password: {password}")
            print("(Save this now; it is not stored in plain text.)")
        if not args.no_email and email_enabled():
            if email_sent:
                print(f"Sent the new password to {target.email}.")
            elif not (target.email and "@" in target.email):
                print("(No email on file for this user; not emailed.)")
            else:
                print("(Email could not be sent; the user did not receive it.)")
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def _add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Admin maintenance CLI for community-chat (operates on the app's database).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-members", help="List members with activity stats.")
    p.add_argument("--all", action="store_true", help="Include inactive members.")
    _add_json(p)
    p.set_defaults(func=_cmd_list_members)

    p = sub.add_parser("delete-member", help="Hard-delete a member and their content.")
    p.add_argument("handle", help="User id or handle.")
    p.add_argument("--yes", action="store_true", help="Skip confirmation.")
    _add_json(p)
    p.set_defaults(func=_cmd_delete_member)

    p = sub.add_parser("set-admin", help="Grant or revoke the admin role.")
    p.add_argument("handle", help="User id or handle.")
    p.add_argument("--role", choices=["admin", "member"], default="admin")
    p.add_argument("--yes", action="store_true", help="Skip confirmation.")
    _add_json(p)
    p.set_defaults(func=_cmd_set_admin)

    p = sub.add_parser("create-invite", help="Create and print an invite code.")
    p.add_argument("--max-uses", type=int, default=1)
    p.add_argument("--note", default=None)
    p.add_argument("--family", default=None, help="Family id or name to scope the invite to.")
    _add_json(p)
    p.set_defaults(func=_cmd_create_invite)

    p = sub.add_parser("list-invites", help="List invites.")
    p.add_argument("--all", action="store_true", help="Include expired/used invites.")
    _add_json(p)
    p.set_defaults(func=_cmd_list_invites)

    p = sub.add_parser("revoke-invite", help="Revoke (or delete) an invite.")
    p.add_argument("id", help="Invite id (see list-invites).")
    p.add_argument("--yes", action="store_true", help="Skip confirmation.")
    _add_json(p)
    p.set_defaults(func=_cmd_revoke_invite)

    p = sub.add_parser("list-families", help="List families with member counts.")
    _add_json(p)
    p.set_defaults(func=_cmd_list_families)

    p = sub.add_parser("create-family", help="Create a family.")
    p.add_argument("name")
    p.add_argument("--description", default=None)
    _add_json(p)
    p.set_defaults(func=_cmd_create_family)

    p = sub.add_parser("assign-member", help="Put a user into a family (or 0 to remove).")
    p.add_argument("handle", help="User id or handle.")
    p.add_argument("family", help="Family id or name (or 0 to remove from a family).")
    _add_json(p)
    p.set_defaults(func=_cmd_assign_member)

    p = sub.add_parser("reset-admin-password", help="Reset the admin account's password.")
    p.add_argument("--handle", help="Admin handle (default: $ADMIN_HANDLE or 'admin').")
    p.add_argument("--password", help="Set the password directly (hidden if omitted).")
    p.add_argument("--generate", action="store_true", help="Generate and print a random password.")
    _add_json(p)
    p.set_defaults(func=_cmd_reset_admin_password)

    p = sub.add_parser("reset-password", help="Reset a user's password (auto-generated by default).")
    p.add_argument(
        "handle",
        nargs="?",
        help="User id or handle. Omit to print the member list + usage.",
    )
    p.add_argument("--password", help="Set the password directly (hidden if omitted).")
    p.add_argument("--generate", action="store_true", help="Generate and print a random password.")
    p.add_argument("--no-email", action="store_true", help="Do not email the new password.")
    p.add_argument("--yes", action="store_true", help="Skip confirmation.")
    _add_json(p)
    p.set_defaults(func=_cmd_reset_password)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
