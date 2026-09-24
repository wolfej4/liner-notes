"""Settings: saved on the admin page, with the container's environment as defaults."""
import json
import os
import re
from contextlib import closing
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db

# key: (environment variable, default, type)
FIELDS = {
    "public_url": ("PUBLIC_URL", "http://127.0.0.1:8089", str),
    "google_fonts": ("GOOGLE_FONTS", True, bool),
    "timezone": ("TZ", "America/Chicago", str),
    "signup_mode": ("SIGNUP_MODE", "invite", str),
    "spotify_client_id": ("SPOTIFY_CLIENT_ID", "", str),
    "spotify_client_secret": ("SPOTIFY_CLIENT_SECRET", "", str),
    "poll_minutes": ("POLL_MINUTES", 10, int),
    "oidc_enabled": ("OIDC_ENABLED", False, bool),
    "oidc_label": ("OIDC_LABEL", "Pocket ID", str),
    "oidc_issuer": ("OIDC_ISSUER", "", str),
    "oidc_client_id": ("OIDC_CLIENT_ID", "", str),
    "oidc_client_secret": ("OIDC_CLIENT_SECRET", "", str),
    "smtp_host": ("SMTP_HOST", "", str),
    "smtp_port": ("SMTP_PORT", 587, int),
    "smtp_security": ("SMTP_SECURITY", "starttls", str),
    "smtp_user": ("SMTP_USER", "", str),
    "smtp_password": ("SMTP_PASSWORD", "", str),
    "smtp_from": ("SMTP_FROM", "", str),
    "newsletter_weekday": ("NEWSLETTER_WEEKDAY", 0, int),
    "newsletter_hour": ("NEWSLETTER_HOUR", 8, int),
}
SECRETS = {"spotify_client_secret", "oidc_client_secret", "smtp_password"}
LABELS = {
    "public_url": "Public address", "timezone": "Time zone", "signup_mode": "Who can sign up",
    "poll_minutes": "Check Spotify every", "oidc_issuer": "Issuer URL", "smtp_port": "Port",
    "smtp_security": "Security", "smtp_from": "From address", "newsletter_weekday": "Weekly send day",
    "newsletter_hour": "Send hour",
}

_cache = None


def _coerce(kind, value):
    if kind is bool:
        return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        return int(str(value).strip())
    return str(value).strip()


def _saved():
    global _cache
    if _cache is None:
        _cache = {r["k"]: json.loads(r["v"]) for r in db.q("SELECT k, v FROM settings")}
    return _cache


def from_env(key):
    env, default, kind = FIELDS[key]
    raw = os.environ.get(env)
    if raw is None or raw.strip() == "":
        return default
    try:
        return _coerce(kind, raw)
    except ValueError:
        return default


def get(key):
    saved = _saved()
    value = saved[key] if key in saved else from_env(key)
    if key == "public_url":
        value = str(value).rstrip("/")
    return value


def _validate(key, v):
    if key == "public_url" and not re.match(r"^https?://[^/\s]+", v):
        return "Use a full address, like https://music.wolfe.house."
    if key == "poll_minutes" and not 2 <= v <= 60:
        return "Choose between 2 and 60 minutes."
    if key == "signup_mode" and v not in ("invite", "open", "closed"):
        return "Choose one of the options."
    if key == "smtp_security" and v not in ("starttls", "ssl", "none"):
        return "Choose one of the options."
    if key == "smtp_port" and not 1 <= v <= 65535:
        return "Enter a port between 1 and 65535."
    if key == "newsletter_weekday" and not 0 <= v <= 6:
        return "Choose a day."
    if key == "newsletter_hour" and not 0 <= v <= 23:
        return "Choose an hour between 0 and 23."
    if key == "timezone":
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            return "Use a time zone name like America/Chicago."
    if key == "oidc_issuer" and v and not re.match(r"^https?://", v):
        return "Use the full issuer address, like https://id.wolfe.house."
    if key == "smtp_from" and v and "@" not in v:
        return "Enter an email address, optionally with a name: Liner Notes <music@wolfe.house>."
    return None


def save(values: dict) -> dict:
    """Validate and store settings. Blank secrets keep their current value. Returns {key: error}."""
    errors, clean = {}, {}
    for key, raw in values.items():
        if key not in FIELDS:
            continue
        if key in SECRETS and (raw is None or str(raw) == ""):
            continue
        try:
            value = _coerce(FIELDS[key][2], raw)
        except (TypeError, ValueError):
            errors[key] = "Enter a number."
            continue
        err = _validate(key, value)
        if err:
            errors[key] = err
        else:
            clean[key] = value
    if errors:
        return errors
    with closing(db.connect()) as con, con:
        con.executemany(
            "INSERT INTO settings(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            [(k, json.dumps(v)) for k, v in clean.items()],
        )
    global _cache
    _cache = None
    return {}


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(get("timezone"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def spotify_redirect() -> str:
    return get("public_url") + "/auth/callback"


def oidc_redirect() -> str:
    return get("public_url") + "/auth/oidc/callback"


def public_host() -> str:
    from urllib.parse import urlsplit
    return urlsplit(get("public_url")).netloc.lower()
