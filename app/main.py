"""Liner Notes server: accounts, the dashboard, Spotify sync, admin settings, and recap emails."""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth, config, db, mailer, newsletter, oidc, spotify
from .views import env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("liner-notes")

HERE = Path(__file__).resolve().parent
DASHBOARD_HTML = HERE / "dashboard.html"
VENDOR = HERE.parent / "vendor"
CDN = {
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js": "chart.umd.js",
    "https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js": "jszip.min.js",
}
PAGE_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
                "X-Content-Type-Options": "nosniff"}


async def poll_forever() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            if spotify.configured():
                await spotify.sync_all()
        except Exception:
            log.exception("unexpected sync failure")
        await asyncio.sleep(config.get("poll_minutes") * 60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    if auth.user_count() == 0:
        log.info("No accounts yet. Open %s/setup to create the admin account.", config.get("public_url"))
    tasks = [asyncio.create_task(poll_forever()), asyncio.create_task(newsletter.loop())]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Liner Notes", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
if VENDOR.is_dir():
    app.mount("/vendor", StaticFiles(directory=VENDOR), name="vendor")


@app.middleware("http")
async def require_app_header(request: Request, call_next):
    # A custom header can't be sent cross-site without a CORS preflight, which this app never allows.
    if request.method in ("POST", "PUT", "DELETE") and request.url.path.startswith("/api/"):
        if request.headers.get("x-liner-notes") != "1":
            return JSONResponse({"detail": "Missing X-Liner-Notes header."}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------- helpers

def page(name: str, request: Request, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("user", auth.current_user(request))
    ctx.setdefault("title", "Liner Notes")
    ctx["fonts"] = config.get("google_fonts")
    return HTMLResponse(env.get_template(name).render(**ctx), status_code=status_code, headers=PAGE_HEADERS)


def message(request: Request, heading: str, text: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return page("message.html", request, status_code=status_code, title=heading, heading=heading, message=text, **ctx)


def signed_in(user_id: int, redirect: str = "/", as_json: bool = True):
    token = auth.create_session(user_id)
    resp = JSONResponse({"redirect": redirect}) if as_json else RedirectResponse(redirect, status_code=303)
    auth.set_session_cookie(resp, token)
    return resp


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "Send JSON.")
    if not isinstance(data, dict):
        raise HTTPException(400, "Send a JSON object.")
    return data


def oidc_ctx() -> dict:
    return {"oidc": oidc.enabled(), "oidc_label": config.get("oidc_label") or "single sign-on"}


def link(path: str) -> str:
    return config.get("public_url") + path


# ---------------------------------------------------------------- dashboard

def render_dashboard() -> str:
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    html = html.replace('<meta name="liner-notes-api" content="">', '<meta name="liner-notes-api" content="./">', 1)
    for url, name in CDN.items():
        if (VENDOR / name).is_file():
            html = html.replace(url, f"vendor/{name}")
    if not config.get("google_fonts"):
        html = "\n".join(l for l in html.splitlines() if "fonts.googleapis.com" not in l and "fonts.gstatic.com" not in l)
    return html


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if auth.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    if not auth.current_user(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(render_dashboard(), headers=PAGE_HEADERS)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- setup, sign-in, sign-up

@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if auth.user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    legacy = db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plays_legacy'") is not None
    return page("setup.html", request, title="Set up", legacy=legacy, min_password=auth.MIN_PASSWORD)


@app.post("/api/setup")
async def setup(request: Request):
    if auth.user_count() > 0:
        raise HTTPException(403, "Liner Notes is already set up. Sign in instead.")
    data = await body(request)
    email = auth.clean_email(data.get("email"))
    password = auth.check_password_rules(data.get("password"))
    uid = auth.create_user(email, data.get("name", ""), password, is_admin=True)
    db.adopt_legacy(uid)
    log.info("admin account created for %s", email)
    return signed_in(uid, "/admin")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    if auth.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    if auth.current_user(request):
        return RedirectResponse("/", status_code=303)
    return page("login.html", request, title="Sign in", error=error,
                signup_open=config.get("signup_mode") == "open", **oidc_ctx())


@app.post("/api/login")
async def login(request: Request):
    data = await body(request)
    email = str(data.get("email") or "").strip()
    ip = request.client.host if request.client else "?"
    auth.check_rate(f"ip:{ip}")
    auth.check_rate(f"email:{email.lower()}")
    row = db.q1("SELECT * FROM users WHERE email=?", (email,))
    if not row or not auth.verify_password(str(data.get("password") or ""), row["password_hash"]):
        auth.note_failure(f"ip:{ip}")
        auth.note_failure(f"email:{email.lower()}")
        raise HTTPException(400, "That email and password don't match.")
    if row["disabled"]:
        raise HTTPException(403, "This account is disabled. Ask the admin to turn it back on.")
    return signed_in(row["id"])


@app.post("/api/logout")
def logout(request: Request):
    auth.end_session(request)
    resp = JSONResponse({"redirect": "/login"})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if config.get("signup_mode") != "open" or auth.user_count() == 0:
        return RedirectResponse("/login", status_code=303)
    return page("join.html", request, title="Create an account", mode="signup", api="/api/signup",
                min_password=auth.MIN_PASSWORD, **oidc_ctx())


@app.post("/api/signup")
async def signup(request: Request):
    if config.get("signup_mode") != "open":
        raise HTTPException(403, "Sign-up is closed. Ask the admin for an invite.")
    data = await body(request)
    uid = auth.create_user(auth.clean_email(data.get("email")), data.get("name", ""),
                           auth.check_password_rules(data.get("password")))
    return signed_in(uid)


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str):
    inv = auth.get_token(token, "invite")
    if not inv:
        return message(request, "This invite doesn't work", "It may have expired or already been used. "
                       "Ask the person who invited you for a new link.", status_code=404)
    if config.get("signup_mode") == "closed":
        return message(request, "Sign-up is closed", "The admin has turned off new accounts for now.")
    return page("join.html", request, title="You're invited", mode="invite", email=inv["email"],
                api=f"/api/invite/{token}", min_password=auth.MIN_PASSWORD, **oidc_ctx())


@app.post("/api/invite/{token}")
async def accept_invite(request: Request, token: str):
    inv = auth.get_token(token, "invite")
    if not inv:
        raise HTTPException(404, "This invite has expired or was already used.")
    if config.get("signup_mode") == "closed":
        raise HTTPException(403, "Sign-up is closed right now.")
    data = await body(request)
    uid = auth.create_user(inv["email"], data.get("name", ""), auth.check_password_rules(data.get("password")),
                           is_admin=bool(inv["is_admin"]))
    auth.use_token(inv["token_hash"])
    return signed_in(uid)


@app.get("/reset/{token}", response_class=HTMLResponse)
def reset_page(request: Request, token: str):
    tok = auth.get_token(token, "reset")
    user = auth.get_user(tok["user_id"]) if tok else None
    if not user:
        return message(request, "This link doesn't work", "It may have expired or already been used. "
                       "Ask the admin for a new one.", status_code=404)
    return page("join.html", request, title="New password", mode="reset", email=user["email"],
                api=f"/api/reset/{token}", min_password=auth.MIN_PASSWORD)


@app.post("/api/reset/{token}")
async def reset(request: Request, token: str):
    tok = auth.get_token(token, "reset")
    if not tok:
        raise HTTPException(404, "This link has expired or was already used.")
    data = await body(request)
    password = auth.check_password_rules(data.get("password"))
    db.run("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(password), tok["user_id"]))
    db.run("DELETE FROM sessions WHERE user_id=?", (tok["user_id"],))
    auth.use_token(tok["token_hash"])
    return signed_in(tok["user_id"])


# ---------------------------------------------------------------- single sign-on

@app.get("/auth/oidc/login")
async def oidc_login(request: Request):
    if not oidc.enabled():
        return RedirectResponse("/login", status_code=303)
    try:
        return RedirectResponse(await oidc.login_url(), status_code=303)
    except oidc.OIDCError as e:
        return message(request, "Single sign-on isn't working", str(e), status_code=502,
                       action_url="/login", action_label="Back to sign-in")


@app.get("/auth/oidc/callback")
async def oidc_callback(request: Request, code: str | None = None, state: str | None = None,
                        error: str | None = None):
    back = {"action_url": "/login", "action_label": "Back to sign-in"}
    if error:
        return message(request, "Sign-in was cancelled", "Nothing changed.", **back)
    try:
        info = await oidc.finish(code, state)
    except oidc.OIDCError as e:
        return message(request, "Couldn't sign you in", str(e), status_code=400, **back)
    if auth.user_count() == 0:
        return message(request, "Set up Liner Notes first", "Create the admin account with a password, "
                       "then single sign-on will work.", action_url="/setup", action_label="Set up")
    row = db.q1("SELECT * FROM users WHERE oidc_sub=?", (info["sub"],))
    if not row and info["email"] and info["email_verified"]:
        row = db.q1("SELECT * FROM users WHERE email=?", (info["email"],))
        if row:
            if row["oidc_sub"]:
                return message(request, "Couldn't sign you in", "That email is already linked to a different "
                               "single sign-on account.", status_code=409, **back)
            db.run("UPDATE users SET oidc_sub=? WHERE id=?", (info["sub"], row["id"]))
    if not row:
        mode = config.get("signup_mode")
        inv = auth.invite_for_email(info["email"]) if info["email"] and info["email_verified"] else None
        if mode != "closed" and (inv or (mode == "open" and info["email"])):
            uid = auth.create_user(info["email"], info["name"], is_admin=bool(inv and inv["is_admin"]),
                                   oidc_sub=info["sub"])
            if inv:
                auth.use_token(inv["token_hash"])
            return signed_in(uid, as_json=False)
        who = info["email"] or "that account"
        return message(request, "No account yet", f"There's no Liner Notes account for {who}. "
                       "Ask the admin for an invite.", status_code=403, **back)
    if row["disabled"]:
        return message(request, "Account disabled", "Ask the admin to turn it back on.", status_code=403, **back)
    return signed_in(row["id"], as_json=False)


# ---------------------------------------------------------------- Spotify connection

@app.get("/spotify/connect")
def spotify_connect(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not spotify.configured():
        return message(request, "Spotify isn't set up yet",
                       "An admin needs to add the Spotify app's client ID and secret on the admin page.",
                       action_url="/admin#spotify" if user["is_admin"] else "/", action_label="Go there"
                       if user["is_admin"] else "Back to the dashboard")
    return RedirectResponse(spotify.authorize_url(user["id"]), status_code=303)


@app.get("/auth/login")
def legacy_connect():
    return RedirectResponse("/spotify/connect", status_code=303)


@app.get("/auth/callback")
async def spotify_callback(request: Request, code: str | None = None, state: str | None = None,
                           error: str | None = None):
    if error:
        return RedirectResponse("/account", status_code=303)
    try:
        await spotify.finish_connect(code, state)
    except spotify.SpotifyError as e:
        return message(request, "Couldn't connect Spotify", str(e), status_code=400,
                       action_url="/account", action_label="Back to your account")
    return RedirectResponse("/", status_code=303)


@app.post("/api/spotify/disconnect")
def spotify_disconnect(request: Request):
    user = auth.require_user(request)
    spotify.disconnect(user["id"])
    return {"message": "Spotify disconnected."}


# ---------------------------------------------------------------- dashboard data

@app.get("/api/status")
def status(request: Request):
    user = auth.require_user(request)
    st = spotify.status(user["id"])
    total = db.q1("SELECT COUNT(*) FROM plays WHERE user_id=?", (user["id"],))[0]
    st.update(total=total, version=user["data_version"], reset=user["data_reset"],
              user={"name": user["name"], "email": user["email"], "is_admin": bool(user["is_admin"])})
    return st


@app.post("/api/sync")
async def sync_now(request: Request):
    user = auth.require_user(request)
    return await spotify.sync_user(user["id"])


@app.get("/api/records")
def get_records(request: Request, after: int = 0):
    """All of this person's plays, or only those added since `after` (a row id from an earlier response).
    Browsers keep a copy and use `after` to fetch just what's new; `reset` changes when history is
    deleted, which tells them to throw their copy away."""
    user = auth.require_user(request)  # read before the rows, so a sync mid-request is caught next time
    cols = ",".join(db.COLS)
    rows = db.q(f"SELECT rowid AS id, {cols} FROM plays WHERE user_id=? AND rowid>? ORDER BY rowid",
                (user["id"], max(0, after)))
    payload = json.dumps({
        "rows": [tuple(r)[1:] for r in rows],
        "max_id": rows[-1]["id"] if rows else max(0, after),
        "version": user["data_version"],
        "reset": user["data_reset"],
    }, separators=(",", ":"), ensure_ascii=False)
    return Response(payload, media_type="application/json", headers={"Cache-Control": "no-store"})


@app.post("/api/records")
async def add_records(request: Request):
    user = auth.require_user(request)
    rows = (await body(request)).get("rows")
    if not isinstance(rows, list):
        raise HTTPException(400, "Send JSON like {\"rows\": [[...], ...]}.")
    return {"added": db.insert_plays(user["id"], rows)}


@app.delete("/api/records")
def delete_records(request: Request):
    user = auth.require_user(request)
    db.run("DELETE FROM plays WHERE user_id=?", (user["id"],))
    db.run("UPDATE spotify SET api_newest=0, gaps='[]' WHERE user_id=?", (user["id"],))
    db.run("UPDATE users SET data_version=data_version+1, data_reset=data_reset+1 WHERE id=?", (user["id"],))
    return {"ok": True}


# ---------------------------------------------------------------- account

def hour_label(h: int) -> str:
    return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return page("account.html", request, user=user, title="Account", layout="page", active="account",
                sp=spotify.status(user["id"]), mail_ready=mailer.configured(), min_password=auth.MIN_PASSWORD,
                weekday=newsletter.WEEKDAYS[config.get("newsletter_weekday")],
                send_hour=hour_label(config.get("newsletter_hour")), **oidc_ctx())


@app.post("/api/account/profile")
async def update_profile(request: Request):
    user = auth.require_user(request)
    data = await body(request)
    email = auth.clean_email(data.get("email"))
    name = str(data.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(400, "Enter a name.")
    if db.q1("SELECT 1 FROM users WHERE email=? AND id<>?", (email, user["id"])):
        raise HTTPException(409, "Another account already uses that email.")
    db.run("UPDATE users SET name=?, email=? WHERE id=?", (name, email, user["id"]))
    return {"message": "Profile saved."}


@app.post("/api/account/password")
async def change_password(request: Request):
    user = auth.require_user(request)
    data = await body(request)
    if user["password_hash"] and not auth.verify_password(str(data.get("current") or ""), user["password_hash"]):
        raise HTTPException(400, "Your current password isn't right.")
    password = auth.check_password_rules(data.get("new"))
    db.run("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(password), user["id"]))
    return {"message": "Password saved."}


@app.post("/api/account/newsletter")
async def newsletter_prefs(request: Request):
    user = auth.require_user(request)
    data = await body(request)
    db.run("UPDATE users SET news_weekly=?, news_monthly=? WHERE id=?",
           (int(bool(data.get("weekly"))), int(bool(data.get("monthly"))), user["id"]))
    return {"message": "Recap settings saved."}


@app.get("/newsletter/preview", response_class=HTMLResponse)
def newsletter_preview(request: Request, kind: str = "month"):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    recap = newsletter.render(user, "week" if kind == "week" else "month")
    return HTMLResponse(recap["html"], headers=PAGE_HEADERS)


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str):
    row = db.q1("SELECT email FROM users WHERE unsub_token=?", (token,))
    if not row:
        return message(request, "This link doesn't work", "It may be from an account that no longer exists.",
                       status_code=404)
    return message(request, "Stop recap emails?", f"{row['email']} won't get weekly or monthly recaps anymore. "
                   "You can turn them back on from your account page.",
                   action_url=f"/unsubscribe/{token}", action_label="Unsubscribe", action_post=True)


@app.post("/unsubscribe/{token}")
def unsubscribe(token: str):
    # Also the target of one-click unsubscribe (RFC 8058) from mail apps, so no header or session needed.
    _, count = db.run("UPDATE users SET news_weekly=0, news_monthly=0 WHERE unsub_token=?", (token,))
    if not count:
        raise HTTPException(404, "Unknown unsubscribe link.")
    return {"message": "You're unsubscribed from recap emails."}


# ---------------------------------------------------------------- admin

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user["is_admin"]:
        return message(request, "Admins only", "Ask an admin if you need a setting changed.", status_code=403)
    users = []
    for row in db.q("SELECT u.*, s.refresh_token sp_token, s.needs_reconnect sp_nr, s.last_error sp_error, "
                    "s.display_name sp_name, (SELECT COUNT(*) FROM plays p WHERE p.user_id=u.id) plays "
                    "FROM users u LEFT JOIN spotify s ON s.user_id=u.id ORDER BY u.created_at"):
        u = dict(row)
        u.update(sp_connected=bool(u["sp_token"]), sp_needs_reconnect=bool(u["sp_nr"]),
                 recaps=[n for n, on in (("Weekly", u["news_weekly"]), ("Monthly", u["news_monthly"])) if on])
        users.append(u)
    invites = [dict(r) for r in db.q("SELECT * FROM tokens WHERE kind='invite' AND used_at IS NULL AND expires_at>? "
                                     "ORDER BY created_at DESC", (time.time(),))]
    return page(
        "admin.html", request, user=user, title="Admin", layout="page", active="admin", users=users, invites=invites,
        s={k: config.get(k) for k in config.FIELDS if k not in config.SECRETS},
        secrets={k: bool(config.get(k)) for k in config.SECRETS},
        spotify_redirect=config.spotify_redirect(), oidc_redirect=config.oidc_redirect(),
        mail_ready=mailer.configured(), newsletter_error=db.kv_get("newsletter_error"),
        weekdays=newsletter.WEEKDAYS, hour_labels=[hour_label(h) for h in range(24)],
    )


@app.post("/api/admin/settings")
async def save_settings(request: Request):
    auth.require_admin(request)
    errors = config.save(await body(request))
    if errors:
        key, err = next(iter(errors.items()))
        raise HTTPException(400, f"{config.LABELS.get(key, key.replace('_', ' ').capitalize())}: {err}")
    return {"message": "Saved."}


@app.post("/api/admin/invites")
async def create_invite(request: Request):
    admin = auth.require_admin(request)
    data = await body(request)
    email = auth.clean_email(data.get("email"))
    if db.q1("SELECT 1 FROM users WHERE email=?", (email,)):
        raise HTTPException(409, "That person already has an account.")
    raw = auth.create_token("invite", email=email, is_admin=bool(data.get("is_admin")), days=7)
    url = link(f"/invite/{raw}")
    if mailer.configured():
        try:
            text = env.get_template("email_invite.txt").render(inviter=admin["name"], link=url)
            await mailer.send_async(email, f"{admin['name']} invited you to Liner Notes", text)
            return {"message": f"Invite emailed to {email}. You can also send this link yourself.", "link": url}
        except Exception as e:
            return {"message": f"Couldn't email the invite ({e}). Send this link to {email} yourself.", "link": url}
    return {"message": f"Send this link to {email}. It works for 7 days.", "link": url}


@app.delete("/api/admin/invites/{token_hash}")
def revoke_invite(request: Request, token_hash: str):
    auth.require_admin(request)
    db.run("UPDATE tokens SET used_at=? WHERE token_hash=? AND kind='invite'", (time.time(), token_hash))
    return {"message": "Invite revoked."}


def _other_user(request: Request, user_id: int) -> tuple[dict, dict]:
    admin = auth.require_admin(request)
    target = auth.get_user(user_id)
    if not target:
        raise HTTPException(404, "That person doesn't exist anymore.")
    if target["id"] == admin["id"]:
        raise HTTPException(400, "You can't do that to your own account here.")
    return admin, target


@app.post("/api/admin/users/{user_id}")
async def admin_user_action(request: Request, user_id: int):
    admin, target = _other_user(request, user_id)
    action = (await body(request)).get("action")
    if action == "make_admin":
        db.run("UPDATE users SET is_admin=1 WHERE id=?", (user_id,))
        return {"message": f"{target['name']} is now an admin."}
    if action == "remove_admin":
        db.run("UPDATE users SET is_admin=0 WHERE id=?", (user_id,))
        return {"message": f"{target['name']} is no longer an admin."}
    if action == "disable":
        db.run("UPDATE users SET disabled=1 WHERE id=?", (user_id,))
        db.run("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return {"message": f"{target['name']} is disabled."}
    if action == "enable":
        db.run("UPDATE users SET disabled=0 WHERE id=?", (user_id,))
        return {"message": f"{target['name']} is enabled."}
    if action == "reset_link":
        raw = auth.create_token("reset", user_id=user_id, days=2)
        url = link(f"/reset/{raw}")
        if mailer.configured():
            try:
                text = env.get_template("email_reset.txt").render(email=target["email"], link=url)
                await mailer.send_async(target["email"], "Set your Liner Notes password", text)
                return {"message": f"Password link emailed to {target['email']}. It works for 2 days.", "link": url}
            except Exception as e:
                return {"message": f"Couldn't email it ({e}). Send this link yourself; it works for 2 days.",
                        "link": url}
        return {"message": f"Send this link to {target['name']}. It works for 2 days.", "link": url}
    raise HTTPException(400, "Unknown action.")


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(request: Request, user_id: int):
    _, target = _other_user(request, user_id)
    db.run("DELETE FROM users WHERE id=?", (user_id,))
    return {"message": f"Deleted {target['name']} and their history."}


@app.post("/api/admin/test-email")
async def test_email(request: Request):
    admin = auth.require_admin(request)
    if not mailer.configured():
        raise HTTPException(400, "Add an SMTP server and a From address, then save, before sending a test.")
    try:
        await newsletter.send_recap(admin, "month", force=True)
    except Exception as e:
        raise HTTPException(502, f"Sending failed: {e}")
    return {"message": f"Sent last month's recap to {admin['email']}."}


@app.post("/api/admin/sync-all")
async def admin_sync_all(request: Request):
    auth.require_admin(request)
    if not spotify.configured():
        raise HTTPException(400, "Add the Spotify client ID and secret first.")
    await spotify.sync_all()
    return {"message": "Checked Spotify for everyone who's connected."}
