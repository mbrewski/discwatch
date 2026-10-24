from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import db
import scraper
from refresh_prices import pull_prices, refresh_one
from refresh_releases import pull_releases

SECRET_FILE = Path(__file__).resolve().parent / "data" / "secret_key"
USER_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")
LOGIN_FAIL_WINDOW = 900
LOGIN_FAIL_MAX = 8

log = logging.getLogger("discwatch.app")


def _secret_key() -> str:
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_FILE.write_text(key)
    SECRET_FILE.chmod(0o600)
    return key


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["PREFERRED_URL_SCHEME"] = "https"
app.secret_key = _secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Deliberately False: LAN browsing over plain http://127.0.0.1:8790 has to keep a session.
# The trade-off is real — any request that lands on an http:// origin sends the session
# cookie in cleartext. Accepted because public traffic goes over HTTPS via the tunnel.
app.config["SESSION_COOKIE_SECURE"] = False

# Hashing a throwaway password keeps the "no such user" branch of login as slow as a real
# check, so response time does not leak which usernames exist.
DUMMY_HASH = generate_password_hash("discwatch-no-such-user")

# Bulk refreshes run on their own daemon thread; per-title refreshes queue on a small pool
# so adding several titles at once cannot spawn an unbounded number of threads.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="discwatch-title")
_login_fails: dict[tuple, list[float]] = {}
_login_fails_lock = threading.Lock()


def _run_job(name: str, fn) -> bool:
    """Start fn in the background. False if a job of that name is still running."""
    with _jobs_lock:
        cur = _jobs.get(name)
        if cur and not cur["finished"]:
            return False
        _jobs[name] = {"started": db.now(), "finished": None, "error": None}

    def runner():
        err = None
        try:
            fn()
        except Exception as e:
            err = str(e)
            log.warning("background job %s failed: %s", name, e)
        with _jobs_lock:
            _jobs[name].update(finished=db.now(), error=err)

    threading.Thread(target=runner, name=f"discwatch-{name}", daemon=True).start()
    return True


def job_status(name: str) -> dict | None:
    with _jobs_lock:
        j = _jobs.get(name)
        return dict(j) if j else None


def _refresh_title_async(row) -> None:
    """Queue a single-title price refresh; sqlite3.Row holds values, so the closed
    connection it came from does not matter."""
    if row is None:
        return

    def work():
        try:
            refresh_one(row)
        except Exception as e:
            log.warning("price refresh failed for %s: %s", row["bluray_id"], e)

    _pool.submit(work)


def safe_next(raw: str | None, fallback: str) -> str:
    """Same-site paths only. '//host' and '/\\host' are protocol-relative and would
    send the browser off-site."""
    if not raw or not raw.startswith("/"):
        return fallback
    if raw.startswith("//") or raw.startswith("/\\"):
        return fallback
    return raw


def _login_key(username: str) -> tuple:
    return (request.remote_addr or "?", username)


def _login_blocked(username: str) -> bool:
    cutoff = time.time() - LOGIN_FAIL_WINDOW
    with _login_fails_lock:
        for k in list(_login_fails):
            kept = [t for t in _login_fails[k] if t > cutoff]
            if kept:
                _login_fails[k] = kept
            else:
                del _login_fails[k]
        return len(_login_fails.get(_login_key(username), [])) >= LOGIN_FAIL_MAX


def _note_login_fail(username: str) -> None:
    with _login_fails_lock:
        _login_fails.setdefault(_login_key(username), []).append(time.time())


def _clear_login_fails(username: str) -> None:
    with _login_fails_lock:
        _login_fails.pop(_login_key(username), None)


def money(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"${cents / 100:.2f}"


def parse_target(raw: str | None) -> int | None:
    if not raw:
        return None
    raw = raw.strip().replace("$", "").replace(",", "")
    if not raw:
        return None
    try:
        return int(round(float(raw) * 100))
    except ValueError:
        return None


@app.context_processor
def inject():
    return {"money": money, "user": getattr(g, "user", None)}


@app.before_request
def gate():
    endpoint = request.endpoint or ""
    if endpoint in {
        "login",
        "login_post",
        "signup",
        "signup_post",
        "setup",
        "setup_post",
        "static",
    } or endpoint.startswith("static"):
        if endpoint in {"setup", "setup_post"}:
            con = db.connect()
            n = db.user_count(con)
            con.close()
            if n > 0:
                return redirect(url_for("login"))
        return None
    con = db.connect()
    n = db.user_count(con)
    if n == 0:
        con.close()
        return redirect(url_for("setup"))
    uid = session.get("user_id")
    user = db.get_user(con, uid) if uid else None
    con.close()
    if not user or user["status"] != "approved":
        session.clear()
        return redirect(url_for("login", next=request.path))
    g.user = user
    return None


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not g.user or not g.user["is_admin"]:
            return "Admins only", 403
        return fn(*args, **kwargs)

    return wrapped


@app.get("/setup")
def setup():
    return render_template("setup.html")


@app.post("/setup")
def setup_post():
    con = db.connect()
    if db.user_count(con) > 0:
        con.close()
        return redirect(url_for("login"))
    username = (request.form.get("username") or "").strip().lower()
    password = request.form.get("password") or ""
    if not USER_RE.match(username):
        con.close()
        return render_template("setup.html", error="Username: 3–32 letters, numbers, . _ -")
    if len(password) < 8:
        con.close()
        return render_template("setup.html", error="Password must be at least 8 characters.")
    uid = db.create_user(con, username, generate_password_hash(password), True)
    moved = db.migrate_legacy_tracks(con, uid)
    con.commit()
    con.close()
    session.clear()
    session["user_id"] = uid
    session.permanent = True
    return redirect(url_for("tracked"))


@app.get("/login")
def login():
    con = db.connect()
    n = db.user_count(con)
    con.close()
    if n == 0:
        return redirect(url_for("setup"))
    return render_template(
        "login.html", error=None, next=safe_next(request.args.get("next"), "")
    )


@app.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip().lower()
    password = request.form.get("password") or ""
    nxt = safe_next(request.form.get("next"), url_for("tracked"))
    if _login_blocked(username):
        return render_template(
            "login.html",
            error="Too many failed attempts. Wait a few minutes and try again.",
            next=nxt,
        )
    con = db.connect()
    user = db.get_user_by_name(con, username)
    con.close()
    if not user:
        check_password_hash(DUMMY_HASH, password)
    if not user or not check_password_hash(user["password_hash"], password):
        _note_login_fail(username)
        return render_template("login.html", error="Wrong username or password.", next=nxt)
    if user["status"] != "approved":
        return render_template(
            "login.html",
            error="That account is waiting for admin approval.",
            next=nxt,
        )
    _clear_login_fails(username)
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    return redirect(nxt)


@app.get("/signup")
def signup():
    con = db.connect()
    n = db.user_count(con)
    con.close()
    if n == 0:
        return redirect(url_for("setup"))
    return render_template("signup.html", error=None, msg=None)


@app.post("/signup")
def signup_post():
    username = (request.form.get("username") or "").strip().lower()
    password = request.form.get("password") or ""
    if not USER_RE.match(username):
        return render_template(
            "signup.html", error="Username: 3–32 letters, numbers, . _ -", msg=None
        )
    if len(password) < 8:
        return render_template(
            "signup.html", error="Password must be at least 8 characters.", msg=None
        )
    con = db.connect()
    if db.user_count(con) == 0:
        con.close()
        return redirect(url_for("setup"))
    if db.get_user_by_name(con, username):
        con.close()
        return render_template("signup.html", error="That username is taken.", msg=None)
    db.create_user(
        con, username, generate_password_hash(password), False, status="pending"
    )
    con.commit()
    con.close()
    return render_template(
        "signup.html",
        error=None,
        msg="Request submitted. An admin has to approve you before you can log in.",
    )


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/users")
@admin_required
def users():
    con = db.connect()
    rows = db.list_users(con)
    last_prices = db.get_meta(con, "last_price_refresh")
    con.close()
    pending = [r for r in rows if r["status"] == "pending"]
    active = [r for r in rows if r["status"] != "pending"]
    return render_template(
        "users.html",
        pending=pending,
        active=active,
        last_prices=last_prices,
        job=job_status("prices"),
        error=request.args.get("error"),
        msg=request.args.get("msg"),
    )


@app.post("/users")
@admin_required
def users_create():
    username = (request.form.get("username") or "").strip().lower()
    password = request.form.get("password") or ""
    is_admin = request.form.get("is_admin") == "1"
    if not USER_RE.match(username):
        return redirect(url_for("users", error="Username: 3–32 letters, numbers, . _ -"))
    if len(password) < 8:
        return redirect(url_for("users", error="Password must be at least 8 characters."))
    con = db.connect()
    if db.get_user_by_name(con, username):
        con.close()
        return redirect(url_for("users", error="That username is taken."))
    db.create_user(
        con, username, generate_password_hash(password), is_admin, status="approved"
    )
    con.commit()
    con.close()
    return redirect(url_for("users", msg=f"Added {username}."))


@app.post("/users/<int:user_id>/delete")
@admin_required
def users_delete(user_id):
    if user_id == g.user["id"]:
        return redirect(url_for("users", error="You cannot delete your own account here."))
    con = db.connect()
    target = db.get_user(con, user_id)
    if not target:
        con.close()
        return redirect(url_for("users", error="No such user."))
    if target["is_admin"] and db.admin_count(con) <= 1:
        con.close()
        return redirect(url_for("users", error="Keep at least one admin."))
    db.delete_user(con, user_id)
    con.commit()
    con.close()
    return redirect(url_for("users", msg="User removed."))


@app.post("/users/<int:user_id>/approve")
@admin_required
def users_approve(user_id):
    con = db.connect()
    target = db.get_user(con, user_id)
    if not target:
        con.close()
        return redirect(url_for("users", error="No such user."))
    db.set_user_status(con, user_id, "approved")
    con.commit()
    con.close()
    return redirect(url_for("users", msg=f"Approved {target['username']}."))


@app.post("/users/<int:user_id>/deny")
@admin_required
def users_deny(user_id):
    if user_id == g.user["id"]:
        return redirect(url_for("users", error="You cannot deny your own account."))
    con = db.connect()
    target = db.get_user(con, user_id)
    if not target:
        con.close()
        return redirect(url_for("users", error="No such user."))
    if target["is_admin"] and db.admin_count(con) <= 1:
        con.close()
        return redirect(url_for("users", error="Keep at least one admin."))
    name = target["username"]
    db.delete_user(con, user_id)
    con.commit()
    con.close()
    return redirect(url_for("users", msg=f"Denied {name}."))


@app.get("/")
def tracked():
    con = db.connect()
    rows = db.list_watched(con, g.user["id"])
    last = db.get_meta(con, "last_price_refresh")
    others = db.list_other_users(con, g.user["id"])
    con.close()
    return render_template(
        "tracked.html",
        rows=rows,
        last=last,
        others=others,
        offers=lambda s: json.loads(s or "[]"),
        msg=request.args.get("msg"),
        error=request.args.get("error"),
    )


@app.post("/share/<bluray_id>")
def share(bluray_id):
    to_user_id = request.form.get("to_user_id", type=int)
    con = db.connect()
    title_row = con.execute(
        "SELECT title FROM titles WHERE bluray_id=?", (bluray_id,)
    ).fetchone()
    target = db.get_user(con, to_user_id) if to_user_id else None
    if not title_row:
        con.close()
        return redirect(url_for("tracked", error="No such title."))
    if not target or target["status"] != "approved" or target["id"] == g.user["id"]:
        con.close()
        return redirect(url_for("tracked", error="Pick a valid person to share with."))
    db.set_watch(con, target["id"], bluray_id, True, shared_by=g.user["id"])
    con.commit()
    con.close()
    return redirect(
        url_for("tracked", msg=f"Shared \u201c{title_row['title']}\u201d with {target['username']}.")
    )


@app.get("/inbox")
def inbox():
    fmt = request.args.get("fmt", "all")
    q = (request.args.get("q") or "").strip()
    con = db.connect()
    sql = """SELECT t.*, 0 AS watching
             FROM titles t
             WHERE t.inbox_hidden=0
               AND NOT EXISTS (
                   SELECT 1 FROM watches w WHERE w.bluray_id=t.bluray_id AND w.user_id=?
               )"""
    args: list = [g.user["id"]]
    if fmt in ("4k", "bluray"):
        sql += " AND t.fmt=?"
        args.append(fmt)
    if q:
        sql += " AND (t.title LIKE ? OR t.studio LIKE ?)"
        args.extend([f"%{q}%", f"%{q}%"])
    # created_at is written once on insert and never touched again, so it is a true
    # "first seen" key. updated_at is rewritten for every row on every pull, which made
    # this sort by pull order -- and since gather_releases() runs parse_rss() first, the
    # actual new releases sorted to the bottom. Title breaks ties within a bulk insert.
    sql += " ORDER BY datetime(t.created_at) DESC, t.title LIMIT 250"
    rows = con.execute(sql, args).fetchall()
    last = db.get_meta(con, "last_release_refresh")
    con.close()
    return render_template(
        "inbox.html",
        rows=rows,
        fmt=fmt,
        q=q,
        last=last,
        msg=request.args.get("msg"),
        job=job_status("releases"),
    )


@app.post("/inbox/hide/<bluray_id>")
def inbox_hide(bluray_id):
    con = db.connect()
    db.hide_from_inbox(con, bluray_id)
    con.commit()
    con.close()
    return redirect(safe_next(request.form.get("next"), url_for("inbox")))


@app.post("/track/<bluray_id>")
def track(bluray_id):
    on = request.form.get("tracked") == "1"
    target = parse_target(request.form.get("target"))
    con = db.connect()
    was = db.is_watching(con, g.user["id"], bluray_id)
    if on:
        db.set_watch(con, g.user["id"], bluray_id, True, target)
    else:
        db.set_watch(con, g.user["id"], bluray_id, False)
    if on and request.form.get("target") is not None:
        db.set_watch_target(con, g.user["id"], bluray_id, target)
    con.commit()
    row = con.execute("SELECT * FROM titles WHERE bluray_id=?", (bluray_id,)).fetchone()
    con.close()
    if on and row and not was:
        _refresh_title_async(row)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return ("", 204)
    return redirect(safe_next(request.form.get("next"), url_for("inbox")))


@app.get("/find")
def find():
    q = (request.args.get("q") or "").strip()
    error = request.args.get("error") or ""
    rows = []
    if q:
        canon = scraper.canonical_title_url(q)
        if canon:
            m = scraper.TITLE_RE.search(q)
            slug, bid = m.group(1), m.group(2)
            rows = [
                {
                    "bluray_id": bid,
                    "slug": slug,
                    "title": scraper.slug_title(slug),
                    "url": canon,
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
    raw = (request.form.get("url") or "").strip()
    if not raw:
        return redirect(url_for("find"))
    # Rebuild the URL from the matched slug + id rather than fetching what was posted,
    # so the host we hit is never attacker-controlled.
    url = scraper.canonical_title_url(raw)
    if not url:
        return redirect(url_for("find", q=raw, error="Need a blu-ray.com movie URL."))
    try:
        data = scraper.polite_parse_title(url)
    except Exception as e:
        return redirect(url_for("find", q=raw, error=f"Could not load that title: {e}"))
    if not data.get("bluray_id"):
        return redirect(url_for("find", q=raw, error="Need a blu-ray.com movie URL."))
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
        "inbox_hidden": 1,
    }
    con = db.connect()
    db.upsert_release(con, item)
    db.set_watch(con, g.user["id"], item["bluray_id"], True)
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
        "inbox_hidden": 1,
    }
    con = db.connect()
    db.upsert_release(con, item)
    db.set_watch(con, g.user["id"], bid, True)
    con.commit()
    row = con.execute("SELECT * FROM titles WHERE bluray_id=?", (bid,)).fetchone()
    con.close()
    _refresh_title_async(row)
    return redirect(url_for("tracked"))


@app.post("/refresh/releases")
@admin_required
def refresh_releases():
    started = _run_job("releases", pull_releases)
    return redirect(
        url_for(
            "inbox",
            msg="Release pull started."
            if started
            else "A release pull is already running.",
        )
    )


@app.post("/refresh/prices")
@admin_required
def refresh_prices():
    started = _run_job("prices", pull_prices)
    return redirect(
        url_for(
            "users",
            msg="Price refresh started."
            if started
            else "A price refresh is already running.",
        )
    )


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
    watching = db.is_watching(con, g.user["id"], bluray_id)
    con.close()
    return render_template(
        "title.html",
        row=row,
        hist=hist,
        offers=json.loads(row["last_offers"] or "[]"),
        watching=watching,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    db.connect().close()
    app.run(host="0.0.0.0", port=8790, debug=False)
