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
