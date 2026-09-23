"""Per-user Spotify connections and syncing from the recently-played endpoint."""
import asyncio
import base64
import json
import logging
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx

from . import config, db

log = logging.getLogger("liner-notes")

SCOPE = "user-read-recently-played"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
RECENT_URL = "https://api.spotify.com/v1/me/player/recently-played"
ME_URL = "https://api.spotify.com/v1/me"
REFRESH_TOKEN_DAYS = 180  # Spotify refresh tokens expire six months after the user signs in
SOURCE_API = 2
NOT_ALLOWLISTED = (
    "Spotify hasn't allowed this Spotify account to use the app yet. The admin needs to add its email under "
    "User Management in the Spotify developer dashboard; then connect again."
)


class SpotifyError(Exception):
    pass


def configured() -> bool:
    return bool(config.get("spotify_client_id") and config.get("spotify_client_secret"))


def connection(user_id: int):
    row = db.q1("SELECT * FROM spotify WHERE user_id=?", (user_id,))
    return dict(row) if row else None


def _update(user_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    db.run(f"UPDATE spotify SET {cols} WHERE user_id=?", (*fields.values(), user_id))


def authorize_url(user_id: int) -> str:
    state = secrets.token_urlsafe(24)
    db.run("INSERT INTO oauth_states(state, kind, user_id, created_at) VALUES(?,?,?,?)",
           (state, "spotify", user_id, time.time()))
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": config.get("spotify_client_id"), "scope": SCOPE,
        "redirect_uri": config.spotify_redirect(), "state": state,
    })


async def _token_request(data: dict, user_id: int | None = None) -> dict:
    cid, secret = config.get("spotify_client_id"), config.get("spotify_client_secret")
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_URL, data=data, headers={"Authorization": f"Basic {basic}"})
    if r.status_code in (400, 401) and "invalid_client" in r.text:
        raise SpotifyError("Spotify rejected the app's client ID or secret. An admin can fix them on the admin page.")
    if r.status_code == 400 and "invalid_grant" in r.text:
        if user_id is not None and data.get("grant_type") == "refresh_token":
            _update(user_id, needs_reconnect=1)
        raise SpotifyError("Spotify ended this connection. Select Reconnect Spotify to sign in again.")
    if r.status_code != 200:
        raise SpotifyError(f"Spotify's sign-in service returned status {r.status_code}.")
    return r.json()


async def finish_connect(code: str, state: str) -> int:
    st = db.pop_state(state, "spotify")
    if not st or not code:
        raise SpotifyError("This Spotify sign-in link expired. Go back and select Connect Spotify again.")
    user_id = st["user_id"]
    tok = await _token_request({"grant_type": "authorization_code", "code": code,
                                "redirect_uri": config.spotify_redirect()})
    async with httpx.AsyncClient(timeout=15) as client:
        me = await client.get(ME_URL, headers={"Authorization": f"Bearer {tok['access_token']}"})
    if me.status_code == 403:
        raise SpotifyError(NOT_ALLOWLISTED)
    name = None
    if me.status_code == 200:
        name = me.json().get("display_name") or me.json().get("id")
    now = time.time()
    db.run(
        "INSERT INTO spotify(user_id, access_token, refresh_token, expires_at, consented_at, display_name,"
        " needs_reconnect, last_error) VALUES(?,?,?,?,?,?,0,NULL) ON CONFLICT(user_id) DO UPDATE SET"
        " access_token=excluded.access_token, refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,"
        " consented_at=excluded.consented_at, display_name=excluded.display_name, needs_reconnect=0, last_error=NULL",
        (user_id, tok["access_token"], tok["refresh_token"], now + int(tok.get("expires_in", 3600)), now, name),
    )
    await sync_user(user_id)
    return user_id


def disconnect(user_id: int) -> None:
    db.run("DELETE FROM spotify WHERE user_id=?", (user_id,))


async def _access_token(user_id: int) -> str:
    conn = connection(user_id)
    if not conn or not conn["refresh_token"]:
        raise SpotifyError("Spotify isn't connected.")
    if (conn["expires_at"] or 0) - 60 > time.time():
        return conn["access_token"]
    new = await _token_request({"grant_type": "refresh_token", "refresh_token": conn["refresh_token"]}, user_id)
    _update(user_id, access_token=new["access_token"], expires_at=time.time() + int(new.get("expires_in", 3600)),
            refresh_token=new.get("refresh_token") or conn["refresh_token"])
    return new["access_token"]


def rows_from_recent(items):
    """Recently-played items to play rows. Play length is the track's full duration,
    since the endpoint doesn't say how much of it was heard."""
    rows = []
    for it in items or []:
        track = (it or {}).get("track") or {}
        played = (it or {}).get("played_at")
        if not track.get("name") or not played:
            continue
        try:
            t = round(datetime.fromisoformat(played.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            continue
        artists = track.get("artists") or []
        artist = (artists[0] or {}).get("name") if artists else None
        rows.append([t, int(track.get("duration_ms") or 0), "m", track["name"], artist or "Unknown artist",
                     (track.get("album") or {}).get("name") or "", "", "", 0, 0, 0, SOURCE_API])
    return rows


_locks: dict[int, asyncio.Lock] = {}


async def sync_user(user_id: int) -> dict:
    lock = _locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        conn = connection(user_id)
        if not conn:
            return {"ok": False, "error": "Spotify isn't connected."}
        if conn["needs_reconnect"]:
            return {"ok": False, "error": "Spotify needs to be reconnected."}
        try:
            token = await _access_token(user_id)
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(RECENT_URL, params={"limit": 50}, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401:
                _update(user_id, expires_at=0)
                raise SpotifyError("Spotify rejected the access token. It will be refreshed on the next sync.")
            if r.status_code == 403:
                raise SpotifyError(NOT_ALLOWLISTED)
            if r.status_code == 429:
                reason = ""
                try:
                    reason = (r.json().get("error") or {}).get("reason", "")
                except ValueError:
                    pass
                raise SpotifyError(
                    "Spotify's API quota for this app is used up; syncing resumes when it resets."
                    if reason == "QUOTA_EXCEEDED" else "Spotify asked to slow down; the next sync will retry.")
            if r.status_code != 200:
                raise SpotifyError(f"Spotify returned status {r.status_code} for recent plays.")
            items = r.json().get("items", [])
        except (SpotifyError, httpx.HTTPError) as e:
            msg = str(e) if isinstance(e, SpotifyError) else f"Couldn't reach Spotify ({e.__class__.__name__})."
            _update(user_id, last_error=msg)
            log.warning("sync failed for user %s: %s", user_id, msg)
            return {"ok": False, "error": msg}

        rows = rows_from_recent(items)
        newest_before = conn["api_newest"] or 0
        gaps = json.loads(conn["gaps"] or "[]")
        if rows and newest_before and len(items) >= 50:
            oldest_now = min(r[0] for r in rows)
            if oldest_now > newest_before:  # a full page of unseen plays: some may have scrolled off
                gaps = (gaps + [[newest_before, oldest_now]])[-20:]
        added = db.insert_plays(user_id, rows)
        newest = max([newest_before] + [r[0] for r in rows])
        _update(user_id, api_newest=newest, gaps=json.dumps(gaps), last_sync=time.time(), last_error=None)
        log.info("sync user %s: %d recent plays, %d new", user_id, len(rows), added)
        return {"ok": True, "added": added}


async def sync_all() -> None:
    rows = db.q("SELECT s.user_id FROM spotify s JOIN users u ON u.id=s.user_id "
                "WHERE u.disabled=0 AND s.needs_reconnect=0 AND s.refresh_token IS NOT NULL")
    for row in rows:
        await sync_user(row["user_id"])


def status(user_id: int) -> dict:
    conn = connection(user_id) or {}
    first = db.q1("SELECT MIN(t) t, COUNT(*) n FROM plays WHERE user_id=? AND x=2", (user_id,))
    consented = conn.get("consented_at")
    return {
        "configured": configured(),
        "connected": bool(conn.get("refresh_token")),
        "needs_reconnect": bool(conn.get("needs_reconnect")),
        "name": conn.get("display_name"),
        "reconnect_by": consented + REFRESH_TOKEN_DAYS * 86400 if consented else None,
        "last_sync": conn.get("last_sync"),
        "last_error": conn.get("last_error"),
        "gaps": json.loads(conn.get("gaps") or "[]"),
        "poll_minutes": config.get("poll_minutes"),
        "synced": first["n"],
        "first_synced_play": first["t"],
    }
