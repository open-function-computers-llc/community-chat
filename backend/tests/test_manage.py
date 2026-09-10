"""Tests for the manage.py admin CLI.

Each scenario runs in an isolated subprocess against its own throwaway SQLite
file, so it never touches the shared in-memory DB used by the other suites
(whose module-level env setup runs at import time). The subprocess seeds a
small set of rows, runs manage.py with the given argv, and the test then
inspects the file DB directly (sqlite3) to assert the effect.
"""
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import bcrypt

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _seed_code(tmp: Path) -> str:
    """Return the seed statements for a fresh DB: an admin + a regular user
    with a family and a couple of group messages."""
    return textwrap.dedent(
        """
        from app.db import Base, SessionLocal, engine
        from app.models import User, Family, GroupMessage
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        s = SessionLocal()
        admin = User(handle='admin', password_hash='oldhash', is_admin=True)
        alice = User(handle='alice', password_hash='oldhash', is_admin=False)
        fam = Family(name='Alfords')
        s.add_all([admin, alice, fam]); s.flush()
        alice.family_id = fam.id
        m1 = GroupMessage(author_id=alice.id, text='hello')
        m2 = GroupMessage(author_id=alice.id, text='again', reply_to_id=m1.id)
        s.add_all([m1, m2])
        s.commit(); s.close()
        """
    )


def _run(tmp: Path, argv, seed: str = None):
    """Run manage.py in a subprocess against a fresh SQLite file in tmp."""
    db_path = tmp / "chat.db"
    env = {
        "DATABASE_URL": f"sqlite:///{db_path}",
        "DATA_DIR": str(tmp),
        "UPLOAD_DIR": str(tmp / "uploads"),
        "PYTHONPATH": str(BACKEND_DIR),
    }
    url = f"sqlite:///{db_path}"
    prelude = (
        f"import os\n"
        f"os.environ['DATABASE_URL']={url!r}\n"
        f"os.environ['DATA_DIR']={str(tmp)!r}\n"
        f"os.environ['UPLOAD_DIR']={str(tmp / 'uploads')!r}\n"
    )
    seed = seed if seed is not None else _seed_code(tmp)
    code = (
        prelude
        + seed
        + f"\nimport manage\nimport sys\nsys.exit(manage.main({list(argv)!r}))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=BACKEND_DIR
    )


def _query(tmp: Path, sql):
    return sqlite3.connect(tmp / "chat.db").execute(sql)


# --- reset-admin-password --------------------------------------------------
def test_reset_admin_password_by_flag(tmp_path):
    proc = _run(
        tmp_path,
        ["reset-admin-password", "--handle", "root", "--password", "newpass"],
        seed=textwrap.dedent(
            """
            from app.db import Base, SessionLocal, engine
            from app.models import User
            Base.metadata.drop_all(engine)
            Base.metadata.create_all(engine)
            s = SessionLocal(); s.add(User(handle='root', password_hash='oldhash', is_admin=True)); s.commit(); s.close()
            """
        ),
    )
    assert proc.returncode == 0, proc.stderr
    stored = _query(tmp_path, "SELECT password_hash FROM users WHERE handle='root'").fetchone()[0]
    assert stored != "oldhash"
    assert bcrypt.checkpw(b"newpass", stored.encode("utf-8"))
    assert not bcrypt.checkpw(b"oldhash", stored.encode("utf-8"))


def test_reset_admin_password_missing_user(tmp_path):
    proc = _run(
        tmp_path,
        ["reset-admin-password", "--handle", "ghost", "--password", "x"],
    )
    assert proc.returncode == 1
    assert "not found" in proc.stderr


def test_reset_admin_password_fallback_to_single_admin(tmp_path):
    # No --handle -> falls back to the single is_admin user.
    proc = _run(tmp_path, ["reset-admin-password", "--password", "fresh"])
    assert proc.returncode == 0, proc.stderr
    assert "admin" in proc.stdout
    stored = _query(tmp_path, "SELECT password_hash FROM users WHERE handle='admin'").fetchone()[0]
    assert bcrypt.checkpw(b"fresh", stored.encode("utf-8"))


def test_reset_admin_password_generates(tmp_path):
    proc = _run(tmp_path, ["reset-admin-password", "--generate"])
    assert proc.returncode == 0, proc.stderr
    assert "New password:" in proc.stdout


# --- reset-password --------------------------------------------------------
def test_reset_password_no_target_shows_usage_and_list(tmp_path):
    proc = _run(tmp_path, ["reset-password"])
    assert proc.returncode == 1, proc.stderr
    # Prints the member roster (default seed has admin + alice).
    assert "alice" in proc.stdout
    assert "admin" in proc.stdout
    # And a usage hint.
    assert "reset-password <id|handle>" in proc.stdout


def test_reset_password_generate(tmp_path):
    proc = _run(tmp_path, ["reset-password", "alice", "--generate", "--yes"])
    assert proc.returncode == 0, proc.stderr
    assert "New password:" in proc.stdout
    stored = _query(tmp_path, "SELECT password_hash FROM users WHERE handle='alice'").fetchone()[0]
    assert stored != "oldhash"


def test_reset_password_explicit(tmp_path):
    proc = _run(tmp_path, ["reset-password", "alice", "--password", "brandnew", "--yes"])
    assert proc.returncode == 0, proc.stderr
    stored = _query(tmp_path, "SELECT password_hash FROM users WHERE handle='alice'").fetchone()[0]
    assert bcrypt.checkpw(b"brandnew", stored.encode("utf-8"))
    assert not bcrypt.checkpw(b"oldhash", stored.encode("utf-8"))


def test_reset_password_missing_user(tmp_path):
    proc = _run(tmp_path, ["reset-password", "ghost", "--password", "x", "--yes"])
    assert proc.returncode == 1
    assert "no user" in proc.stderr


def test_reset_password_json(tmp_path):
    proc = _run(tmp_path, ["reset-password", "alice", "--generate", "--yes", "--json"])
    assert proc.returncode == 0, proc.stderr
    import json

    data = json.loads(proc.stdout)
    assert data["ok"] is True
    assert data["handle"] == "alice"
    assert "email_sent" in data


# --- list-members ----------------------------------------------------------
def test_list_members(tmp_path):
    proc = _run(tmp_path, ["list-members"])
    assert proc.returncode == 0, proc.stderr
    assert "alice" in proc.stdout
    assert "ADMIN" in proc.stdout
    # alice posted 2 group messages.
    assert "g=2" in proc.stdout


def test_list_members_json(tmp_path):
    proc = _run(tmp_path, ["list-members", "--json"])
    assert proc.returncode == 0, proc.stderr
    import json

    data = json.loads(proc.stdout)
    by = {u["handle"]: u for u in data}
    assert by["alice"]["group_message_count"] == 2
    assert by["admin"]["is_admin"] is True
    assert by["alice"]["family_name"] == "Alfords"


# --- delete-member ---------------------------------------------------------
def test_delete_member(tmp_path):
    proc = _run(tmp_path, ["delete-member", "alice", "--yes"])
    assert proc.returncode == 0, proc.stderr
    assert _query(tmp_path, "SELECT count(*) FROM users WHERE handle='alice'").fetchone()[0] == 0
    # Their group messages are gone too.
    assert _query(tmp_path, "SELECT count(*) FROM group_messages").fetchone()[0] == 0
    assert _query(tmp_path, "SELECT count(*) FROM users WHERE handle='admin'").fetchone()[0] == 1


def test_delete_member_unknown(tmp_path):
    proc = _run(tmp_path, ["delete-member", "ghost", "--yes"])
    assert proc.returncode == 1
    assert "no user" in proc.stderr


# --- set-admin -------------------------------------------------------------
def test_set_admin_grant(tmp_path):
    proc = _run(tmp_path, ["set-admin", "alice", "--yes"])
    assert proc.returncode == 0, proc.stderr
    assert _query(tmp_path, "SELECT is_admin FROM users WHERE handle='alice'").fetchone()[0] == 1


def test_set_admin_revoke(tmp_path):
    proc = _run(tmp_path, ["set-admin", "alice", "--role", "member", "--yes"])
    assert proc.returncode == 0, proc.stderr
    assert _query(tmp_path, "SELECT is_admin FROM users WHERE handle='alice'").fetchone()[0] == 0


# --- invites ---------------------------------------------------------------
def _seed_with_invite(tmp: Path) -> str:
    return textwrap.dedent(
        """
        from app.db import Base, SessionLocal, engine
        from app.models import User, Family, Invite
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        s = SessionLocal()
        s.add(User(handle='admin', password_hash='oldhash', is_admin=True))
        s.add(Family(name='Alfords'))
        s.add(Invite(code='ABCDE123', max_uses=3, times_used=0, created_by=1, note='hi'))
        s.commit(); s.close()
        """
    )


def test_create_invite(tmp_path):
    proc = _run(tmp_path, ["create-invite", "--max-uses", "3", "--note", "hi"])
    assert proc.returncode == 0, proc.stderr
    import re

    assert re.search(r"Invite code: \w+", proc.stdout)
    assert _query(tmp_path, "SELECT count(*) FROM invites").fetchone()[0] == 1


def test_list_invites_json(tmp_path):
    proc = _run(tmp_path, ["list-invites", "--json"], seed=_seed_with_invite(tmp_path))
    assert proc.returncode == 0, proc.stderr
    import json

    rows = json.loads(proc.stdout)
    assert rows[0]["code"] == "ABCDE123"
    assert rows[0]["is_active"] is True
    assert rows[0]["family_name"] is None  # invite had no family
    assert _query(tmp_path, "SELECT max_uses FROM invites").fetchone()[0] == 3


def test_revoke_invite(tmp_path):
    proc = _run(tmp_path, ["revoke-invite", "1", "--yes"], seed=_seed_with_invite(tmp_path))
    assert proc.returncode == 0, proc.stderr
    # Revoking an unused invite zeroes max_uses (marks it inactive).
    assert _query(tmp_path, "SELECT max_uses FROM invites").fetchone()[0] == 0


def test_revoke_invite_bad_id(tmp_path):
    proc = _run(tmp_path, ["revoke-invite", "999", "--yes"], seed=_seed_with_invite(tmp_path))
    assert proc.returncode == 1
    assert "no invite" in proc.stderr


def test_create_invite_family_scoped(tmp_path):
    proc = _run(tmp_path, ["create-invite", "--family", "Alfords", "--max-uses", "1"])
    assert proc.returncode == 0, proc.stderr
    assert _query(
        tmp_path, "SELECT family_id FROM invites"
    ).fetchone()[0] is not None


# --- families --------------------------------------------------------------
def test_list_families(tmp_path):
    proc = _run(tmp_path, ["list-families"])
    assert proc.returncode == 0, proc.stderr
    assert "Alfords" in proc.stdout
    assert "members=1" in proc.stdout


def test_create_family(tmp_path):
    proc = _run(tmp_path, ["create-family", "Bobs", "--description", "bob's folks"])
    assert proc.returncode == 0, proc.stderr
    assert _query(tmp_path, "SELECT count(*) FROM families WHERE name='Bobs'").fetchone()[0] == 1


def test_create_family_duplicate(tmp_path):
    proc = _run(tmp_path, ["create-family", "Alfords"])
    assert proc.returncode == 1
    assert "already exists" in proc.stderr


def test_assign_member(tmp_path):
    # alice is already in Alfords; move her out with 0.
    proc = _run(tmp_path, ["assign-member", "alice", "0"])
    assert proc.returncode == 0, proc.stderr
    assert _query(tmp_path, "SELECT family_id FROM users WHERE handle='alice'").fetchone()[0] is None


def _seed_with_bobs(tmp: Path) -> str:
    return textwrap.dedent(
        """
        from app.db import Base, SessionLocal, engine
        from app.models import User, Family
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        s = SessionLocal()
        s.add(User(handle='admin', password_hash='oldhash', is_admin=True))
        s.add(User(handle='alice', password_hash='oldhash'))
        s.add(Family(name='Bobs'))
        s.commit(); s.close()
        """
    )


def test_assign_member_to_family(tmp_path):
    proc = _run(tmp_path, ["assign-member", "alice", "Bobs"], seed=_seed_with_bobs(tmp_path))
    assert proc.returncode == 0, proc.stderr
    fid = _query(tmp_path, "SELECT id FROM families WHERE name='Bobs'").fetchone()[0]
    assert _query(tmp_path, "SELECT family_id FROM users WHERE handle='alice'").fetchone()[0] == fid


def test_assign_member_bad_family(tmp_path):
    proc = _run(tmp_path, ["assign-member", "alice", "Nowhere"])
    assert proc.returncode == 1
    assert "no family" in proc.stderr
