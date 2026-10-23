from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "discwatch.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
  bluray_id TEXT PRIMARY KEY,
  slug TEXT,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  cover TEXT,
  fmt TEXT,
  studio TEXT,
  year TEXT,
  runtime TEXT,
  rating TEXT,
  release_date TEXT,
  source TEXT,
  tracked INTEGER NOT NULL DEFAULT 0,
  target_cents INTEGER,
  notes TEXT,
  list_price_cents INTEGER,
  best_price_cents INTEGER,
  amazon_url TEXT,
  last_offers TEXT,
  last_fetch TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bluray_id TEXT NOT NULL,
  retailer TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_title ON price_history(bluray_id, seen_at);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'approved',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watches (
  user_id INTEGER NOT NULL,
  bluray_id TEXT NOT NULL,
  target_cents INTEGER,
  notes TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, bluray_id)
);
CREATE INDEX IF NOT EXISTS idx_watches_title ON watches(bluray_id);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    # Background refreshes write concurrently with request threads; wait rather than
    # failing straight away with "database is locked".
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    if "status" not in cols:
        con.execute(
            "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'"
        )
        con.commit()
    tcols = {r[1] for r in con.execute("PRAGMA table_info(titles)")}
    if "inbox_hidden" not in tcols:
        con.execute(
            "ALTER TABLE titles ADD COLUMN inbox_hidden INTEGER NOT NULL DEFAULT 0"
        )
        con.execute(
            """UPDATE titles SET inbox_hidden=1
               WHERE source IN ('manual','manual-name','search')"""
        )
        con.commit()
    return con


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_release(con: sqlite3.Connection, item: dict) -> None:
    ts = now()
    row = con.execute(
        "SELECT tracked FROM titles WHERE bluray_id=?", (item["bluray_id"],)
    ).fetchone()
    if row:
        con.execute(
            """UPDATE titles SET slug=COALESCE(?,slug), title=COALESCE(?,title),
               url=COALESCE(NULLIF(?,''),url), cover=COALESCE(?,cover), fmt=COALESCE(?,fmt),
               studio=COALESCE(?,studio), year=COALESCE(?,year),
               runtime=COALESCE(?,runtime), rating=COALESCE(?,rating),
               release_date=COALESCE(?,release_date),
               source=COALESCE(?,source), updated_at=?
               WHERE bluray_id=?""",
            (
                item.get("slug"),
                item.get("title"),
                item["url"],
                item.get("cover"),
                item.get("fmt"),
                item.get("studio"),
                item.get("year"),
                item.get("runtime"),
                item.get("rating"),
                item.get("release_date"),
                item.get("source"),
                ts,
                item["bluray_id"],
            ),
        )
    else:
        con.execute(
            """INSERT INTO titles(bluray_id,slug,title,url,cover,fmt,studio,year,
               runtime,rating,release_date,source,tracked,inbox_hidden,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
            (
                item["bluray_id"],
                item.get("slug"),
                item["title"],
                item["url"],
                item.get("cover"),
                item.get("fmt"),
                item.get("studio"),
                item.get("year"),
                item.get("runtime"),
                item.get("rating"),
                item.get("release_date"),
                item.get("source"),
                1 if item.get("inbox_hidden") else 0,
                ts,
                ts,
            ),
        )


def hide_from_inbox(con: sqlite3.Connection, bluray_id: str) -> None:
    con.execute(
        "UPDATE titles SET inbox_hidden=1, updated_at=? WHERE bluray_id=?",
        (now(), bluray_id),
    )


def set_tracked(con: sqlite3.Connection, bluray_id: str, tracked: bool) -> None:
    con.execute(
        "UPDATE titles SET tracked=?, updated_at=? WHERE bluray_id=?",
        (1 if tracked else 0, now(), bluray_id),
    )


def set_target(con: sqlite3.Connection, bluray_id: str, cents: int | None) -> None:
    con.execute(
        "UPDATE titles SET target_cents=?, updated_at=? WHERE bluray_id=?",
        (cents, now(), bluray_id),
    )


def apply_prices(con: sqlite3.Connection, data: dict) -> None:
    ts = data.get("fetched_at") or now()
    con.execute(
        """UPDATE titles SET title=COALESCE(?,title), cover=COALESCE(?,cover),
           fmt=COALESCE(?,fmt), studio=COALESCE(?,studio),
           release_date=COALESCE(?,release_date),
           list_price_cents=?, best_price_cents=?, amazon_url=?,
           last_offers=?, last_fetch=?, updated_at=?
           WHERE bluray_id=?""",
        (
            data.get("title"),
            data.get("cover"),
            data.get("fmt"),
            data.get("studio"),
            data.get("release_date"),
            data.get("list_price_cents"),
            data.get("best_price_cents"),
            data.get("amazon_url"),
            json.dumps(data.get("offers") or []),
            ts,
            ts,
            data["bluray_id"],
        ),
    )
    for off in data.get("offers") or []:
        if off.get("retailer") == "List / MSRP":
            continue
        last = con.execute(
            """SELECT price_cents FROM price_history
               WHERE bluray_id=? AND retailer=? ORDER BY id DESC LIMIT 1""",
            (data["bluray_id"], off["retailer"]),
        ).fetchone()
        if last and last["price_cents"] == off["price_cents"]:
            continue
        con.execute(
            "INSERT INTO price_history(bluray_id,retailer,price_cents,seen_at) VALUES(?,?,?,?)",
            (data["bluray_id"], off["retailer"], off["price_cents"], ts),
        )


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def user_count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user(con: sqlite3.Connection, user_id: int):
    return con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_name(con: sqlite3.Connection, username: str):
    return con.execute(
        "SELECT * FROM users WHERE username=?", (username.strip().lower(),)
    ).fetchone()


def list_users(con: sqlite3.Connection):
    return con.execute(
        """SELECT id, username, is_admin, status, created_at FROM users
           ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, username"""
    ).fetchall()


def create_user(
    con: sqlite3.Connection,
    username: str,
    password_hash: str,
    is_admin: bool,
    status: str = "approved",
) -> int:
    if status not in ("pending", "approved"):
        status = "pending"
    cur = con.execute(
        """INSERT INTO users(username, password_hash, is_admin, status, created_at)
           VALUES(?,?,?,?,?)""",
        (
            username.strip().lower(),
            password_hash,
            1 if is_admin else 0,
            status,
            now(),
        ),
    )
    return int(cur.lastrowid)


def set_user_status(con: sqlite3.Connection, user_id: int, status: str) -> None:
    con.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))


def delete_user(con: sqlite3.Connection, user_id: int) -> None:
    con.execute("DELETE FROM watches WHERE user_id=?", (user_id,))
    con.execute("DELETE FROM users WHERE id=?", (user_id,))


def admin_count(con: sqlite3.Connection) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM users WHERE is_admin=1 AND status='approved'"
    ).fetchone()[0]


def set_watch(
    con: sqlite3.Connection, user_id: int, bluray_id: str, on: bool, target_cents=None
) -> None:
    if on:
        existing = con.execute(
            "SELECT target_cents FROM watches WHERE user_id=? AND bluray_id=?",
            (user_id, bluray_id),
        ).fetchone()
        if existing:
            if target_cents is not None:
                con.execute(
                    "UPDATE watches SET target_cents=? WHERE user_id=? AND bluray_id=?",
                    (target_cents, user_id, bluray_id),
                )
        else:
            con.execute(
                """INSERT INTO watches(user_id, bluray_id, target_cents, created_at)
                   VALUES(?,?,?,?)""",
                (user_id, bluray_id, target_cents, now()),
            )
    else:
        con.execute(
            "DELETE FROM watches WHERE user_id=? AND bluray_id=?",
            (user_id, bluray_id),
        )


def set_watch_target(
    con: sqlite3.Connection, user_id: int, bluray_id: str, cents: int | None
) -> None:
    con.execute(
        "UPDATE watches SET target_cents=? WHERE user_id=? AND bluray_id=?",
        (cents, user_id, bluray_id),
    )


def is_watching(con: sqlite3.Connection, user_id: int, bluray_id: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM watches WHERE user_id=? AND bluray_id=?",
        (user_id, bluray_id),
    ).fetchone()
    return bool(row)


def list_watched(con: sqlite3.Connection, user_id: int):
    return con.execute(
        """SELECT t.bluray_id, t.slug, t.title, t.url, t.cover, t.fmt, t.studio,
                  t.year, t.release_date, t.list_price_cents, t.best_price_cents,
                  t.last_offers, t.amazon_url,
                  w.target_cents AS target_cents
           FROM watches w JOIN titles t ON t.bluray_id=w.bluray_id
           WHERE w.user_id=?
           ORDER BY (t.best_price_cents IS NULL), t.best_price_cents ASC, t.title""",
        (user_id,),
    ).fetchall()


def titles_anyone_watches(con: sqlite3.Connection):
    return con.execute(
        """SELECT DISTINCT t.bluray_id, t.url, t.title, t.fmt, t.year
           FROM titles t JOIN watches w ON w.bluray_id=t.bluray_id"""
    ).fetchall()


def migrate_legacy_tracks(con: sqlite3.Connection, user_id: int) -> int:
    rows = con.execute(
        "SELECT bluray_id, target_cents FROM titles WHERE tracked=1"
    ).fetchall()
    n = 0
    for r in rows:
        if is_watching(con, user_id, r["bluray_id"]):
            continue
        con.execute(
            """INSERT INTO watches(user_id, bluray_id, target_cents, created_at)
               VALUES(?,?,?,?)""",
            (user_id, r["bluray_id"], r["target_cents"], now()),
        )
        n += 1
    return n
