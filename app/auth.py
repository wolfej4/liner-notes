"""Accounts, passwords, sessions, and one-time links (invites and password resets)."""
import base64
import hashlib
import hmac
import re
import secrets
import time

from fastapi import HTTPException, Request

from . import config, db

SESSION_COOKIE = "ln_session"
SESSION_DAYS = 30
MIN_PASSWORD = 10


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        hashlib.scrypt(b"x", salt=b"0" * 16, n=2**14, r=8, p=1, dklen=32)  # keep timing similar
        return False
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_password_rules(password) -> str:
    password = str(password or "")
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Use at least {MIN_PASSWORD} characters for the password.")
    return password


def clean_email(email) -> str:
    email = str(email or "").strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Enter a valid email address.")
    return email


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------- sessions

def create_session(user_id: int) -> str:
    token, now = secrets.token_urlsafe(32), time.time()
    db.run("INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES(?,?,?,?)",
           (sha(token), user_id, now, now + SESSION_DAYS * 86400))
    db.run("UPDATE users SET last_login=? WHERE id=?", (now, user_id))
    db.run("DELETE FROM sessions WHERE expires_at<?", (now,))
    return token


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=config.secure_cookies(), path="/")


def end_session(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.run("DELETE FROM sessions WHERE token_hash=?", (sha(token),))


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = db.q1(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0",
        (sha(token), time.time()),
    )
    return dict(row) if row else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in first.")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Only admins can do that.")
    return user


# ---------------------------------------------------------------- sign-in throttling

_failures: dict[str, list[float]] = {}


def check_rate(key: str) -> None:
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < 900]
    _failures[key] = recent
    if len(recent) >= 5:
        raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.")


def note_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.time())


# ---------------------------------------------------------------- users

def user_count() -> int:
    return db.q1("SELECT COUNT(*) FROM users")[0]


def create_user(email: str, name: str, password: str | None = None, is_admin: bool = False,
                oidc_sub: str | None = None) -> int:
    from .newsletter import current_keys  # recaps start with the next full period

    if db.q1("SELECT 1 FROM users WHERE email=?", (email,)):
        raise HTTPException(409, "There's already an account with that email.")
    week_key, month_key = current_keys()
    uid, _ = db.run(
        "INSERT INTO users(email, name, password_hash, oidc_sub, is_admin, created_at, unsub_token,"
        " last_weekly, last_monthly) VALUES(?,?,?,?,?,?,?,?,?)",
        (email, (name or "").strip()[:80] or email.split("@")[0], hash_password(password) if password else None,
         oidc_sub, int(is_admin), time.time(), secrets.token_urlsafe(24), week_key, month_key),
    )
    return uid


def get_user(user_id: int):
    row = db.q1("SELECT * FROM users WHERE id=?", (user_id,))
    return dict(row) if row else None


# ---------------------------------------------------------------- one-time links

def create_token(kind: str, email: str | None = None, user_id: int | None = None, is_admin: bool = False,
                 days: float = 7) -> str:
    raw, now = secrets.token_urlsafe(24), time.time()
    if kind == "invite" and email:
        db.run("UPDATE tokens SET used_at=? WHERE kind='invite' AND email=? COLLATE NOCASE AND used_at IS NULL",
               (now, email))
    db.run("INSERT INTO tokens(token_hash, kind, email, user_id, is_admin, created_at, expires_at)"
           " VALUES(?,?,?,?,?,?,?)", (sha(raw), kind, email, user_id, int(is_admin), now, now + days * 86400))
    return raw


def get_token(raw: str, kind: str):
    row = db.q1("SELECT * FROM tokens WHERE token_hash=? AND kind=? AND used_at IS NULL AND expires_at>?",
                (sha(raw or ""), kind, time.time()))
    return dict(row) if row else None


def use_token(token_hash: str) -> None:
    db.run("UPDATE tokens SET used_at=? WHERE token_hash=?", (time.time(), token_hash))


def invite_for_email(email: str):
    row = db.q1("SELECT * FROM tokens WHERE kind='invite' AND email=? COLLATE NOCASE AND used_at IS NULL"
                " AND expires_at>? ORDER BY created_at DESC", (email, time.time()))
    return dict(row) if row else None
