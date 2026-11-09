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
from app.models import ChannelNotification, Invite, User  # noqa: E402
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


def _opt_in(handle, enabled=True, room_id=None):
    """Toggle immediate email for a channel (room_id None = group chat)."""
    db = SessionLocal()
    u = db.query(User).filter(User.handle == handle).first()
    cond = (
        ChannelNotification.room_id.is_(None)
        if room_id is None
        else ChannelNotification.room_id == room_id
    )
    row = db.query(ChannelNotification).filter(
        ChannelNotification.user_id == u.id, cond
    ).first()
    if row is None:
        row = ChannelNotification(user_id=u.id, room_id=room_id)
        db.add(row)
    row.immediate_email = enabled
    db.commit()
    db.close()


def _patch_send(monkeypatch, sink):
    def fake(subject, html, to_addr, text=None):
        sink.append({"to": to_addr, "subject": subject, "html": html, "text": text})
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


def test_notify_group_only_opt_in(monkeypatch):
    sink: list[dict] = []
    _patch_send(monkeypatch, sink)
    # Silence the register-time welcome/invite emails so only the direct
    # notify_* call below is observed.
    monkeypatch.setattr(email_mod, "fire_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(email_mod, "fire_invite_used_email", lambda *a, **k: None)

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
    monkeypatch.setattr(email_mod, "fire_invite_used_email", lambda *a, **k: None)

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
    monkeypatch.setattr(email_mod, "fire_invite_used_email", lambda *a, **k: None)

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


def test_brand_illustration_is_inlined(monkeypatch):
    # The unDraw brand art ships as an inline CID part (remote URLs get
    # blocked by client image privacy), referenced by every framed email.
    from pathlib import Path as _Path

    assert email_mod.BRAND_IMAGE.exists(), "brand asset must ship with the backend"
    html = email_mod.welcome_html("Sam")
    assert f"cid:{email_mod.BRAND_CID}" in html

    msg = email_mod._build_message("subj", html, "a@b.c")
    images = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(images) == 1
    assert images[0].get("Content-ID") == f"<{email_mod.BRAND_CID}>"
    assert images[0].get_payload(decode=True) == email_mod.BRAND_IMAGE.read_bytes()

    # Missing asset -> no cid reference in the HTML (no broken-image icon)
    # and nothing attached.
    monkeypatch.setattr(email_mod, "BRAND_IMAGE", _Path("/nonexistent/undraw.png"))
    html = email_mod.welcome_html("Sam")
    assert "cid:" not in html
    msg = email_mod._build_message("subj", html, "a@b.c")
    assert [p for p in msg.walk() if p.get_content_type() == "image/png"] == []


def test_community_name_brands_email_copy(monkeypatch):
    # COMMUNITY_NAME drives the reader-facing brand ("{name} Chat") across
    # subjects, body copy, footers, and the masthead alt — while the CTA
    # buttons stay generic product wording by design.
    monkeypatch.setenv("COMMUNITY_NAME", "Whimsy and Wonder")

    welcome = _norm(email_mod.welcome_html("Cy"))
    assert "Welcome to Whimsy and Wonder Chat" in welcome
    assert "Whimsy and Wonder Chat" in welcome
    assert "Powered by Community Chat" in welcome
    assert 'alt="Whimsy and Wonder Chat"' in welcome

    invite = _norm(email_mod.invite_html("there", "CODE1", "http://x/l?code=CODE1", None))
    assert "invited to join Whimsy and Wonder Chat." in invite
    assert "Join Community Chat" in invite  # button stays generic
    assert "Powered by Community Chat" in invite

    # Subjects read the brand live (not frozen at import).
    from app import summary as summary_mod

    assert summary_mod._scope_subject("nightly") == "Your Whimsy and Wonder Chat daily summary"

    # No COMMUNITY_NAME: plain product name, never the doubled brand.
    monkeypatch.delenv("COMMUNITY_NAME")
    welcome = _norm(email_mod.welcome_html("Cy"))
    assert "Welcome to Community Chat" in welcome
    assert "Community Chat Chat" not in welcome
    assert "Powered by Community Chat" in welcome


def test_register_fires_welcome_and_invite(monkeypatch):
    welcome: list[str] = []
    invites: list[str] = []
    monkeypatch.setattr(
        email_mod, "fire_welcome_email", lambda to, name: welcome.append(to)
    )
    monkeypatch.setattr(
        email_mod, "fire_invite_used_email", lambda to, creator, joiner, fam: invites.append(to)
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


def test_templates_escape_untrusted_text():
    # %%MERGE_TAG%% values are escaped by default: user-controlled text can
    # never inject markup, even if a call site forgets to escape.
    html = email_mod._group_html("<script>alert(1)</script>", "a > b & c")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "a &gt; b &amp; c" in html
    html = email_mod.invite_html("Ben <b>", "CODE1", "http://x/?a=1&b=2", "A & B")
    assert "Ben &lt;b&gt;" in html
    assert "A &amp; B" in html
    assert "a=1&amp;b=2" in html  # attribute context too


def test_templates_render_all_bodies():
    # Smoke: every shipped template merges without leftover %%TAG%%s.
    for html in (
        email_mod._group_html("Al", "hi"),
        email_mod._group_html("Al", "hi", room_label="The Alfords"),
        email_mod.invite_html("Bo", "X", "http://x", "Fam"),
        email_mod.invite_html("Bo", "X", "http://x", None),
        email_mod.invite_used_html("Root", "Bo", "Fam"),
        email_mod.invite_used_html("Root", "Bo", None),
        email_mod.welcome_html("Cy"),
        email_mod.welcome_html(""),
        email_mod.password_reset_html("Di", "pw", "di"),
        email_mod.test_email_html(),
    ):
        assert "%%" not in html


SETTINGS_FOOTER = "Manage or stop them in Settings."


def _norm(html: str) -> str:
    # Collapse whitespace so source line-wrapping in the templates doesn't
    # affect content assertions (HTML renders it collapsed anyway).
    return " ".join(html.split())


def test_footer_matches_each_audience():
    # Only opted-in members get the "notifications / Settings" footer; every
    # other audience gets a footer that actually explains why they're listed.
    group = _norm(email_mod._group_html("Al", "hi"))
    assert "you turned on email notifications" in group
    assert SETTINGS_FOOTER in group

    invite = _norm(email_mod.invite_html("there", "CODE1", "http://x/login?code=CODE1", None))
    assert "invited to join the chat application for your community" in invite
    assert SETTINGS_FOOTER not in invite
    assert "you turned on email notifications" not in invite

    used = _norm(email_mod.invite_used_html("Root", "Newbie", "The Alfords"))
    assert "someone used an invite you" in used
    assert "Newbie" in used and "Root" in used and "The Alfords" in used
    assert SETTINGS_FOOTER not in used

    reset = _norm(email_mod.password_reset_html("Di", "pw", "di"))
    assert "administrator reset your" in reset and "Community" in reset
    assert SETTINGS_FOOTER not in reset

    welcome = _norm(email_mod.welcome_html("Cy"))
    assert "just created your" in welcome and "Community Chat account" in welcome
    assert SETTINGS_FOOTER not in welcome

    test = _norm(email_mod.test_email_html())
    assert "delivery test" in test
    assert SETTINGS_FOOTER not in test


def test_send_email_uses_matching_plaintext(monkeypatch):
    # The text/plain part must match the audience too (some clients show it).
    captured: list[dict] = []

    def fake_smtp(subject, html, to_addr, text=None):
        captured.append({"to": to_addr, "text": text})

    monkeypatch.setattr(email_mod, "_send_smtp", fake_smtp)
    monkeypatch.setattr(email_mod, "email_enabled", lambda: True)

    email_mod.send_email("s", "h", "a@b.c")  # default = member notifications
    assert captured[0]["text"] == email_mod.TEXT_NOTIFICATIONS
    email_mod.send_email("s", "h", "a@b.c", email_mod.TEXT_INVITE)
    assert captured[1]["text"] == email_mod.TEXT_INVITE

    # The admin invite endpoint sends with the invite plaintext.
    sink: list[dict] = []
    _patch_send(monkeypatch, sink)
    c = client()
    admin = _make_user(c, "root2", admin=True)
    r = c.post(
        "/api/invites", headers=auth(admin["token"]), json={"max_uses": 1}
    )
    inv = r.json()
    r = c.post(
        f"/api/invites/{inv['id']}/email",
        headers=auth(admin["token"]),
        json={"emails": ["someone@example.com"]},
    )
    assert r.status_code == 200
    assert sink[0]["text"] == email_mod.TEXT_INVITE
    assert "invited to join the chat application" in _norm(sink[0]["html"])


# --- opportunistic STARTTLS (Mailpit-safety regression) --------------------
class _StubSMTP:
    """Stand-in for smtplib.SMTP. Set _StubSMTP.supports_starttls to control
    whether STARTTLS is advertised; when it isn't, starttls() raises exactly
    like smtplib does against a plaintext sink such as Mailpit."""

    instances: list = []
    supports_starttls = True

    def __init__(self, host, port, timeout=None):
        self.calls = []
        type(self).instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return name == "starttls" and type(self).supports_starttls

    def starttls(self, *args, **kwargs):
        if not type(self).supports_starttls:
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"]))

    def quit(self):
        self.calls.append("quit")


def test_send_smtp_upgrades_when_starttls_advertised(monkeypatch):
    import smtplib

    _StubSMTP.instances = []
    _StubSMTP.supports_starttls = True
    monkeypatch.setattr(email_mod.smtplib, "SMTP", _StubSMTP)
    assert email_mod.send_email("subject", "<p>hi</p>", "someone@example.com") is True
    srv = _StubSMTP.instances[-1]
    assert "starttls" in srv.calls
    assert ("send", "someone@example.com") in srv.calls


def test_send_smtp_plaintext_when_starttls_not_supported(monkeypatch):
    # Regression: Mailpit (dev SMTP sink) doesn't advertise STARTTLS. An
    # unconditional starttls() raised SMTPNotSupportedError and every dev
    # email silently failed. Plaintext must proceed normally.
    import smtplib

    _StubSMTP.instances = []
    _StubSMTP.supports_starttls = False
    monkeypatch.setattr(email_mod.smtplib, "SMTP", _StubSMTP)
    assert email_mod.send_email("subject", "<p>hi</p>", "someone@example.com") is True
    srv = _StubSMTP.instances[-1]
    assert "starttls" not in srv.calls
    assert ("send", "someone@example.com") in srv.calls


# --- admin "email this invite" ---------------------------------------------
def _make_admin_invite(c, monkeypatch, family_name=None, max_uses=2):
    """Common setup: an admin, an optional family, and one invite."""
    sink: list[dict] = []
    _patch_send(monkeypatch, sink)
    c = client()
    admin = _make_user(c, "root", admin=True)
    fam_id = None
    if family_name:
        r = c.post("/api/families", headers=auth(admin["token"]), json={"name": family_name})
        assert r.status_code == 201
        fam_id = r.json()["id"]
    r = c.post(
        "/api/invites",
        headers=auth(admin["token"]),
        json={"max_uses": max_uses, "family_id": fam_id},
    )
    assert r.status_code == 201
    return c, admin, r.json(), sink


def test_email_invite_sends_to_unique_addresses(monkeypatch):
    c, admin, inv, sink = _make_admin_invite(c=None, monkeypatch=monkeypatch, family_name="The Alfords")

    r = c.post(
        f"/api/invites/{inv['id']}/email",
        headers=auth(admin["token"]),
        json={"emails": ["grandma@example.com", "grandpa@example.com", "grandma@example.com"]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "sent": 2, "failed": [], "total": 2}
    assert [s["to"] for s in sink] == ["grandma@example.com", "grandpa@example.com"]
    assert sink[0]["subject"] == "You're invited to Community Chat"
    # Uses the shared invite template: code, join link, and family name.
    assert inv["code"] in sink[0]["html"]
    assert f"/login?code={inv['code']}" in sink[0]["html"]
    assert "The Alfords" in sink[0]["html"]


def test_email_invite_rejects_bad_address(monkeypatch):
    c, admin, inv, sink = _make_admin_invite(c=None, monkeypatch=monkeypatch)
    r = c.post(
        f"/api/invites/{inv['id']}/email",
        headers=auth(admin["token"]),
        json={"emails": ["not-an-email"]},
    )
    assert r.status_code == 422
    assert sink == []


def test_email_invite_used_up_invite_rejected(monkeypatch):
    c, admin, inv, sink = _make_admin_invite(c=None, monkeypatch=monkeypatch, max_uses=1)
    assert c.delete(f"/api/invites/{inv['id']}", headers=auth(admin["token"])).status_code == 200
    r = c.post(
        f"/api/invites/{inv['id']}/email",
        headers=auth(admin["token"]),
        json={"emails": ["someone@example.com"]},
    )
    assert r.status_code == 400
    assert "used up" in r.json()["detail"]
    assert sink == []


def test_email_invite_requires_admin(monkeypatch):
    c, admin, inv, sink = _make_admin_invite(c=None, monkeypatch=monkeypatch)
    regular = _make_user(c, "carla")
    r = c.post(
        f"/api/invites/{inv['id']}/email",
        headers=auth(regular["token"]),
        json={"emails": ["someone@example.com"]},
    )
    assert r.status_code == 403
    assert sink == []


def test_email_invite_requires_smtp_config(monkeypatch):
    from app.routers import invites as invites_mod

    monkeypatch.setattr(invites_mod, "email_enabled", lambda: False)
    c, admin, inv, sink = _make_admin_invite(c=None, monkeypatch=monkeypatch)
    r = c.post(
        f"/api/invites/{inv['id']}/email",
        headers=auth(admin["token"]),
        json={"emails": ["someone@example.com"]},
    )
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]
    assert sink == []
