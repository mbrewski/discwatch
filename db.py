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
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
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
               url=?, cover=COALESCE(?,cover), fmt=COALESCE(?,fmt),
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
               runtime,rating,release_date,source,tracked,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
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
                ts,
                ts,
            ),
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
