"""Channel notification settings + Channel Summary digest tests.

Covers the /api/notifications/channels API (list/create/update, room
membership checks), the per-channel gating of immediate fan-out (email +
push), digest collection windows/membership, and the pure scheduler clock.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["SMTP_HOST"] = "smtp.test"
os.environ["SMTP_PORT"] = "1025"
os.environ["SMTP_USERNAME"] = "user"
os.environ["SMTP_PASSWORD"] = "pass"
os.environ["SMTP_FROM_ADDRESS"] = "noreply@test.example"
os.environ["SMTP_FROM_NAME"] = "Test Chat"
os.environ["APP_ORIGIN"] = "http://localhost:8983"
os.environ["VAPID_PUBLIC_KEY"] = "testpub"
os.environ["VAPID_PRIVATE_KEY"] = "testpriv"
os.environ["VAPID_EMAIL"] = "mailto:test@test.example"
os.environ["SUMMARY_ENABLED"] = "0"  # never start the loop in tests

_tmp = tempfile.mkdtemp(prefix="chat-notif-test-")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DATA_DIR"] = _tmp
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import (  # noqa: E402
    Base,
    ChannelNotification,
    DmRoom,
    DmRoomFamily,
    Family,
    GroupMessage,
    PushSubscription,
    RoomMessage,
    SessionLocal,
    User,
    engine,
    utcnow,
)
from app.models import Invite  # noqa: E402
from app.routers import email as email_mod  # noqa: E402
from app.routers import push as push_mod  # noqa: E402
from app import summary as summary_mod  # noqa: E402


def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return TestClient(app)


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def _make_user(c, handle, email=None, family=None):
    """Register via a per-handle invite; optionally attach to a family
    (created on first use)."""
    db = SessionLocal()
    db.add(Invite(code="N" + handle, max_uses=5, times_used=0))
    db.commit()
    db.close()
    r = c.post(
        "/api/auth/register",
        json={
            "invite_code": "N" + handle,
            "handle": handle,
            "password": "secret123",
            "display_name": handle.title(),
            "email": email,
        },
    )
    assert r.status_code in (200, 201), r.text
    data = r.json()
    if family is not None:
        db = SessionLocal()
        u = db.query(User).filter(User.handle == handle).first()
        fam = db.query(Family).filter(Family.name == family).first()
        if fam is None:
            fam = Family(name=family)
            db.add(fam)
            db.flush()
        u.family_id = fam.id
        db.commit()
        db.close()
    return data


def _create_room(*family_names):
    db = SessionLocal()
    room = DmRoom()
    db.add(room)
    db.flush()
    for name in family_names:
        fam = db.query(Family).filter(Family.name == name).first()
        if fam is None:
            fam = Family(name=name)
            db.add(fam)
            db.flush()
        db.add(DmRoomFamily(room_id=room.id, family_id=fam.id))
    db.commit()
    rid = room.id
    db.close()
    return rid


def _channel_row(user_id, room_id=None, **flags):
    db = SessionLocal()
    row = ChannelNotification(user_id=user_id, room_id=room_id)
    for k, v in flags.items():
        setattr(row, k, v)
    db.add(row)
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Channels API
# ---------------------------------------------------------------------------
def test_channels_list_defaults():
    c = client()
    me = _make_user(c, "solo", email="solo@test.example")
    r = c.get("/api/notifications/channels", headers=auth(me["token"]))
    assert r.status_code == 200
    channels = r.json()
    assert len(channels) == 1
    assert channels[0]["room_id"] is None
    assert channels[0]["name"] == "Group Chat"
    assert channels[0]["settings"] == {
        "immediate_email": False,
        "immediate_push": True,
        "nightly_email": False,
        "nightly_push": False,
        "weekly_email": False,
        "weekly_push": False,
    }


def test_channels_include_family_rooms():
    c = client()
    mia = _make_user(c, "mia", family="Alfords")
    _make_user(c, "noah", family="Davids")
    _create_room("Alfords", "Davids")
    r = c.get("/api/notifications/channels", headers=auth(mia["token"]))
    names = [(ch["room_id"], ch["name"]) for ch in r.json()]
    assert names[0] == (None, "Group Chat")
    assert names[1][1] == "Alfords & Davids"
    assert names[1][0] is not None


def test_update_channel_creates_then_updates_row():
    c = client()
    me = _make_user(c, "solo")
    body = {
        "room_id": None,
        "immediate_email": True,
        "immediate_push": False,
        "nightly_email": True,
        "nightly_push": True,
        "weekly_email": False,
        "weekly_push": True,
    }
    r = c.put("/api/notifications/channels", headers=auth(me["token"]), json=body)
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["weekly_push"] is True

    r = c.put(
        "/api/notifications/channels",
        headers=auth(me["token"]),
        json={**body, "nightly_email": False},
    )
    assert r.status_code == 200
    assert r.json()["settings"]["nightly_email"] is False

    db = SessionLocal()
    u = db.query(User).filter(User.handle == "solo").first()
    rows = db.query(ChannelNotification).filter(ChannelNotification.user_id == u.id).all()
    assert len(rows) == 1  # update, not duplicate
    assert rows[0].immediate_email is True and rows[0].nightly_email is False
    db.close()


def test_update_room_channel_requires_membership():
    c = client()
    mia = _make_user(c, "mia", family="Alfords")
    zed = _make_user(c, "zed", family="Outsiders")
    room_id = _create_room("Alfords", "Davids")
    body = {
        "room_id": room_id,
        "immediate_email": False,
        "immediate_push": True,
        "nightly_email": True,
        "nightly_push": False,
        "weekly_email": False,
        "weekly_push": False,
    }
    # Not in the room -> 403.
    r = c.put("/api/notifications/channels", headers=auth(zed["token"]), json=body)
    assert r.status_code == 403
    # Unknown room -> 404.
    r = c.put("/api/notifications/channels", headers=auth(mia["token"]), json={**body, "room_id": 99999})
    assert r.status_code == 404
    # Member -> ok.
    r = c.put("/api/notifications/channels", headers=auth(mia["token"]), json=body)
    assert r.status_code == 200
    assert r.json()["settings"]["nightly_email"] is True


# ---------------------------------------------------------------------------
# Immediate fan-out gating
# ---------------------------------------------------------------------------
def test_group_push_default_on_and_opt_out(monkeypatch):
    sent: list[tuple[int, dict]] = []
    monkeypatch.setattr(
        push_mod,
        "_deliver",
        lambda info, user_id, payload: (sent.append((user_id, payload)), "ok")[1],
    )
    c = client()
    a = _make_user(c, "alice")
    b = _make_user(c, "bob")
    d = _make_user(c, "dan")
    db = SessionLocal()
    for uid in (b["user"]["id"], d["user"]["id"]):
        db.add(PushSubscription(endpoint=f"e{uid}", p256dh="k", auth="a", user_id=uid))
    db.commit()
    db.close()

    db = SessionLocal()
    n = push_mod.notify_group(db, a["user"]["id"], {"title": "t", "body": "b", "tag": "g"})
    db.close()
    assert n == 2 and sorted(u for u, _ in sent) == [b["user"]["id"], d["user"]["id"]]

    sent.clear()
    _channel_row(d["user"]["id"], None, immediate_push=False)
    db = SessionLocal()
    n = push_mod.notify_group(db, a["user"]["id"], {"title": "t", "body": "b", "tag": "g2"})
    db.close()
    assert n == 1 and sent[0][0] == b["user"]["id"]


def test_room_push_is_per_room(monkeypatch):
    sent: list[tuple[int, dict]] = []
    monkeypatch.setattr(
        push_mod,
        "_deliver",
        lambda info, user_id, payload: (sent.append((user_id, payload)), "ok")[1],
    )
    c = client()
    mia = _make_user(c, "mia", family="Alfords")
    noah = _make_user(c, "noah", family="Alfords")
    room_id = _create_room("Alfords")
    db = SessionLocal()
    db.add(PushSubscription(endpoint="e-noah", p256dh="k", auth="a", user_id=noah["user"]["id"]))
    db.commit()
    db.close()

    # Push is off for THIS room (the group row stays on) -> no send.
    _channel_row(noah["user"]["id"], None, immediate_push=True)
    _channel_row(noah["user"]["id"], room_id, immediate_push=False)
    db = SessionLocal()
    n = push_mod.notify_room(
        db, {mia["user"]["id"], noah["user"]["id"]}, mia["user"]["id"],
        {"title": "t", "body": "b", "tag": "r"}, room_id=room_id,
    )
    db.close()
    assert n == 0 and sent == []

    # And the same user IS pushed in the group channel.
    db = SessionLocal()
    n = push_mod.notify_group(db, mia["user"]["id"], {"title": "t", "body": "b", "tag": "g"})
    db.close()
    assert n == 1


def test_room_email_is_per_room(monkeypatch):
    sink: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda subject, html, to_addr, text=None: (
        sink.append({"to": to_addr, "subject": subject, "text": text}), True)[1])
    monkeypatch.setattr(email_mod, "fire_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(email_mod, "fire_invite_used_email", lambda *a, **k: None)

    c = client()
    mia = _make_user(c, "mia", email="mia@test.example", family="Alfords")
    noah = _make_user(c, "noah", email="noah@test.example", family="Alfords")
    room_id = _create_room("Alfords")
    # Noah opted into group email only — the room email must not fire.
    _channel_row(noah["user"]["id"], None, immediate_email=True)
    db = SessionLocal()
    email_mod.notify_room(
        db, {mia["user"]["id"], noah["user"]["id"]}, mia["user"]["id"],
        "Mia", "hey", "Alfords", room_id=room_id,
    )
    db.close()
    assert sink == []
    # Opt the room in -> fires.
    _channel_row(noah["user"]["id"], room_id, immediate_email=True)
    db = SessionLocal()
    email_mod.notify_room(
        db, {mia["user"]["id"], noah["user"]["id"]}, mia["user"]["id"],
        "Mia", "hey", "Alfords", room_id=room_id,
    )
    db.close()
    assert [s["to"] for s in sink] == ["noah@test.example"]


# ---------------------------------------------------------------------------
# Digest collection
# ---------------------------------------------------------------------------
def _post_group(author_id, text, ago):
    db = SessionLocal()
    db.add(GroupMessage(author_id=author_id, text=text, created_at=utcnow() - ago))
    db.commit()
    db.close()


def _post_room(room_id, sender_id, text, ago):
    db = SessionLocal()
    db.add(RoomMessage(room_id=room_id, sender_id=sender_id, text=text, created_at=utcnow() - ago))
    db.commit()
    db.close()


def test_collect_nightly_group_window_and_render():
    c = client()
    alice = _make_user(c, "alice", family="Alfords")
    bob = _make_user(c, "bob", email="bob@test.example", family="Alfords")
    room_id = _create_room("Alfords")
    _channel_row(bob["user"]["id"], None, nightly_email=True)
    _post_group(alice["user"]["id"], "recent hello", timedelta(hours=3))
    _post_group(alice["user"]["id"], "ancient news", timedelta(days=3))
    _post_group(bob["user"]["id"], "own message still counts", timedelta(hours=1))

    db = SessionLocal()
    email_batch, push_batch = summary_mod.collect_summaries(db, "nightly")
    db.close()
    assert push_batch == []  # nightly_push not enabled
    assert len(email_batch) == 1
    subject, html, addr, _text = email_batch[0]
    assert addr == "bob@test.example"
    assert "daily summary" in subject
    assert "Group Chat" in html and "(2 new)" in html  # window excludes the old one
    assert "recent hello" in html and "ancient news" not in html
    assert "turned on Channel" in html  # summary footer


def test_collect_weekly_rooms_membership_and_push():
    c = client()
    mia = _make_user(c, "mia", family="Alfords")
    noah = _make_user(c, "noah", email="noah@test.example", family="Davids")
    room_shared = _create_room("Alfords", "Davids")
    room_private = _create_room("Alfords")
    _channel_row(noah["user"]["id"], room_shared, weekly_email=True, weekly_push=True)
    db = SessionLocal()
    db.add(PushSubscription(endpoint="e-noah", p256dh="k", auth="a", user_id=noah["user"]["id"]))
    db.commit()
    db.close()
    _post_room(room_shared, mia["user"]["id"], "shared room hello", timedelta(days=2))
    _post_room(room_private, mia["user"]["id"], "private family business", timedelta(days=2))
    _post_room(room_shared, mia["user"]["id"], "ancient out-of-window news", timedelta(days=9))

    db = SessionLocal()
    email_batch, push_batch = summary_mod.collect_summaries(db, "weekly")
    db.close()
    assert len(email_batch) == 1
    subject, html, addr, _text = email_batch[0]
    assert addr == "noah@test.example" and "weekly" in subject
    assert "Alfords &amp; Davids" in html and "(1 new)" in html  # label is HTML-escaped
    assert "shared room hello" in html
    assert "private family business" not in html  # other room
    assert "ancient out-of-window news" not in html  # outside the window
    assert len(push_batch) == 1
    sub_id, uid, info, payload = push_batch[0]
    assert uid == noah["user"]["id"] and info["endpoint"] == "e-noah"
    assert "Alfords & Davids: 1" in payload["body"]
    assert payload["tag"].startswith("summary-weekly-")


def test_collect_skips_detached_family_and_quiet_channels():
    c = client()
    mia = _make_user(c, "mia", family="Alfords")
    noah = _make_user(c, "noah", email="noah@test.example", family="Davids")
    room_id = _create_room("Alfords", "Davids")
    _channel_row(noah["user"]["id"], room_id, nightly_email=True)
    # Noah's family leaves the room; nothing else has activity.
    db = SessionLocal()
    db.execute(DmRoomFamily.__table__.delete().where(DmRoomFamily.room_id == room_id))
    db.commit()
    db.close()

    db = SessionLocal()
    email_batch, push_batch = summary_mod.collect_summaries(db, "nightly")
    db.close()
    assert email_batch == [] and push_batch == []


def test_run_summary_delivers_email_and_push(monkeypatch):
    c = client()
    alice = _make_user(c, "alice", family="Alfords")
    bob = _make_user(c, "bob", email="bob@test.example", family="Alfords")
    _channel_row(bob["user"]["id"], None, nightly_email=True, nightly_push=True)
    db = SessionLocal()
    db.add(PushSubscription(endpoint="e-bob", p256dh="k", auth="a", user_id=bob["user"]["id"]))
    db.commit()
    db.close()
    _post_group(alice["user"]["id"], "summary content", timedelta(hours=2))

    # Patch only now: registration fires its own welcome email, which must
    # not pollute the assertions below.
    emails: list[tuple] = []
    pushes: list[tuple] = []
    monkeypatch.setattr(
        email_mod, "send_email",
        lambda subject, html, to_addr, text=None: emails.append((to_addr, subject, text)) or True,
    )
    monkeypatch.setattr(summary_mod, "_push_one", lambda info, uid, payload: pushes.append((uid, payload)) or "ok")

    db = SessionLocal()
    stats = summary_mod.run_summary(db, "nightly")
    db.close()
    assert stats == {"emails_sent": 1, "pushes_delivered": 1, "pushes_total": 1}
    # Registration fires its own welcome email in the background; match the
    # digest specifically rather than assuming sink order.
    digest = [e for e in emails if e[2] == summary_mod.TEXT_SUMMARY]
    assert len(digest) == 1
    assert digest[0][0] == "bob@test.example"
    assert pushes[0][1]["title"] == "Nightly summary"
    assert pushes[0][1]["body"] == "Group Chat: 1"


def test_digests_never_reach_non_opted_in_users(monkeypatch):
    """The strictly-opt-in guarantee: activity exists, but with no
    ChannelNotification rows nothing is collected at all."""
    monkeypatch.setattr(email_mod, "send_email", lambda *a, **k: True)
    c = client()
    alice = _make_user(c, "alice", email="alice@test.example", family="Alfords")
    room_id = _create_room("Alfords")
    _post_group(alice["user"]["id"], "hello", timedelta(hours=1))
    _post_room(room_id, alice["user"]["id"], "room hello", timedelta(hours=1))
    db = SessionLocal()
    email_batch, push_batch = summary_mod.collect_summaries(db, "nightly")
    db.close()
    assert email_batch == [] and push_batch == []


# ---------------------------------------------------------------------------
# Scheduler clock
# ---------------------------------------------------------------------------
def _dt(**kw):
    return datetime(**kw, tzinfo=timezone.utc)


def test_due_scopes_clock():
    os.environ["SUMMARY_ENABLED"] = "1"
    try:
        # Before the nightly slot: nothing.
        assert summary_mod.due_scopes(_dt(year=2026, month=11, day=3, hour=1, minute=30), {}) == []
        # After it: nightly (Tuesday is not the weekly weekday).
        assert summary_mod.due_scopes(_dt(year=2026, month=11, day=3, hour=2, minute=5), {}) == ["nightly"]
        # Already ran today: nothing.
        assert summary_mod.due_scopes(
            _dt(year=2026, month=11, day=3, hour=9, minute=0), {"nightly": "2026-11-03"}
        ) == []
        # Saturday after both slots: both.
        saturday = _dt(year=2026, month=11, day=7, hour=3, minute=0)
        assert summary_mod.due_scopes(saturday, {}) == ["nightly", "weekly"]
        # Saturday weekly already ran but nightly not: only nightly.
        assert summary_mod.due_scopes(
            saturday, {"weekly": "2026-11-07"}
        ) == ["nightly"]
        # Disabled kills everything.
        os.environ["SUMMARY_ENABLED"] = "0"
        assert summary_mod.due_scopes(saturday, {}) == []
    finally:
        os.environ["SUMMARY_ENABLED"] = "0"


def test_run_marker_persists_across_ticks():
    c = client()  # fresh schema
    db = SessionLocal()
    from app.summary import _load_last_runs, _mark_run

    assert _load_last_runs(db) == {"nightly": None, "weekly": None}
    _mark_run(db, "nightly", "2026-11-03")
    _mark_run(db, "weekly", "2026-11-07")
    assert _load_last_runs(db) == {"nightly": "2026-11-03", "weekly": "2026-11-07"}
    _mark_run(db, "nightly", "2026-11-04")  # update in place, no dup
    rows = db.query(summary_mod.DigestRun).all() if hasattr(summary_mod, "DigestRun") else []
    from app.models import DigestRun
    rows = db.query(DigestRun).all()
    assert len(rows) == 2
    db.close()
