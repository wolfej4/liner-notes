"""Weekly and monthly recap emails."""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from . import config, db, mailer
from .views import env, hours

log = logging.getLogger("liner-notes")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def periods(now: datetime) -> dict:
    """The most recent complete week (Mon-Sun) and month, as local datetimes, with stable keys."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday = today - timedelta(days=today.weekday())
    w_start, w_end = this_monday - timedelta(days=7), this_monday
    iso = w_start.isocalendar()
    m_end = today.replace(day=1)
    m_start = (m_end - timedelta(days=1)).replace(day=1)
    return {
        "week": (w_start, w_end, f"{iso[0]}-W{iso[1]:02d}", w_start - timedelta(days=7)),
        "month": (m_start, m_end, m_start.strftime("%Y-%m"), (m_start - timedelta(days=1)).replace(day=1)),
    }


def current_keys() -> tuple[str, str]:
    p = periods(datetime.now(config.tz()))
    return p["week"][2], p["month"][2]


def _ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def compute(user_id: int, start: datetime, end: datetime, prev_start: datetime) -> dict:
    s, e, ps = _ms(start), _ms(end), _ms(prev_start)
    base = "FROM plays WHERE user_id=? AND k='m' AND t>=? AND t<?"
    tot = db.q1(f"SELECT COALESCE(SUM(ms),0) ms, COALESCE(SUM(ms>=30000),0) plays, COUNT(DISTINCT ar) artists {base}",
                (user_id, s, e))
    prev = db.q1(f"SELECT COALESCE(SUM(ms),0) ms {base}", (user_id, ps, s))
    artists = db.q(f"SELECT ar, SUM(ms) ms {base} GROUP BY ar ORDER BY ms DESC LIMIT 5", (user_id, s, e))
    tracks = db.q(f"SELECT tr, ar, COUNT(*) plays {base} AND ms>=30000 GROUP BY tr, ar "
                  "ORDER BY plays DESC, SUM(ms) DESC LIMIT 5", (user_id, s, e))
    new = db.q("SELECT ar, SUM(CASE WHEN t>=? THEN ms ELSE 0 END) ms FROM plays "
               "WHERE user_id=? AND k='m' AND t<? GROUP BY ar HAVING MIN(t)>=? ORDER BY ms DESC",
               (s, user_id, e, s))
    by_day = defaultdict(int)
    tz = config.tz()
    for row in db.q(f"SELECT t, ms {base}", (user_id, s, e)):
        by_day[datetime.fromtimestamp(row["t"] / 1000, tz).date()] += row["ms"]
    busiest = max(by_day.items(), key=lambda kv: kv[1]) if by_day else None
    top_ms = artists[0]["ms"] if artists else 1
    return {
        "ms": tot["ms"], "plays": tot["plays"], "artist_count": tot["artists"], "prev_ms": prev["ms"],
        "artists": [{"name": a["ar"], "ms": a["ms"], "pct": round(a["ms"] / top_ms * 100)} for a in artists],
        "tracks": [{"name": t["tr"], "artist": t["ar"], "plays": t["plays"]} for t in tracks],
        "new_count": len(new), "new_top": new[0]["ar"] if new else None,
        "busiest": {"date": busiest[0], "ms": busiest[1]} if busiest else None,
        "days": len(by_day),
    }


def _period_label(kind: str, start: datetime, end: datetime) -> str:
    if kind == "month":
        return f"{start:%B} {start.year}"
    last = end - timedelta(days=1)
    if start.month == last.month:
        return f"the week of {start:%B} {start.day}–{last.day}"
    return f"the week of {start:%b} {start.day} to {last:%b} {last.day}"


def render(user: dict, kind: str, when: datetime | None = None) -> dict:
    now = when or datetime.now(config.tz())
    start, end, key, prev_start = periods(now)[kind]
    data = compute(user["id"], start, end, prev_start)
    label = _period_label(kind, start, end)
    change = None
    if data["prev_ms"] > 0 and data["ms"] > 0:
        pct = round((data["ms"] / data["prev_ms"] - 1) * 100)
        prev_name = "the week before" if kind == "week" else f"{prev_start:%B}"
        if pct >= 5:
            change = f"That's {pct}% more than {prev_name}."
        elif pct <= -5:
            change = f"That's {-pct}% less than {prev_name}."
        else:
            change = f"About the same as {prev_name}."
    top = data["artists"][0]["name"] if data["artists"] else None
    headline = "Your week in music" if kind == "week" else f"Your {start:%B} in music"
    subject = f"{headline}: {hours(data['ms'])}" + (f", led by {top}" if top else "")
    public = config.get("public_url")
    ctx = dict(user=user, kind=kind, data=data, label=label, change=change, headline=headline,
               dashboard=public + "/", account=public + "/account",
               unsubscribe=f"{public}/unsubscribe/{user['unsub_token']}",
               busiest_label=(f"{data['busiest']['date']:%A}, {data['busiest']['date']:%B} "
                              f"{data['busiest']['date'].day}") if data["busiest"] else None)
    return {
        "key": key, "data": data, "subject": subject,
        "html": env.get_template("email_recap.html").render(**ctx),
        "text": env.get_template("email_recap.txt").render(**ctx),
        "unsubscribe": ctx["unsubscribe"],
    }


async def send_recap(user: dict, kind: str, force: bool = False, to: str | None = None) -> str:
    """Returns 'sent' or 'empty' (nothing played, so nothing sent). Raises on SMTP errors."""
    recap = render(user, kind)
    if recap["data"]["plays"] == 0 and not force:
        return "empty"
    await mailer.send_async(
        to or user["email"], recap["subject"], recap["text"], recap["html"],
        headers={"List-Unsubscribe": f"<{recap['unsubscribe']}>",
                 "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    )
    return "sent"


async def run_due() -> None:
    if not mailer.configured():
        return
    now = datetime.now(config.tz())
    send_hour, send_day = config.get("newsletter_hour"), config.get("newsletter_weekday")
    p = periods(now)
    weekly_due = now.weekday() > send_day or (now.weekday() == send_day and now.hour >= send_hour)
    monthly_due = (now.day > 1 or now.hour >= send_hour) and now.day <= 7
    users = [dict(u) for u in db.q("SELECT * FROM users WHERE disabled=0 AND (news_weekly=1 OR news_monthly=1)")]
    for user in users:
        for kind, due, flag, col in (("week", weekly_due, "news_weekly", "last_weekly"),
                                     ("month", monthly_due, "news_monthly", "last_monthly")):
            key = p[kind][2]
            if not (due and user[flag] and user[col] != key):
                continue
            try:
                result = await send_recap(user, kind)
            except Exception as e:  # keep going for everyone else; retry on the next check
                db.kv_set("newsletter_error", f"{datetime.now(config.tz()):%b %d %I:%M %p}: {e}")
                log.warning("recap to %s failed: %s", user["email"], e)
                continue
            db.run(f"UPDATE users SET {col}=? WHERE id=?", (key, user["id"]))
            if result == "sent":
                db.kv_del("newsletter_error")
                log.info("sent %sly recap to %s", kind, user["email"])


async def loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await run_due()
        except Exception:
            log.exception("newsletter check failed")
        await asyncio.sleep(600)
