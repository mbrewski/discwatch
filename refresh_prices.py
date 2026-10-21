#!/usr/bin/env python3
"""Refresh shop prices for every title someone is watching."""
from __future__ import annotations

import sys

import db
import scraper


def refresh_one(row) -> None:
    bid = row["bluray_id"]
    url = row["url"] if "url" in row.keys() else ""
    if scraper.is_bluray_title_url(url):
        data = scraper.polite_parse_title(url)
        data["bluray_id"] = data.get("bluray_id") or bid
    else:
        year = row["year"] if "year" in row.keys() else None
        data = scraper.prices_for_title(row["title"], row["fmt"] or "4k", year=year)
        data["bluray_id"] = bid
        data["title"] = row["title"]
        data["fmt"] = row["fmt"]
        data["cover"] = None
        data["studio"] = None
        data["release_date"] = None
    con = db.connect()
    db.apply_prices(con, data)
    con.commit()
    con.close()


def pull_prices() -> int:
    con = db.connect()
    rows = db.titles_anyone_watches(con)
    con.close()
    ok = 0
    for r in rows:
        try:
            refresh_one(r)
            ok += 1
        except Exception as e:
            print(f"discwatch: price fail {r['bluray_id']}: {e}", file=sys.stderr, flush=True)
    con = db.connect()
    db.set_meta(con, "last_price_refresh", db.now())
    con.commit()
    con.close()
    print(f"discwatch: priced {ok}/{len(rows)} watched titles", flush=True)
    return ok


if __name__ == "__main__":
    try:
        pull_prices()
    except Exception as e:
        print(f"discwatch: price pull failed: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
