"""Environment-driven configuration.

Centralizes the env vars so they're read in one place. Values are optional;
callers get sensible defaults (or None when a feature is disabled).
"""
import os


def vapid_private_key() -> str | None:
    """Base64url VAPID private key (raw P-256 scalar). None disables push."""
    key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    return key or None


def vapid_public_key() -> str | None:
    """Base64url VAPID public key (DER SubjectPublicKeyInfo). Exposed to the
    browser for pushManager.subscribe(); not sensitive."""
    key = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
    return key or None


def vapid_email() -> str:
    """The VAPID 'sub' claim. Must look like a contact (email or url)."""
    return os.environ.get("VAPID_EMAIL", "").strip() or "mailto:admin@example.com"


def push_enabled() -> bool:
    """Push notifications require a private key + a public key. Without them
    the subscribe endpoint returns a 503 and sends are skipped."""
    return bool(vapid_private_key() and vapid_public_key())


# Base URL used as the VAPID audience / deep-link origin. In production this
# is set to https://your-domain; locally it's http://localhost:PORT.
def app_origin() -> str:
    return os.environ.get("APP_ORIGIN", "").strip().rstrip("/") or "http://localhost:8983"


# ---------------------------------------------------------------------------
# Transactional email (SMTP). Sent via a provider (e.g. Resend, Mailgun) or
# a local dev sink like Mailpit. All fields are optional; email is a no-op
# (feature disabled) until SMTP_HOST + SMTP_FROM_ADDRESS are set.
#
# Local dev: docker-compose runs a Mailpit service and points the app at it
# (SMTP_HOST=mailpit, SMTP_PORT=1025). See .env.local / docker-compose.yml.
# ---------------------------------------------------------------------------
def smtp_host() -> str | None:
    """SMTP server host. None disables email entirely."""
    return os.environ.get("SMTP_HOST", "").strip() or None


def smtp_port() -> int:
    return int(os.environ.get("SMTP_PORT", "587"))


def smtp_username() -> str | None:
    return os.environ.get("SMTP_USERNAME", "").strip() or None


def smtp_password() -> str | None:
    return os.environ.get("SMTP_PASSWORD", "").strip() or None


def smtp_from_address() -> str | None:
    """Sender (From) email address. Required to actually send."""
    return os.environ.get("SMTP_FROM_ADDRESS", "").strip() or None


def smtp_from_name() -> str:
    return os.environ.get("SMTP_FROM_NAME", "Community Chat").strip() or "Community Chat"


def smtp_timeout() -> int:
    return int(os.environ.get("SMTP_TIMEOUT", "10"))


def email_enabled() -> bool:
    """Email requires a host + a From address. Without both, sends are skipped
    and the settings endpoint reports the feature unavailable."""
    return bool(smtp_host() and smtp_from_address())


def _hhmm(name: str, default: str) -> tuple[int, int]:
    raw = os.environ.get(name, default).strip() or default
    try:
        hh, mm = raw.split(":", 1)
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except ValueError:
        pass
    hh, mm = default.split(":", 1)
    return int(hh), int(mm)


def community_name() -> str:
    """The community this deployment runs for (e.g. "Whimsy and Wonder").
    Displayed in the app shell, brand chrome, and emails; unbranded
    deployments fall back to the product name."""
    return os.environ.get("COMMUNITY_NAME", "Community Chat").strip() or "Community Chat"


def community_brand() -> str:
    """The brand readers see in email copy — "{name} Chat" for a named
    community, plain "Community Chat" when COMMUNITY_NAME is unset (never
    the doubled "Community Chat Chat")."""
    name = community_name()
    return name if name == "Community Chat" else f"{name} Chat"


def summary_enabled() -> bool:
    """Channel Summary scheduler on/off (SUMMARY_ENABLED=0 disables)."""
    return os.environ.get("SUMMARY_ENABLED", "1").strip() != "0"


def summary_nightly_time() -> tuple[int, int]:
    """UTC HH:MM the nightly summary runs (default 02:00 UTC ≈ evening US)."""
    return _hhmm("SUMMARY_NIGHTLY_UTC", "02:00")


def summary_weekly_time() -> tuple[int, int]:
    """UTC HH:MM the weekly summary runs on its weekday (default 02:30)."""
    return _hhmm("SUMMARY_WEEKLY_UTC", "02:30")


def summary_weekly_weekday() -> int:
    """Python weekday() the weekly summary runs on (Mon=0 … Sat=5, Sun=6)."""
    try:
        d = int(os.environ.get("SUMMARY_WEEKLY_WEEKDAY", "5"))
        return d if 0 <= d <= 6 else 5
    except ValueError:
        return 5
