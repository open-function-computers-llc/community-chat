"""Tests for the families feature: create families, attach invites to a
family, and verify a user joining via a family invite is assigned to it.
"""
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="chat-fam-")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DATA_DIR"] = _tmp
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ["ADMIN_HANDLE"] = "admin"
os.environ["ADMIN_PASSWORD"] = "adminpass123"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import io  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.main import app  # noqa: E402
from app.db import Base, engine  # noqa: E402


def auth_header(token: str):
    return {"Authorization": f"Bearer {token}"}


def make_png(width: int, height: int, color=(200, 30, 30)) -> bytes:
    """A real, decodable PNG (so the avatar processor has something to crop)."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


WEBP_MAGIC = b"RIFF"  # WebP files start with RIFF....WEBP


def test_families_full_flow():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with TestClient(app) as c:
        # Admin logs in (seeded by lifespan).
        r = c.post("/api/auth/login", json={"handle": "admin", "password": "adminpass123"})
        assert r.status_code == 200, r.text
        admin_token = r.json()["token"]
        admin_id = r.json()["user"]["id"]

        # No families yet.
        r = c.get("/api/families", headers=auth_header(admin_token))
        assert r.status_code == 200
        assert r.json() == []

        # Create two families.
        r = c.post(
            "/api/families",
            headers=auth_header(admin_token),
            json={"name": "Holsapples", "description": "The big one"},
        )
        assert r.status_code == 201, r.text
        fam1 = r.json()
        assert fam1["name"] == "Holsapples"
        assert fam1["member_count"] == 0

        r = c.post("/api/families", headers=auth_header(admin_token), json={"name": "Smiths"})
        assert r.status_code == 201
        fam2 = r.json()

        # Duplicate name is rejected.
        r = c.post("/api/families", headers=auth_header(admin_token), json={"name": "Smiths"})
        assert r.status_code == 409

        # Rename a family.
        r = c.put(
            f"/api/families/{fam2['id']}",
            headers=auth_header(admin_token),
            json={"name": "Smiths & Co"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Smiths & Co"

        # List families.
        r = c.get("/api/families", headers=auth_header(admin_token))
        names = [f["name"] for f in r.json()]
        assert "Holsapples" in names and "Smiths & Co" in names

        # --- Invite into a specific family ---
        r = c.post(
            "/api/invites",
            headers=auth_header(admin_token),
            json={"max_uses": 1, "family_id": fam1["id"], "note": "for grandma"},
        )
        assert r.status_code == 201, r.text
        invite = r.json()
        assert invite["family_id"] == fam1["id"]
        assert invite["family_name"] == "Holsapples"

        # Invite into a non-existent family is rejected.
        r = c.post(
            "/api/invites",
            headers=auth_header(admin_token),
            json={"max_uses": 1, "family_id": 9999},
        )
        assert r.status_code == 404

        # An invite with no family is allowed (admin/staff).
        r = c.post("/api/invites", headers=auth_header(admin_token), json={"max_uses": 1})
        assert r.status_code == 201
        assert r.json()["family_id"] is None

        # Grandma joins via the family invite -> assigned to Holsapples.
        r = c.post(
            "/api/auth/register",
            json={
                "invite_code": invite["code"],
                "handle": "grandma",
                "password": "grandma123",
                "display_name": "Grandma",
            },
        )
        assert r.status_code == 200, r.text
        grandma = r.json()["user"]
        assert grandma["family_id"] == fam1["id"]
        assert grandma["family_name"] == "Holsapples"

        # Family member count reflects the new member.
        r = c.get("/api/families", headers=auth_header(admin_token))
        fam1_now = [f for f in r.json() if f["id"] == fam1["id"]][0]
        assert fam1_now["member_count"] == 1

        # Delete the family with members -> blocked.
        r = c.delete(f"/api/families/{fam1['id']}", headers=auth_header(admin_token))
        assert r.status_code == 400

        # --- Family avatar upload + replace ---
        # Upload a *large, non-square* image: it should be center-cropped to a
        # square, resized to 500x500, and stored as WebP.
        big_png = make_png(1200, 900, color=(200, 30, 30))
        r = c.post(
            f"/api/families/{fam1['id']}/avatar",
            headers=auth_header(admin_token),
            files={"file": ("family.png", big_png, "image/png")},
        )
        assert r.status_code == 200, r.text
        avatar1 = r.json()
        assert avatar1["avatar_url"] is not None
        assert avatar1["avatar_url"].startswith("/uploads/")

        # The stored file is a 500x500 WebP, not the original PNG.
        r = c.get(avatar1["avatar_url"])
        assert r.status_code == 200
        assert r.content[:4] == WEBP_MAGIC
        img = Image.open(io.BytesIO(r.content))
        assert img.format == "WEBP"
        assert img.size == (500, 500), img.size

        # The avatar is visible in the family list.
        r = c.get("/api/families", headers=auth_header(admin_token))
        fam1_now = [f for f in r.json() if f["id"] == fam1["id"]][0]
        assert fam1_now["avatar_url"] == avatar1["avatar_url"]

        # Replace it: the old file should be gone, the new one set.
        r = c.post(
            f"/api/families/{fam1['id']}/avatar",
            headers=auth_header(admin_token),
            files={"file": ("family2.png", make_png(640, 640, color=(30, 120, 200)), "image/png")},
        )
        assert r.status_code == 200, r.text
        avatar2 = r.json()
        assert avatar2["avatar_url"] != avatar1["avatar_url"]

        # Old file deleted (no revisions kept) — the proxy now serves a 200
        # placeholder (not a 404) so the WAF/browser never see a miss.
        r = c.get(avatar1["avatar_url"])
        assert r.status_code == 200
        assert r.headers.get("X-File-Missing") == "true"
        # New file served (also a 500x500 WebP).
        r = c.get(avatar2["avatar_url"])
        assert r.status_code == 200
        assert r.headers.get("X-File-Missing") is None
        assert r.content[:4] == WEBP_MAGIC
        assert Image.open(io.BytesIO(r.content)).size == (500, 500)

        # Non-image is rejected.
        r = c.post(
            f"/api/families/{fam1['id']}/avatar",
            headers=auth_header(admin_token),
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        )
        assert r.status_code == 415

        # A declared image that isn't actually decodable is rejected too.
        r = c.post(
            f"/api/families/{fam1['id']}/avatar",
            headers=auth_header(admin_token),
            files={"file": ("corrupt.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")},
        )
        assert r.status_code == 415

        # Avatar on a non-existent family.
        r = c.post(
            "/api/families/9999/avatar",
            headers=auth_header(admin_token),
            files={"file": ("x.png", big_png, "image/png")},
        )
        assert r.status_code == 404

        # Delete the empty family -> ok.
        r = c.delete(f"/api/families/{fam2['id']}", headers=auth_header(admin_token))
        assert r.status_code == 200

        # Deleting a family unassigns any pending invites pointing at it.
        r = c.get("/api/families", headers=auth_header(admin_token))
        assert all(f["id"] != fam2["id"] for f in r.json())

    print("FAMILIES OK")


def test_admin_gating():
    """Non-admins cannot create/edit/delete families or create/revoke invites;
    they can still list families, set a family avatar, and see only their own
    join-invite."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"handle": "admin", "password": "adminpass123"})
        admin_token = r.json()["token"]

        # Admin creates a family + an invite for a second user.
        r = c.post("/api/families", headers=auth_header(admin_token), json={"name": "Holsapples"})
        assert r.status_code == 201
        fam = r.json()
        r = c.post(
            "/api/invites",
            headers=auth_header(admin_token),
            json={"max_uses": 3, "family_id": fam["id"], "note": "for the family"},
        )
        assert r.status_code == 201
        invite = r.json()

        # A non-admin joins via that invite.
        r = c.post(
            "/api/auth/register",
            json={
                "invite_code": invite["code"],
                "handle": "alice",
                "password": "alice123",
                "display_name": "Alice",
            },
        )
        assert r.status_code == 200
        alice = r.json()["user"]
        alice_token = r.json()["token"]
        assert alice["is_admin"] is False

        # Admin is flagged as admin.
        r = c.get("/api/auth/me", headers=auth_header(admin_token))
        assert r.json()["is_admin"] is True

        # Non-admin is blocked from creating a family.
        r = c.post("/api/families", headers=auth_header(alice_token), json={"name": "Secret"})
        assert r.status_code == 403

        # Non-admin is blocked from editing a family.
        r = c.put(f"/api/families/{fam['id']}", headers=auth_header(alice_token), json={"name": "X"})
        assert r.status_code == 403

        # Non-admin is blocked from deleting a family.
        r = c.delete(f"/api/families/{fam['id']}", headers=auth_header(alice_token))
        assert r.status_code == 403

        # Non-admin is blocked from creating an invite.
        r = c.post("/api/invites", headers=auth_header(alice_token), json={"max_uses": 1})
        assert r.status_code == 403

        # Non-admin is blocked from revoking an invite.
        r = c.delete(f"/api/invites/{invite['id']}", headers=auth_header(alice_token))
        assert r.status_code == 403

        # Non-admin CAN list families (read-only).
        r = c.get("/api/families", headers=auth_header(alice_token))
        assert r.status_code == 200
        assert any(f["id"] == fam["id"] for f in r.json())

        # Non-admin CAN set a family avatar (members may update it).
        r = c.post(
            f"/api/families/{fam['id']}/avatar",
            headers=auth_header(alice_token),
            files={"file": ("fam.png", make_png(800, 600, color=(20, 160, 60)), "image/png")},
        )
        assert r.status_code == 200

        # Non-admin sees ONLY their own join-invite (not the admin's full list).
        r = c.get("/api/invites", headers=auth_header(alice_token))
        assert r.status_code == 200
        listing = r.json()
        assert len(listing) == 1
        assert listing[0]["code"] == invite["code"]
        assert listing[0]["family_name"] == "Holsapples"

        # Admin still sees all invites.
        r = c.get("/api/invites", headers=auth_header(admin_token))
        assert r.status_code == 200
        assert any(i["code"] == invite["code"] for i in r.json())

    print("ADMIN GATING OK")
