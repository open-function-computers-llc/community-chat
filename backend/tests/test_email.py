"""Email notification tests.

We don't hit a real SMTP server; instead we monkeypatch the SMTP send in
app.routers.email to record what would have been sent. SMTP_HOST + SMTP_FROM_ADDRESS
are set for the test process so email_enabled() is true.

Mirrors test_push.py: the fan-out functions are tested directly (synchronously)
to avoid background-task timing issues, plus a register-time invite test.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["SMTP_HOST"] = "smtp.test"
os.environ["SMTP_PORT"] = "1025"
os.environ["SMTP_USERNAME"] = "user"
os.environ["SMTP_PASSWORD"] = "pass"
os.environ["SMTP_FROM_ADDRESS"] = "noreply@test.example"
os.environ["SMTP_FROM_NAME"] = "Test Chat"
os.environ["APP_ORIGIN"] = "http://localhost:8983"

_tmp = tempfile.mkdtemp(prefix="chat-email-test-")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DATA_DIR"] = _tmp
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import DMSettings, Invite, User  # noqa: E402
from app.routers import email as email_mod  # noqa: E402


def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return TestClient(app)


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def _make_user(c, handle, email=None, admin=False, invite=None):
    if invite is None:
        db = SessionLocal()
        db.add(Invite(code="EMAIL" + handle, max_uses=5, times_used=0))
        db.commit()
        db.close()
        invite = "EMAIL" + handle
    r = c.post(
        "/api/auth/register",
        json={
            "invite_code": invite,
            "handle": handle,
            "password": "secret123",
            "display_name": handle.title(),
            "email": email,
        },
    )
    assert r.status_code in (200, 201), r.text
    data = r.json()
    if admin:
        db = SessionLocal()
        u = db.query(User).filter(User.handle == handle).first()
        u.is_admin = True
        db.commit()
        db.close()
    return data


def _opt_in(handle, enabled=True):
    db = SessionLocal()
    u = db.query(User).filter(User.handle == handle).first()
    if u.dm_settings is None:
        u.dm_settings = DMSettings(user_id=u.id)
    u.dm_settings.email_notifications = enabled
    db.commit()
    db.close()


def _patch_send(monkeypatch, sink):
    def fake(subject, html, to_addr):
        sink.append({"to": to_addr, "subject": subject, "html": html})
        return True

    monkeypatch.setattr(email_mod, "send_email", fake)


def test_email_status_enabled():
    c = client()
    info = _make_user(c, "stat", email="stat@test.example", admin=True)
    r = c.get("/api/email/status", headers=auth(info["token"]))
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "has_email": True}


def test_email_status_no_email():
    c = client()
    info = _make_user(c, "noemail", admin=True)
    r = c.get("/api/email/status", headers=auth(info["token"]))
    assert r.json() == {"enabled": True, "has_email": False}


def test_settings_email_notifications_roundtrip():
    c = client()
    info = _make_user(c, "sara", email="sara@test.example", admin=True)
    r = c.put(
        "/api/auth/me/settings",
        headers=auth(info["token"]),
        json={"do_not_disturb": False, "notify_mentions": True, "notify_replies": True, "email_notifications": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email_notifications"] is True
    r = c.get("/api/auth/me/settings", headers=auth(info["token"]))
    assert r.json()["email_notifications"] is True


def test_notify_group_only_opt_in(monkeypatch):
    sink: list[dict] = []
    _patch_send(monkeypatch, sink)
    # Silence the register-time welcome/invite emails so only the direct
    # notify_* call below is observed.
    monkeypatch.setattr(email_mod, "fire_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(email_mod, "fire_invite_email", lambda *a, **k: None)

    c = client()
    _make_user(c, "bob", email="bob@test.example", admin=True)
    _make_user(c, "cara", email="cara@test.example")
    _make_user(c, "dan", email=None)  # no email -> never
    # bob opt-in; cara opt-out; dan no email.
    _opt_in("bob", True)
    _opt_in("cara", False)

    a = _make_user(c, "alice", email="alice@test.example")  # sender
    sender_id = a["user"]["id"]

    db = SessionLocal()
    email_mod.notify_group(db, sender_id, "Alice", "hello everyone")
    db.close()

    # Only bob (opt-in + has email) gets an email; not the sender, not opt-out, not no-email.
    assert [s["to"] for s in sink] == ["bob@test.example"]
    assert "New message" in sink[0]["subject"]


def test_notify_room_only_opt_in(monkeypatch):
    sink: list[dict] = []
    _patch_send(monkeypatch, sink)
    monkeypatch.setattr(email_mod, "fire_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(email_mod, "fire_invite_email", lambda *a, **k: None)

    c = client()
    admin = _make_user(c, "root", email="root@test.example", admin=True)
    t1 = _make_user(c, "mia", email="mia@test.example", invite="EMAILroot")
    t2 = _make_user(c, "noah", email="noah@test.example", invite="EMAILroot")
    mia_id, noah_id = t1["user"]["id"], t2["user"]["id"]
    # noah opt-in, mia opt-out. Sender = mia.
    _opt_in("noah", True)
    _opt_in("mia", False)

    db = SessionLocal()
    email_mod.notify_room(db, {mia_id, noah_id}, mia_id, "Mia", "hey", "Family")
    db.close()

    # Only noah (opt-in, has email, not the sender) is emailed.
    assert [s["to"] for s in sink] == ["noah@test.example"]


def test_notify_room_sender_excluded(monkeypatch):
    sink: list[dict] = []
    _patch_send(monkeypatch, sink)
    monkeypatch.setattr(email_mod, "fire_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(email_mod, "fire_invite_email", lambda *a, **k: None)

    c = client()
    admin = _make_user(c, "root", email="root@test.example", admin=True)
    t1 = _make_user(c, "mia", email="mia@test.example", invite="EMAILroot")
    t2 = _make_user(c, "noah", email="noah@test.example", invite="EMAILroot")
    mia_id, noah_id = t1["user"]["id"], t2["user"]["id"]
    _opt_in("noah", True)
    _opt_in("mia", True)  # both opt-in, but sender is excluded

    db = SessionLocal()
    email_mod.notify_room(db, {mia_id, noah_id}, mia_id, "Mia", "hey", "Family")
    db.close()

    assert [s["to"] for s in sink] == ["noah@test.example"]


def test_disabled_when_no_smtp_host(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    sink: list[dict] = []
    _patch_send(monkeypatch, sink)

    c = client()
    _make_user(c, "bob", email="bob@test.example", admin=True)
    _opt_in("bob", True)
    a = _make_user(c, "alice", email="alice@test.example")
    db = SessionLocal()
    email_mod.notify_group(db, a["user"]["id"], "Alice", "hi")
    db.close()
    assert sink == []  # email_enabled() False -> send_email returns False without recording


def test_register_fires_welcome_and_invite(monkeypatch):
    welcome: list[str] = []
    invites: list[str] = []
    monkeypatch.setattr(
        email_mod, "fire_welcome_email", lambda to, name: welcome.append(to)
    )
    monkeypatch.setattr(
        email_mod, "fire_invite_email", lambda to, name, code, link, fam: invites.append(to)
    )

    c = client()
    # Admin creates an invite (so we can register a new member with it).
    admin = _make_user(c, "admin", email="admin@test.example", admin=True)
    r = c.post("/api/invites", headers=auth(admin["token"]), json={"max_uses": 5})
    assert r.status_code == 201
    code = r.json()["code"]

    r = c.post(
        "/api/auth/register",
        json={"invite_code": code, "handle": "newbie", "password": "secret123", "email": "newbie@test.example"},
    )
    assert r.status_code in (200, 201), r.text

    assert "newbie@test.example" in welcome
    assert "admin@test.example" in invites


def test_render_invite_contains_code_and_link():
    html = email_mod.invite_html("Dave", "ABC123", "http://localhost:8983/login?code=ABC123", "The Alfords")
    assert "ABC123" in html
    assert "/login?code=ABC123" in html
    assert "Alfords" in html


def test_render_welcome():
    html = email_mod.welcome_html("Dana")
    assert "Dana" in html
    assert "http://localhost:8983/" in html
