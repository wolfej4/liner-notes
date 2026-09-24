"""SQLite storage: schema, migrations, and small query helpers."""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

# One row per play. x = source: 0 basic export, 1 extended export, 2 synced from the Spotify API.
COLS = ("t", "ms", "k", "tr", "ar", "al", "pf", "cc", "sk", "sh", "off", "x")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL DEFAULT '',
  password_hash TEXT,
  oidc_sub TEXT UNIQUE,
  is_admin INTEGER NOT NULL DEFAULT 0,
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  last_login REAL,
  news_weekly INTEGER NOT NULL DEFAULT 0,
  news_monthly INTEGER NOT NULL DEFAULT 1,
  last_weekly TEXT,
  last_monthly TEXT,
  unsub_token TEXT NOT NULL,
  data_version INTEGER NOT NULL DEFAULT 0,
  data_reset INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens(
  token_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  email TEXT,
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  used_at REAL
);
CREATE TABLE IF NOT EXISTS oauth_states(
  state TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  user_id INTEGER,
  data TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spotify(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  access_token TEXT,
  refresh_token TEXT,
  expires_at REAL NOT NULL DEFAULT 0,
  consented_at REAL,
  display_name TEXT,
  needs_reconnect INTEGER NOT NULL DEFAULT 0,
  last_sync REAL,
  last_error TEXT,
  api_newest INTEGER NOT NULL DEFAULT 0,
  gaps TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS plays(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  t INTEGER NOT NULL, ms INTEGER NOT NULL, k TEXT NOT NULL,
  tr TEXT NOT NULL, ar TEXT NOT NULL, al TEXT NOT NULL DEFAULT '',
  pf TEXT NOT NULL DEFAULT '', cc TEXT NOT NULL DEFAULT '',
  sk INTEGER NOT NULL DEFAULT 0, sh INTEGER NOT NULL DEFAULT 0, off INTEGER NOT NULL DEFAULT 0,
  x INTEGER NOT NULL,
  UNIQUE(user_id, t, ms, tr)
);
CREATE INDEX IF NOT EXISTS plays_user_t ON plays(user_id, t);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DATA_DIR / "liner-notes.db", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not os.access(DATA_DIR, os.W_OK | os.X_OK):
        raise SystemExit(
            f"Liner Notes can't write to {DATA_DIR} (running as uid {os.getuid()}, gid {os.getgid()}). "
            f"If /data is a host folder, run on the Docker host: chown -R {os.getuid()}:{os.getgid()} <that folder>"
        )
    with closing(connect()) as con:
        con.execute("PRAGMA journal_mode=WAL")
        cols = [r[1] for r in con.execute("PRAGMA table_info(plays)")]
        if cols and "user_id" not in cols:
            # Single-user install from before accounts existed. The first admin account adopts it.
            con.execute("ALTER TABLE plays RENAME TO plays_legacy")
            con.execute("DROP INDEX IF EXISTS plays_t")
        con.executescript(SCHEMA)
        ucols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
        if "data_reset" not in ucols:
            con.execute("ALTER TABLE users ADD COLUMN data_reset INTEGER NOT NULL DEFAULT 0")
        con.commit()


def q(sql, params=()):
    with closing(connect()) as con:
        return con.execute(sql, params).fetchall()


def q1(sql, params=()):
    with closing(connect()) as con:
        return con.execute(sql, params).fetchone()


def run(sql, params=()):
    """Execute one statement; returns (lastrowid, rowcount)."""
    with closing(connect()) as con, con:
        cur = con.execute(sql, params)
        return cur.lastrowid, cur.rowcount


def kv_get(key, default=None):
    row = q1("SELECT v FROM kv WHERE k=?", (key,))
    return json.loads(row["v"]) if row else default


def kv_set(key, value) -> None:
    run("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(value)))


def kv_del(*keys) -> None:
    with closing(connect()) as con, con:
        con.executemany("DELETE FROM kv WHERE k=?", [(k,) for k in keys])


def pop_state(state: str, kind: str, max_age: int = 600):
    """Fetch and delete a one-time OAuth state. Returns None if unknown or expired."""
    import time
    if not state:
        return None
    row = q1("SELECT * FROM oauth_states WHERE state=? AND kind=?", (state, kind))
    run("DELETE FROM oauth_states WHERE state=? OR created_at<?", (state, time.time() - max_age))
    if not row or time.time() - row["created_at"] > max_age:
        return None
    return dict(row)


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


def insert_plays(user_id: int, rows) -> int:
    clean = [c for c in (clean_row(r) for r in rows) if c]
    if not clean:
        return 0
    with closing(connect()) as con, con:
        before = con.total_changes
        con.executemany(
            f"INSERT OR IGNORE INTO plays(user_id,{','.join(COLS)}) VALUES(?,{','.join('?' * len(COLS))})",
            [(user_id, *c) for c in clean],
        )
        added = con.total_changes - before
        if added:
            con.execute("UPDATE users SET data_version=data_version+1 WHERE id=?", (user_id,))
    return added


def adopt_legacy(user_id: int) -> None:
    """Move a pre-accounts install's plays and Spotify connection to this user."""
    cols = ",".join(COLS)
    with closing(connect()) as con, con:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plays_legacy'").fetchone():
            con.execute(f"INSERT OR IGNORE INTO plays(user_id,{cols}) SELECT ?,{cols} FROM plays_legacy", (user_id,))
            con.execute("DROP TABLE plays_legacy")
            con.execute("UPDATE users SET data_version=data_version+1 WHERE id=?", (user_id,))
    tok = kv_get("token")
    if tok:
        run(
            "INSERT OR REPLACE INTO spotify(user_id, access_token, refresh_token, expires_at, consented_at,"
            " display_name, last_sync, api_newest, gaps) VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, tok.get("access_token"), tok.get("refresh_token"), tok.get("expires_at", 0),
             tok.get("consented_at"), (kv_get("profile") or {}).get("name"), kv_get("last_sync"),
             kv_get("api_newest", 0), json.dumps(kv_get("gaps", []))),
        )
    kv_del("token", "profile", "last_sync", "api_newest", "gaps", "needs_reconnect", "last_error", "oauth_state", "version")
