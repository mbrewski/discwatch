from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone

from flask import Flask, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import scraper
from refresh_releases import pull_releases

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["PREFERRED_URL_SCHEME"] = "https"
app.config["SESSION_COOKIE_SECURE"] = True
_lock = threading.Lock()


def money(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"${cents / 100:.2f}"


def parse_target(raw: str | None) -> int | None:
    if not raw:
        return None
    raw = raw.strip().replace("$", "")
    if not raw:
        return None
    return int(round(float(raw) * 100))


@app.context_processor
def inject():
    return {"money": money}


@app.get("/")
def tracked():
    con = db.connect()
    rows = con.execute(
        """SELECT * FROM titles WHERE tracked=1
           ORDER BY (best_price_cents IS NULL), best_price_cents ASC, title"""
    ).fetchall()
    hist = {}
    for r in rows:
        h = con.execute(
            """SELECT retailer, price_cents, seen_at FROM price_history
               WHERE bluray_id=? ORDER BY seen_at DESC LIMIT 8""",
            (r["bluray_id"],),
        ).fetchall()
        hist[r["bluray_id"]] = h
    last = db.get_meta(con, "last_price_refresh")
    con.close()
    return render_template(
        "tracked.html",
        rows=rows,
        hist=hist,
        last=last,
        offers=lambda s: json.loads(s or "[]"),
    )


@app.get("/inbox")
def inbox():
    fmt = request.args.get("fmt", "all")
    q = (request.args.get("q") or "").strip()
    con = db.connect()
    sql = "SELECT * FROM titles WHERE 1=1"
    args = []
    if fmt in ("4k", "bluray"):
        sql += " AND fmt=?"
        args.append(fmt)
    if q:
        sql += " AND (title LIKE ? OR studio LIKE ?)"
        args.extend([f"%{q}%", f"%{q}%"])
    sql += " ORDER BY datetime(updated_at) DESC LIMIT 250"
    rows = con.execute(sql, args).fetchall()
    last = db.get_meta(con, "last_release_refresh")
    con.close()
    return render_template("inbox.html", rows=rows, fmt=fmt, q=q, last=last)


@app.post("/track/<bluray_id>")
def track(bluray_id):
    on = request.form.get("tracked") == "1"
    con = db.connect()
    db.set_tracked(con, bluray_id, on)
    target = parse_target(request.form.get("target"))
    if request.form.get("target") is not None:
        db.set_target(con, bluray_id, target)
    con.commit()
    row = con.execute("SELECT * FROM titles WHERE bluray_id=?", (bluray_id,)).fetchone()
    con.close()
    if on and row:
        try:
            refresh_one(row)
        except Exception:
            pass
    nxt = request.form.get("next") or url_for("inbox")
    return redirect(nxt)


@app.get("/find")
def find():
    q = (request.args.get("q") or "").strip()
    error = request.args.get("error") or ""
    rows = []
    if q:
        m = scraper.TITLE_RE.search(q)
        if m:
            slug, bid = m.group(1), m.group(2)
            url = q.split("?")[0]
            if not url.startswith("http"):
                url = f"{scraper.BASE}/movies/{slug}/{bid}/"
            rows = [
                {
                    "bluray_id": bid,
                    "slug": slug,
                    "title": scraper.slug_title(slug),
                    "url": url,
                    "cover": scraper.cover_url(bid),
                    "fmt": scraper.classify(slug, slug),
                    "year": None,
                }
            ]
        else:
            try:
                rows = scraper.search_catalog(q)
            except Exception as e:
                error = f"Catalog search failed: {e}"
    return render_template("find.html", q=q, rows=rows, error=error)


@app.post("/add")
def add():
    url = (request.form.get("url") or "").strip()
    if not url:
        return redirect(url_for("find"))
    try:
        data = scraper.polite_parse_title(url)
    except Exception as e:
        return redirect(url_for("find", q=url, error=f"Could not load that title: {e}"))
    if not data.get("bluray_id"):
        return redirect(url_for("find", q=url, error="Need a blu-ray.com movie URL."))
    item = {
        "bluray_id": data["bluray_id"],
        "slug": data.get("slug"),
        "title": data["title"],
        "url": data["url"],
        "cover": data.get("cover"),
        "fmt": data.get("fmt"),
        "studio": data.get("studio"),
        "year": None,
        "runtime": None,
        "rating": None,
        "release_date": data.get("release_date"),
        "source": "manual",
    }
    con = db.connect()
    db.upsert_release(con, item)
    db.set_tracked(con, item["bluray_id"], True)
    db.apply_prices(con, data)
    con.commit()
    con.close()
    return redirect(url_for("tracked"))


def manual_id(title: str, fmt: str, year: str | None) -> str:
    key = f"{title.strip().lower()}|{fmt}|{(year or '').strip()}"
    return "m-" + hashlib.sha1(key.encode()).hexdigest()[:12]


@app.post("/add-name")
def add_name():
    title = (request.form.get("title") or "").strip()
    fmt = request.form.get("fmt") or "4k"
    if fmt not in ("4k", "bluray"):
        fmt = "4k"
    year = (request.form.get("year") or "").strip() or None
    if not title:
        return redirect(url_for("find", error="Enter a title to track."))
    bid = manual_id(title, fmt, year)
    item = {
        "bluray_id": bid,
        "slug": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"),
        "title": title,
        "url": "",
        "cover": None,
        "fmt": fmt,
        "studio": None,
        "year": year,
        "runtime": None,
        "rating": None,
        "release_date": None,
        "source": "manual-name",
    }
    con = db.connect()
    db.upsert_release(con, item)
    db.set_tracked(con, bid, True)
    con.commit()
    row = con.execute("SELECT * FROM titles WHERE bluray_id=?", (bid,)).fetchone()
    con.close()
    try:
        refresh_one(row)
    except Exception:
        pass
    return redirect(url_for("tracked"))


@app.post("/refresh/releases")
def refresh_releases():
    with _lock:
        pull_releases()
    return redirect(url_for("inbox"))


@app.post("/refresh/prices")
def refresh_prices():
    with _lock:
        con = db.connect()
        rows = con.execute(
            "SELECT bluray_id, url, title, fmt FROM titles WHERE tracked=1"
        ).fetchall()
        con.close()
        for r in rows:
            try:
                refresh_one(r)
            except Exception:
                continue
        con = db.connect()
        db.set_meta(con, "last_price_refresh", db.now())
        con.commit()
        con.close()
    return redirect(url_for("tracked"))


def refresh_one(row) -> None:
    bid = row["bluray_id"]
    url = row["url"] if "url" in row.keys() else ""
    if scraper.is_bluray_title_url(url):
        data = scraper.polite_parse_title(url)
        data["bluray_id"] = data.get("bluray_id") or bid
    else:
        data = scraper.prices_for_title(
            row["title"], row["fmt"] or "4k", year=row["year"] if "year" in row.keys() else None
        )
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


@app.get("/title/<bluray_id>")
def title_detail(bluray_id):
    con = db.connect()
    row = con.execute("SELECT * FROM titles WHERE bluray_id=?", (bluray_id,)).fetchone()
    if not row:
        con.close()
        return "Not found", 404
    hist = con.execute(
        "SELECT * FROM price_history WHERE bluray_id=? ORDER BY seen_at DESC LIMIT 40",
        (bluray_id,),
    ).fetchall()
    con.close()
    return render_template(
        "title.html", row=row, hist=hist, offers=json.loads(row["last_offers"] or "[]")
    )


if __name__ == "__main__":
    db.connect().close()
    app.run(host="0.0.0.0", port=8790, debug=False)
