#!/usr/bin/env python3
"""Pull recent 4K/Blu-ray titles from blu-ray.com into the local DB."""
from __future__ import annotations

import sys

import db
import mail
import scraper


def pull_releases() -> int:
    items = scraper.gather_releases()
    con = db.connect()
    became = []
    for it in items:
        if db.upsert_release(con, it):
            became.append(it)
    db.set_meta(con, "last_release_refresh", db.now())
    batches = []
    if became:
        for sub in db.preorder_subscribers(con):
            mine = db.watched_among(con, sub["id"], became)
            if mine:
                batches.append((sub["email"], mine))
    con.commit()
    con.close()
    emailed = 0
    for addr, mine in batches:
        try:
            mail.notify_preorders([addr], mine)
            emailed += len(mine)
        except Exception as e:
            print(f"discwatch: pre-order mail failed: {e}", file=sys.stderr, flush=True)
    if emailed:
        print(f"discwatch: emailed {emailed} tracked pre-order alerts", flush=True)
    print(f"discwatch: stored {len(items)} titles", flush=True)
    return len(items)


if __name__ == "__main__":
    try:
        pull_releases()
    except Exception as e:
        print(f"discwatch: release pull failed: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
