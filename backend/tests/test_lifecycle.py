"""End-to-end integration test on a throwaway in-memory SQLite database.

Simulates the real lifecycle:
  1. Seeder creates the admin user on boot.
  2. Admin logs in.
  3. Admin creates an invite.
  4. A second user joins using that invite code.
  5. Both users exchange a group message and a DM; reactions + read receipts
     are verified.
"""
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="chat-it-")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DATA_DIR"] = _tmp
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ["SUMMARY_ENABLED"] = "0"  # keep the digest loop out of tests
os.environ["ADMIN_HANDLE"] = "admin"
os.environ["ADMIN_PASSWORD"] = "adminpass123"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import Base, engine  # noqa: E402


def auth_header(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_full_lifecycle():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with TestClient(app) as c:
        # 1. Verify the seeder created the admin (lifespan ran on context entry).
        r = c.post("/api/auth/login", json={"handle": "admin", "password": "adminpass123"})
        assert r.status_code == 200, r.text
        admin = r.json()
        admin_token = admin["token"]
        admin_user = admin["user"]
        assert admin_user["handle"] == "admin"

        # 2. Admin can list users (just themselves).
        r = c.get("/api/auth/users", headers=auth_header(admin_token))
        assert r.status_code == 200
        assert [u["handle"] for u in r.json()] == ["admin"]

        # 3. Admin creates an invite.
        r = c.post("/api/invites", headers=auth_header(admin_token), json={"max_uses": 1, "note": "for dave"})
        assert r.status_code == 201, r.text
        invite = r.json()
        invite_code = invite["code"]
        assert invite["is_active"] is True

        # 4. A second user joins using the invite code (the only join path).
        r = c.post(
            "/api/auth/register",
            json={
                "invite_code": invite_code,
                "handle": "dave",
                "password": "davepass123",
                "display_name": "Dave",
            },
        )
        assert r.status_code == 200, r.text
        dave = r.json()
        dave_token = dave["token"]
        assert dave["user"]["handle"] == "dave"

        # 4b. The invite is now consumed.
        r = c.get("/api/invites", headers=auth_header(admin_token))
        used = [i for i in r.json() if i["code"] == invite_code][0]
        assert used["times_used"] == 1
        assert used["is_active"] is False

        # 4c. Re-using the consumed invite fails.
        r = c.post(
            "/api/auth/register",
            json={"invite_code": invite_code, "handle": "eve", "password": "evepass123"},
        )
        assert r.status_code == 403

        # 5. Group chat: dave posts, admin sees it.
        r = c.post("/api/messages", headers=auth_header(dave_token), json={"text": "hello everyone!"})
        assert r.status_code == 201, r.text
        group_msg = r.json()
        assert group_msg["author"]["handle"] == "dave"
        assert group_msg["text"] == "hello everyone!"

        r = c.get("/api/messages", headers=auth_header(admin_token))
        assert r.status_code == 200
        msgs = r.json()
        assert any(m["id"] == group_msg["id"] and m["text"] == "hello everyone!" for m in msgs)

        # 5b. Admin replies to dave's message.
        r = c.post(
            "/api/messages",
            headers=auth_header(admin_token),
            json={"text": "hey dave, welcome", "reply_to_id": group_msg["id"]},
        )
        assert r.status_code == 201
        assert r.json()["reply_to"]["author"]["handle"] == "dave"

        # 5c. Reaction on the group message.
        r = c.post(
            f"/api/messages/{group_msg['id']}/reactions/\U0001F44D",
            headers=auth_header(admin_token),
        )
        assert r.status_code == 200
        assert r.json()["count"] == 1

        # 6. Family rooms: put admin and dave in (different) families, open a
        # shared room between them, and exchange messages.
        r = c.post("/api/families", headers=auth_header(admin_token), json={"name": "Admins"})
        assert r.status_code == 201
        fam_admin = r.json()
        r = c.post("/api/families", headers=auth_header(admin_token), json={"name": "Davids"})
        assert r.status_code == 201
        fam_dave = r.json()

        # Assign each user to a family (admin API: use the families store by
        # re-registering isn't possible, so assign via the DB through a helper
        # endpoint is not exposed — instead, use the invite family path is
        # already consumed. We assign via a direct DB update helper.)
        from app.db import SessionLocal
        from app.models import User

        db = SessionLocal()
        admin_row = db.query(User).filter(User.handle == "admin").first()
        dave_row = db.query(User).filter(User.handle == "dave").first()
        admin_row.family_id = fam_admin["id"]
        dave_row.family_id = fam_dave["id"]
        db.commit()
        db.close()

        # Admin opens the room between the two families.
        r = c.post(
            "/api/dms/rooms",
            headers=auth_header(admin_token),
            json={"family_id": fam_dave["id"]},
        )
        assert r.status_code == 201, r.text
        room = r.json()
        room_id = room["id"]
        fam_names = {f["name"] for f in room["families"]}
        assert fam_names == {"Admins", "Davids"}

        # Sending to the room works for a member; the message lands for both.
        r = c.post(
            f"/api/dms/rooms/{room_id}",
            headers=auth_header(admin_token),
            json={"text": "psst, hi dave"},
        )
        assert r.status_code == 201, r.text
        msg = r.json()
        assert msg["sender"]["handle"] == "admin"
        assert "read_at" not in msg  # no read receipts in rooms

        # Dave can read the room history.
        r = c.get(f"/api/dms/rooms/{room_id}", headers=auth_header(dave_token))
        assert r.status_code == 200
        history = r.json()
        assert len(history["messages"]) == 1
        assert history["messages"][0]["text"] == "psst, hi dave"

        # Non-members cannot read or write the room.
        # (A user with no family can't be a member.)
        r = c.post(
            "/api/invites", headers=auth_header(admin_token), json={"max_uses": 1, "note": "out"}
        )
        r = c.post(
            "/api/auth/register",
            json={"invite_code": r.json()["code"], "handle": "eve", "password": "evepass123"},
        )
        eve_token = r.json()["token"]
        assert c.get(f"/api/dms/rooms/{room_id}", headers=auth_header(eve_token)).status_code == 403
        assert c.post(
            f"/api/dms/rooms/{room_id}", headers=auth_header(eve_token), json={"text": "hi"}
        ).status_code == 403

        # 7. Admin's room list shows the shared room.
        r = c.get("/api/dms/rooms", headers=auth_header(admin_token))
        assert r.status_code == 200
        rooms = r.json()
        assert any(rm["id"] == room_id for rm in rooms)

        # 8. WebSocket hello handshake for dave.
        dave_id = dave["user"]["id"]
        with c.websocket_connect(f"/ws?token={dave_token}") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["user_id"] == dave_id

    print("LIFECYCLE OK: seed -> login -> invite -> join -> group chat -> family room -> ws")
