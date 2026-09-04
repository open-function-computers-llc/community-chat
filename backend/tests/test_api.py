"""End-to-end API tests using a throwaway in-memory SQLite database.

Each test run drops and recreates all tables, so no state leaks between runs
and nothing touches a real data file.
"""
import io
import os
import sys
import tempfile
from pathlib import Path

# Use a throwaway in-memory DB (shared across sessions via StaticPool in db.py).
# Keep a temp dir for file uploads only.
_tmp = tempfile.mkdtemp(prefix="chat-test-")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DATA_DIR"] = _tmp
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.main import app  # noqa: E402
from app.db import Base, engine  # noqa: E402


def _png(width: int, height: int, color=(10, 90, 180)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return TestClient(app)


def make_user(c: TestClient, handle: str, password: str = "secret123", invite: str | None = None, as_admin: bool = False):
    """Create a user, generating an invite if none given. Returns (token, user).

    If as_admin is set, the freshly-registered user is promoted to admin in
    the DB (the seeded admin is created by lifespan, but these users are
    created via the public register endpoint).
    """
    if invite is None:
        # first user: create an invite directly via db
        from app.db import SessionLocal
        from app.models import Invite
        import secrets

        db = SessionLocal()
        inv = Invite(code="FIRSTCODE" + handle, max_uses=5, times_used=0)
        db.add(inv)
        db.commit()
        db.close()
        invite = "FIRSTCODE" + handle

    r = c.post(
        "/api/auth/register",
        json={
            "invite_code": invite,
            "handle": handle,
            "password": password,
            "display_name": handle.title(),
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    if as_admin:
        from app.db import SessionLocal
        from app.models import User

        db = SessionLocal()
        u = db.query(User).filter(User.handle == handle).first()
        u.is_admin = True
        db.commit()
        db.close()
    return data["token"], data["user"]


def auth(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_invite_required():
    c = client()
    r = c.post("/api/auth/register", json={"invite_code": "NOPE", "handle": "alice", "password": "secret123"})
    assert r.status_code == 403


def test_register_and_login():
    c = client()
    token, user = make_user(c, "alice")
    assert user["handle"] == "alice"

    r = c.get("/api/auth/me", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["handle"] == "alice"

    r = c.post("/api/auth/login", json={"handle": "alice", "password": "secret123"})
    assert r.status_code == 200
    assert "token" in r.json()

    r = c.post("/api/auth/login", json={"handle": "alice", "password": "wrong"})
    assert r.status_code == 401


def test_profile_update_and_settings():
    c = client()
    token, _ = make_user(c, "bob")
    r = c.patch(
        "/api/auth/me/profile",
        headers=auth(token),
        json={"display_name": "Bobby", "email": "bob@example.com", "bio": "hi"},
    )
    assert r.status_code == 200
    assert r.json()["display_name"] == "Bobby"
    assert r.json()["bio"] == "hi"

    r = c.get("/api/auth/me/settings", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["notify_mentions"] is True

    r = c.put(
        "/api/auth/me/settings",
        headers=auth(token),
        json={"do_not_disturb": True, "notify_mentions": False, "notify_replies": True, "email_notifications": False},
    )
    assert r.status_code == 200
    assert r.json()["do_not_disturb"] is True
    assert r.json()["email_notifications"] is False


def test_user_avatar_processed_to_webp():
    c = client()
    token, user = make_user(c, "carl")
    uid = user["id"]

    # Upload a large non-square image via the dedicated avatar endpoint.
    r = c.post(
        "/api/auth/me/avatar",
        headers=auth(token),
        files={"file": ("me.png", _png(1000, 700), "image/png")},
    )
    assert r.status_code == 200, r.text
    avatar_url = r.json()["avatar_url"]
    assert avatar_url and avatar_url.startswith("/uploads/")

    # Stored file is a 500x500 WebP.
    r = c.get(avatar_url)
    assert r.status_code == 200
    assert r.content[:4] == b"RIFF"
    img = Image.open(io.BytesIO(r.content))
    assert img.format == "WEBP"
    assert img.size == (500, 500), img.size

    # Replacing it deletes the old file (no full-res revision retained).
    old_url = avatar_url
    r = c.post(
        "/api/auth/me/avatar",
        headers=auth(token),
        files={"file": ("me2.png", _png(400, 400, color=(200, 20, 20)), "image/png")},
    )
    assert r.status_code == 200
    new_url = r.json()["avatar_url"]
    assert new_url != old_url
    assert c.get(old_url).status_code == 404
    assert c.get(new_url).status_code == 200

    # Non-image rejected.
    r = c.post(
        "/api/auth/me/avatar",
        headers=auth(token),
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert r.status_code == 415


def test_oversized_avatar_rejected():
    # A >25MB image must be rejected with 413 (avatars are shrunk on upload,
    # so the source cap is looser than general files, but still bounded).
    import app.routers.upload_utils as uu

    c = client()
    token, _ = make_user(c, "frank")
    big = b"0" * (26 * 1024 * 1024)  # 26 MB
    r = c.post(
        "/api/auth/me/avatar",
        headers=auth(token),
        files={"file": ("big.jpg", big, "image/jpeg")},
    )
    # 413 (too large) — not a crash/500.
    assert r.status_code == 413
    assert "25 MB" in r.json().get("detail", "") or "too large" in r.json().get("detail", "").lower()
    # Sanity: the constant is what we expect.
    assert uu.MAX_AVATAR_SIZE == 25 * 1024 * 1024


def test_invite_lifecycle():
    c = client()
    t1, _ = make_user(c, "carol", as_admin=True)
    r = c.get("/api/invites", headers=auth(t1))
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r = c.post("/api/invites", headers=auth(t1), json={"max_uses": 2, "note": "for dave"})
    assert r.status_code == 201
    code = r.json()["code"]

    t2, u2 = make_user(c, "dave", invite=code)
    r = c.get("/api/invites", headers=auth(t1))
    used = [i for i in r.json() if i["code"] == code][0]
    assert used["times_used"] == 1

    t3, _ = make_user(c, "erin", invite=code)
    r = c.get("/api/invites", headers=auth(t1))
    used = [i for i in r.json() if i["code"] == code][0]
    assert used["times_used"] == 2
    assert used["is_active"] is False


def test_group_messages_crud():
    c = client()
    t1, _ = make_user(c, "frank", as_admin=True)
    t2, _ = make_user(c, "grace", invite=_next_invite(c, t1))

    r = c.post("/api/messages", headers=auth(t1), json={"text": "hello world"})
    assert r.status_code == 201
    msg = r.json()
    assert msg["text"] == "hello world"
    assert msg["author"]["handle"] == "frank"

    r = c.get("/api/messages", headers=auth(t2))
    assert r.status_code == 200
    assert r.json()[0]["text"] == "hello world"

    r = c.post(
        "/api/messages",
        headers=auth(t2),
        json={"text": "hi frank", "reply_to_id": msg["id"]},
    )
    assert r.status_code == 201
    assert r.json()["reply_to"]["author"]["handle"] == "frank"

    r = c.patch("/api/messages/%d" % msg["id"], headers=auth(t1), json={"text": "edited"})
    assert r.status_code == 200
    assert r.json()["text"] == "edited"
    assert r.json()["edited_at"] is not None

    r = c.patch("/api/messages/%d" % msg["id"], headers=auth(t2), json={"text": "nope"})
    assert r.status_code == 403

    r = c.post("/api/messages/%d/reactions/👍" % msg["id"], headers=auth(t2))
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["user_ids"] == [2]

    r = c.delete("/api/messages/%d/reactions/👍" % msg["id"], headers=auth(t2))
    assert r.status_code == 200
    assert r.json()["removed"] is True

    r = c.delete("/api/messages/%d" % msg["id"], headers=auth(t1))
    assert r.status_code == 200
    r = c.get("/api/messages", headers=auth(t1))
    assert all(m["id"] != msg["id"] for m in r.json())


def _assign_family(c, handle, family_id):
    """Test helper: assign a user to a family directly in the DB."""
    from app.db import SessionLocal
    from app.models import User

    db = SessionLocal()
    u = db.query(User).filter(User.handle == handle).first()
    u.family_id = family_id
    db.commit()
    db.close()


def test_family_room_dm():
    c = client()
    admin, _ = make_user(c, "heidi", as_admin=True)
    t2, u2 = make_user(c, "ivan", invite=_next_invite(c, admin))

    # Two families, one per user.
    r = c.post("/api/families", headers=auth(admin), json={"name": "Heidis"})
    assert r.status_code == 201
    fam_heidi = r.json()["id"]
    r = c.post("/api/families", headers=auth(admin), json={"name": "Ivans"})
    assert r.status_code == 201
    fam_ivan = r.json()["id"]
    _assign_family(c, "heidi", fam_heidi)
    _assign_family(c, "ivan", fam_ivan)

    # Admin opens the room between the two families.
    r = c.post("/api/dms/rooms", headers=auth(admin), json={"family_id": fam_ivan})
    assert r.status_code == 201, r.text
    room_id = r.json()["id"]

    # Sending a room message works and has no read receipt field.
    r = c.post("/api/dms/rooms/%d" % room_id, headers=auth(admin), json={"text": "hey"})
    assert r.status_code == 201
    msg = r.json()
    assert msg["sender"]["handle"] == "heidi"
    assert "read_at" not in msg

    # Ivan reads the room history.
    r = c.get("/api/dms/rooms/%d" % room_id, headers=auth(t2))
    assert r.status_code == 200
    history = r.json()
    assert len(history["messages"]) == 1
    assert history["messages"][0]["text"] == "hey"

    # Reaction on a room message.
    r = c.post(
        "/api/dms/rooms/%d/messages/%d/reactions/\U0001F44D" % (room_id, msg["id"]),
        headers=auth(t2),
    )
    assert r.status_code == 200
    assert r.json()["count"] == 1

    # A user with no family cannot access the room.
    t3, _ = make_user(c, "zoe", invite=_next_invite(c, admin))
    assert c.get("/api/dms/rooms/%d" % room_id, headers=auth(t3)).status_code == 403
    assert c.post(
        "/api/dms/rooms/%d" % room_id, headers=auth(t3), json={"text": "nope"}
    ).status_code == 403

    # Admin's room list shows the room.
    r = c.get("/api/dms/rooms", headers=auth(admin))
    assert r.status_code == 200
    assert any(rm["id"] == room_id for rm in r.json())


def test_admin_user_stats_and_delete():
    c = client()
    admin, _ = make_user(c, "root", as_admin=True)
    # Two regular members, each with some content.
    bob, ubob = make_user(c, "bob", invite=_next_invite(c, admin))
    carl, ucarl = make_user(c, "carl", invite=_next_invite(c, admin))

    # Give bob and carl families and open a room between them.
    r = c.post("/api/families", headers=auth(admin), json={"name": "Bobs"})
    assert r.status_code == 201
    fam_bob = r.json()["id"]
    r = c.post("/api/families", headers=auth(admin), json={"name": "Carls"})
    assert r.status_code == 201
    fam_carl = r.json()["id"]
    _assign_family(c, "bob", fam_bob)
    _assign_family(c, "carl", fam_carl)

    r = c.post("/api/dms/rooms", headers=auth(bob), json={"family_id": fam_carl})
    assert r.status_code == 201
    room_id = r.json()["id"]

    # Bob sends 2 group messages and 1 room message; Carl sends 1 room message.
    assert c.post("/api/messages", headers=auth(bob), json={"text": "b1"}).status_code == 201
    assert c.post("/api/messages", headers=auth(bob), json={"text": "b2"}).status_code == 201
    assert c.post("/api/dms/rooms/%d" % room_id, headers=auth(bob), json={"text": "room from bob"}).status_code == 201
    assert c.post("/api/dms/rooms/%d" % room_id, headers=auth(carl), json={"text": "room from carl"}).status_code == 201

    # Non-admins cannot see the stats endpoint or delete users.
    assert c.get("/api/auth/admin/users", headers=auth(bob)).status_code == 403
    assert c.delete("/api/auth/users/%d" % ucarl["id"], headers=auth(bob)).status_code == 403

    # Admin sees stats reflecting activity.
    r = c.get("/api/auth/admin/users", headers=auth(admin))
    assert r.status_code == 200
    by_handle = {u["handle"]: u for u in r.json()}
    assert by_handle["bob"]["group_message_count"] == 2
    assert by_handle["bob"]["room_message_count"] == 1
    assert by_handle["bob"]["last_active_at"] is not None
    assert by_handle["carl"]["group_message_count"] == 0
    assert by_handle["carl"]["room_message_count"] == 1

    # Admin cannot delete themselves.
    root_id = None
    r = c.get("/api/auth/me", headers=auth(admin))
    root_id = r.json()["id"]
    assert c.delete("/api/auth/users/%d" % root_id, headers=auth(admin)).status_code == 400

    # Admin hard-deletes Carl: his DMs (both directions), group msgs, etc. go away.
    assert c.delete("/api/auth/users/%d" % ucarl["id"], headers=auth(admin)).status_code == 200

    # Carl is gone from the member list.
    r = c.get("/api/auth/admin/users", headers=auth(admin))
    handles = {u["handle"] for u in r.json()}
    assert "carl" not in handles
    assert "bob" in handles

    # Carl's room messages are gone, but bob's remain (room is intact).
    r = c.get("/api/dms/rooms/%d" % room_id, headers=auth(bob))
    assert r.status_code == 200
    room_texts = [m["text"] for m in r.json()["messages"]]
    assert "room from bob" in room_texts
    assert "room from carl" not in room_texts

    # Group messages Bob sent are intact (Bob was not deleted).
    r = c.get("/api/messages", headers=auth(bob))
    texts = [m["text"] for m in r.json()]
    assert "b1" in texts and "b2" in texts

    # Carl's account no longer logs in.
    r = c.post("/api/auth/login", json={"handle": "carl", "password": "secret123"})
    assert r.status_code == 401


def test_file_upload():
    c = client()
    t1, _ = make_user(c, "judy")
    r = c.post(
        "/api/files/upload",
        headers=auth(t1),
        files={"file": ("hello.txt", b"hello there", "text/plain")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "hello.txt"
    assert body["url"].startswith("/uploads/")

    r = c.get(body["url"])
    assert r.status_code == 200
    assert r.content == b"hello there"

    r = c.get("/api/files/me", headers=auth(t1))
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = c.delete("/api/files/%d" % body["id"], headers=auth(t1))
    assert r.status_code == 200
    r = c.get("/api/files/me", headers=auth(t1))
    assert r.json() == []


def test_ws_hello():
    c = client()
    t1, _ = make_user(c, "kim")
    with c.websocket_connect(f"/ws?token={t1}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "hello"


def _next_invite(c: TestClient, token: str) -> str:
    r = c.post("/api/invites", headers=auth(token), json={"max_uses": 1})
    assert r.status_code == 201
    return r.json()["code"]
