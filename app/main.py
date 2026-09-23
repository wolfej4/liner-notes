"""Liner Notes server.

Serves the dashboard, stores listening history in SQLite, and keeps it current by
polling Spotify's recently-played endpoint (the only listening-history endpoint the
Web API offers; it returns your last 50 plays).
"""
import asyncio
import base64
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8089").strip().rstrip("/")
POLL_MINUTES = max(2, int(os.environ.get("POLL_MINUTES", "10")))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
GOOGLE_FONTS = os.environ.get("GOOGLE_FONTS", "true").strip().lower() not in ("false", "0", "no")

REDIRECT_URI = f"{PUBLIC_URL}/auth/callback"
SCOPE = "user-read-recently-played"
REFRESH_TOKEN_DAYS = 180  # Spotify refresh tokens expire six months after the user signs in
RECENT_URL = "https://api.spotify.com/v1/me/player/recently-played"
TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"

HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "static" / "index.html"
VENDOR = HERE.parent / "vendor"
CDN = {
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js": "chart.umd.js",
    "https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js": "jszip.min.js",
}

# One row per play. x = source: 0 basic export, 1 extended export, 2 synced from the API.
COLS = ("t", "ms", "k", "tr", "ar", "al", "pf", "cc", "sk", "sh", "off", "x")
SOURCE_API = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("liner-notes")


# ---------------------------------------------------------------- storage

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DATA_DIR / "liner-notes.db", timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not os.access(DATA_DIR, os.W_OK | os.X_OK):
        raise SystemExit(
            f"Liner Notes can't write to {DATA_DIR} (running as uid {os.getuid()}, gid {os.getgid()}). "
            f"If /data is a host folder, run on the Docker host: chown -R {os.getuid()}:{os.getgid()} <that folder>"
        )
    with closing(connect()) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS plays(
              t INTEGER NOT NULL, ms INTEGER NOT NULL, k TEXT NOT NULL,
              tr TEXT NOT NULL, ar TEXT NOT NULL, al TEXT NOT NULL DEFAULT '',
              pf TEXT NOT NULL DEFAULT '', cc TEXT NOT NULL DEFAULT '',
              sk INTEGER NOT NULL DEFAULT 0, sh INTEGER NOT NULL DEFAULT 0, off INTEGER NOT NULL DEFAULT 0,
              x INTEGER NOT NULL,
              UNIQUE(t, ms, tr)
            );
            CREATE INDEX IF NOT EXISTS plays_t ON plays(t);
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
            """
        )
        con.commit()


def kv_get(key, default=None):
    with closing(connect()) as con:
        row = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return json.loads(row["v"]) if row else default


def kv_set(key, value) -> None:
    with closing(connect()) as con, con:
        con.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value)),
        )


def kv_del(*keys) -> None:
    with closing(connect()) as con, con:
        con.executemany("DELETE FROM kv WHERE k=?", [(k,) for k in keys])


def bump_version() -> None:
    kv_set("version", kv_get("version", 0) + 1)


def clean_row(row):
    if not isinstance(row, (list, tuple)) or len(row) != len(COLS):
        return None
    try:
        t, ms = int(row[0]), max(0, int(row[1] or 0))
        k = "p" if row[2] == "p" else "m"
        tr, ar, al, pf, cc = (str(v or "")[:500] for v in row[3:8])
        sk, sh, off = (1 if v else 0 for v in row[8:11])
        x = int(row[11])
    except (TypeError, ValueError):
        return None
    if x not in (0, 1, 2) or (k == "m" and not tr):
        return None
    return (t, ms, k, tr, ar or "Unknown artist", al, pf, cc, sk, sh, off, x)


def insert_rows(rows) -> int:
    clean = [c for c in (clean_row(r) for r in rows) if c]
    if not clean:
        return 0
    with closing(connect()) as con, con:
        before = con.total_changes
        con.executemany(
            f"INSERT OR IGNORE INTO plays({','.join(COLS)}) VALUES({','.join('?' * len(COLS))})", clean
        )
        added = con.total_changes - before
    if added:
        bump_version()
    return added


# ---------------------------------------------------------------- spotify

class SpotifyError(Exception):
    pass


def rows_from_recent(items):
    """Convert recently-played items to play rows. Play length is the track's full
    duration, since the endpoint doesn't say how much of it was heard."""
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
        rows.append([
            t, int(track.get("duration_ms") or 0), "m", track["name"], artist or "Unknown artist",
            (track.get("album") or {}).get("name") or "", "", "", 0, 0, 0, SOURCE_API,
        ])
    return rows


async def token_request(data: dict) -> dict:
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_URL, data=data, headers={"Authorization": f"Basic {basic}"})
    if r.status_code == 400 and "invalid_grant" in r.text:
        if data.get("grant_type") == "refresh_token":
            kv_set("needs_reconnect", True)
        raise SpotifyError("Spotify ended this connection. Select Reconnect Spotify to sign in again.")
    if r.status_code != 200:
        raise SpotifyError(f"Spotify's sign-in service returned status {r.status_code}.")
    return r.json()


async def access_token() -> str:
    tok = kv_get("token")
    if not tok:
        raise SpotifyError("Spotify isn't connected.")
    if tok["expires_at"] - 60 > time.time():
        return tok["access_token"]
    new = await token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    tok["access_token"] = new["access_token"]
    tok["expires_at"] = time.time() + int(new.get("expires_in", 3600))
    if new.get("refresh_token"):
        tok["refresh_token"] = new["refresh_token"]
    kv_set("token", tok)
    return tok["access_token"]


_sync_lock = asyncio.Lock()


async def sync() -> dict:
    async with _sync_lock:
        if not kv_get("token"):
            return {"ok": False, "error": "Spotify isn't connected."}
        if kv_get("needs_reconnect"):
            return {"ok": False, "error": "Spotify needs to be reconnected."}
        try:
            token = await access_token()
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(RECENT_URL, params={"limit": 50}, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401:
                tok = kv_get("token")
                tok["expires_at"] = 0
                kv_set("token", tok)
                raise SpotifyError("Spotify rejected the access token. It will be refreshed on the next sync.")
            if r.status_code == 429:
                reason = ""
                try:
                    reason = (r.json().get("error") or {}).get("reason", "")
                except ValueError:
                    pass
                raise SpotifyError(
                    "Spotify's API quota for this app is used up; syncing resumes when it resets."
                    if reason == "QUOTA_EXCEEDED"
                    else "Spotify asked to slow down; the next sync will retry."
                )
            if r.status_code != 200:
                raise SpotifyError(f"Spotify returned status {r.status_code} for recent plays.")
            items = r.json().get("items", [])
        except (SpotifyError, httpx.HTTPError) as e:
            msg = str(e) if isinstance(e, SpotifyError) else f"Couldn't reach Spotify ({e.__class__.__name__})."
            kv_set("last_error", msg)
            log.warning("sync failed: %s", msg)
            return {"ok": False, "error": msg}

        rows = rows_from_recent(items)
        newest_before = kv_get("api_newest", 0)
        if rows and newest_before and len(items) >= 50:
            oldest_now = min(r[0] for r in rows)
            if oldest_now > newest_before:
                # A full page with nothing we've seen: plays in between may have scrolled off.
                gaps = kv_get("gaps", [])
                gaps.append([newest_before, oldest_now])
                kv_set("gaps", gaps[-20:])
        added = insert_rows(rows)
        if rows:
            kv_set("api_newest", max(newest_before, max(r[0] for r in rows)))
        kv_set("last_sync", time.time())
        kv_del("last_error")
        log.info("sync: %d recent plays, %d new", len(rows), added)
        return {"ok": True, "added": added}


async def poll_forever() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await sync()
        except Exception:  # keep the loop alive no matter what
            log.exception("unexpected sync failure")
        await asyncio.sleep(POLL_MINUTES * 60)


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if not (CLIENT_ID and CLIENT_SECRET):
        log.warning("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set; Spotify sync is off")
    else:
        log.info("Spotify redirect URI: %s (register this exact value in the Spotify dashboard)", REDIRECT_URI)
    task = asyncio.create_task(poll_forever())
    yield
    task.cancel()


app = FastAPI(title="Liner Notes", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)
if VENDOR.is_dir():
    app.mount("/vendor", StaticFiles(directory=VENDOR), name="vendor")


@app.middleware("http")
async def require_app_header(request: Request, call_next):
    # A custom header can't be sent cross-site without a CORS preflight, which this app never allows.
    if request.method in ("POST", "PUT", "DELETE") and request.url.path.startswith("/api/"):
        if request.headers.get("x-liner-notes") != "1":
            return JSONResponse({"detail": "Missing X-Liner-Notes header."}, status_code=403)
    return await call_next(request)


def render_index() -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Turns on server mode in the dashboard; API paths are relative to this page.
    html = html.replace('<meta name="liner-notes-api" content="">', '<meta name="liner-notes-api" content="./">', 1)
    for url, name in CDN.items():
        if (VENDOR / name).is_file():
            html = html.replace(url, f"vendor/{name}")
    if not GOOGLE_FONTS:
        html = "\n".join(
            line for line in html.splitlines() if "fonts.googleapis.com" not in line and "fonts.gstatic.com" not in line
        )
    return html


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(render_index(), headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/auth/login")
def login():
    if not (CLIENT_ID and CLIENT_SECRET):
        raise HTTPException(500, "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in the container's environment first.")
    state = secrets.token_urlsafe(24)
    kv_set("oauth_state", {"s": state, "at": time.time()})
    query = urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "scope": SCOPE,
        "redirect_uri": REDIRECT_URI, "state": state,
    })
    return RedirectResponse(f"{AUTHORIZE_URL}?{query}")


@app.get("/auth/callback")
async def callback(code: str | None = None, state: str | None = None, error: str | None = None):
    saved = kv_get("oauth_state")
    kv_del("oauth_state")
    if error:
        return RedirectResponse(f"{PUBLIC_URL}/?spotify=denied")
    if not (saved and state and code) or not secrets.compare_digest(saved["s"], state) or time.time() - saved["at"] > 600:
        raise HTTPException(400, "This sign-in link expired. Go back to Liner Notes and select Connect Spotify again.")
    try:
        tok = await token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})
    except SpotifyError as e:
        raise HTTPException(502, str(e))
    now = time.time()
    kv_set("token", {
        "access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
        "expires_at": now + int(tok.get("expires_in", 3600)), "consented_at": now,
    })
    kv_del("needs_reconnect", "last_error")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            me = await client.get("https://api.spotify.com/v1/me", headers={"Authorization": f"Bearer {tok['access_token']}"})
        if me.status_code == 200:
            kv_set("profile", {"name": me.json().get("display_name") or me.json().get("id")})
    except httpx.HTTPError:
        pass
    await sync()
    return RedirectResponse(f"{PUBLIC_URL}/?spotify=connected")


@app.get("/api/status")
def status():
    tok = kv_get("token")
    with closing(connect()) as con:
        total, synced, first_api = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(x=2), 0), MIN(CASE WHEN x=2 THEN t END) FROM plays"
        ).fetchone()
    consented = tok.get("consented_at") if tok else None
    return {
        "configured": bool(CLIENT_ID and CLIENT_SECRET),
        "connected": bool(tok),
        "needs_reconnect": bool(kv_get("needs_reconnect")),
        "name": (kv_get("profile") or {}).get("name"),
        "reconnect_by": consented + REFRESH_TOKEN_DAYS * 86400 if consented else None,
        "last_sync": kv_get("last_sync"),
        "last_error": kv_get("last_error"),
        "gaps": kv_get("gaps", []),
        "poll_minutes": POLL_MINUTES,
        "total": total,
        "synced": synced,
        "first_synced_play": first_api,
        "version": kv_get("version", 0),
    }


@app.post("/api/sync")
async def sync_now():
    return await sync()


@app.post("/api/disconnect")
def disconnect():
    kv_del("token", "profile", "needs_reconnect", "last_error")
    return {"ok": True}


@app.get("/api/records")
def get_records():
    with closing(connect()) as con:
        rows = con.execute(f"SELECT {','.join(COLS)} FROM plays ORDER BY t").fetchall()
    body = json.dumps([tuple(r) for r in rows], separators=(",", ":"), ensure_ascii=False)
    return Response(body, media_type="application/json", headers={"Cache-Control": "no-store"})


@app.post("/api/records")
async def add_records(request: Request):
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "Send JSON like {\"rows\": [[...], ...]}.")
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise HTTPException(400, "Send JSON like {\"rows\": [[...], ...]}.")
    return {"added": insert_rows(rows)}


@app.delete("/api/records")
def delete_records():
    with closing(connect()) as con, con:
        con.execute("DELETE FROM plays")
    kv_del("api_newest", "gaps")
    bump_version()
    return {"ok": True}
