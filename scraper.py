"""Fetch recent Blu-ray/4K titles and prices from blu-ray.com."""
from __future__ import annotations

import html as htmlmod
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote, urljoin

import requests

import retailers

BASE = "https://www.blu-ray.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
RSS_NEW = f"{BASE}/rss/newreleasesfeed.xml"
PAGE_4K = f"{BASE}/4k/"
PREORDERS = f"{BASE}/movies/movies.php?show=newpreorders"
SEARCH_4K = f"{BASE}/movies/search.php?action=search&ultrahd=1&sortby=releasetimestamp"

TITLE_RE = re.compile(
    r"https://www\.blu-ray\.com/movies/([^/\"\s]+)/(\d+)/", re.I
)
PRICE_RE = re.compile(r"\$([0-9]+(?:\.[0-9]{2})?)")

log = logging.getLogger("discwatch.scraper")


def fetch(url: str, timeout: int = 30) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "latin-1"
    text = r.text
    if len(text) < 200 or text.strip() in {"error42"}:
        raise RuntimeError(f"blu-ray.com blocked or empty response for {url}")
    return text


def cover_url(bluray_id: str, size: str = "medium") -> str:
    return f"https://images.static-bluray.com/movies/covers/{bluray_id}_{size}.jpg"


def classify(title: str, slug: str = "") -> str:
    blob = f"{title} {slug}".lower()
    if "4k" in blob or "ultra hd" in blob or "uhd" in blob:
        return "4k"
    return "bluray"


def slug_title(slug: str) -> str:
    s = re.sub(r"-Blu-ray$", "", slug, flags=re.I)
    s = s.replace("-", " ")
    return htmlmod.unescape(s)


def parse_money(s: str | None) -> int | None:
    if not s:
        return None
    m = PRICE_RE.search(s.replace(",", ""))
    if not m:
        return None
    return int(round(float(m.group(1)) * 100))


def parse_rss() -> list[dict]:
    raw = fetch(RSS_NEW)
    root = ET.fromstring(raw)
    items = []
    for item in root.findall("./channel/item"):
        link = (item.findtext("link") or "").strip()
        m = TITLE_RE.search(link)
        if not m:
            continue
        slug, bid = m.group(1), m.group(2)
        title = htmlmod.unescape((item.findtext("title") or "").strip())
        desc = htmlmod.unescape(item.findtext("description") or "")
        # Alliance Entertainment | 2025 | 292 mins | Rated TV-MA | Oct 20, 2026
        meta = re.search(
            r">([^<]+)</font>",
            desc.replace("&lt;", "<").replace("&gt;", ">"),
        )
        studio = year = runtime = rating = release = None
        if meta:
            parts = [p.strip() for p in meta.group(1).split("|")]
            if parts:
                studio = parts[0] or None
            if len(parts) > 1 and re.fullmatch(r"\d{4}(?:-\d{4})?", parts[1]):
                year = parts[1]
            if len(parts) > 2:
                runtime = parts[2]
            if len(parts) > 3:
                rating = parts[3]
            if len(parts) > 4:
                release = parts[4]
        amazon = None
        am = re.search(r'href="(https://www\.blu-ray\.com/link/click\.php[^"]+)"', desc.replace("&lt;", "<").replace("&gt;", ">"))
        if am:
            amazon = htmlmod.unescape(am.group(1))
        items.append(
            {
                "bluray_id": bid,
                "slug": slug,
                "title": title.replace(" (Blu-ray)", "").strip(),
                "url": link.split("?")[0],
                "cover": cover_url(bid),
                "fmt": classify(title, slug),
                "studio": studio,
                "year": year,
                "runtime": runtime,
                "rating": rating,
                "release_date": _norm_date(release),
                "amazon_click": amazon,
                "source": "rss",
            }
        )
    return items


def parse_preorder_tables(html: str, source: str) -> list[dict]:
    items = []
    # Title row + date cell
    row_re = re.compile(
        r'<a href="https://www\.blu-ray\.com/movies/([^"/]+)/(\d+)/">\s*'
        r"(.*?)</a>\s*(?:<font[^>]*>\(([^<]*)\)</font>)?\s*</td>\s*"
        r'<td[^>]*>\s*([A-Za-z]{3} \d{1,2}, \d{4})\s*</td>',
        re.S,
    )
    for slug, bid, title_html, year, date in row_re.findall(html):
        title = htmlmod.unescape(re.sub(r"<[^>]+>", " ", title_html))
        title = re.sub(r"\s+", " ", title).strip()
        items.append(
            {
                "bluray_id": bid,
                "slug": slug,
                "title": title,
                "url": f"{BASE}/movies/{slug}/{bid}/",
                "cover": cover_url(bid),
                "fmt": classify(title, slug),
                "studio": None,
                "year": year or None,
                "runtime": None,
                "rating": None,
                "release_date": _norm_date(date),
                "amazon_click": None,
                "source": source,
            }
        )
    return items


def parse_4k_hub() -> list[dict]:
    html = fetch(PAGE_4K)
    items = []
    i = html.find("<h2>Upcoming 4K Blu-ray Movies</h2>")
    j = html.find("<h2>New 4K Blu-ray Pre-orders</h2>")
    if i >= 0:
        chunk = html[i : j if j > i else i + 40000]
        # cover cards: title on img
        for m in re.finditer(
            r'href="https://www\.blu-ray\.com/movies/([^"/]+)/(\d+)/"[^>]*>'
            r'.*?title="([^"]+)"',
            chunk,
            re.S,
        ):
            slug, bid, title = m.group(1), m.group(2), htmlmod.unescape(m.group(3))
            items.append(
                {
                    "bluray_id": bid,
                    "slug": slug,
                    "title": title,
                    "url": f"{BASE}/movies/{slug}/{bid}/",
                    "cover": cover_url(bid),
                    "fmt": "4k",
                    "studio": None,
                    "year": None,
                    "runtime": None,
                    "rating": None,
                    "release_date": None,
                    "amazon_click": None,
                    "source": "4k-upcoming",
                }
            )
        items.extend(parse_preorder_tables(chunk, "4k-upcoming"))
    if j >= 0:
        items.extend(parse_preorder_tables(html[j : j + 25000], "4k-preorder"))
    for it in items:
        if it.get("source") == "4k-preorder":
            it["preorder"] = True
    return _dedupe(items)


def parse_new_preorders() -> list[dict]:
    """blu-ray.com 'New Blu-ray Pre-orders' cover grid (not the 4K hub tables)."""
    html = fetch(PREORDERS)
    items = []
    seen: set[str] = set()

    def add(slug: str, bid: str, title: str) -> None:
        if bid in seen:
            return
        seen.add(bid)
        title = htmlmod.unescape(title).replace(" (Blu-ray)", "").strip()
        items.append(
            {
                "bluray_id": bid,
                "slug": slug,
                "title": title,
                "url": f"{BASE}/movies/{slug}/{bid}/",
                "cover": cover_url(bid),
                "fmt": classify(title, slug),
                "studio": None,
                "year": None,
                "runtime": None,
                "rating": None,
                "release_date": None,
                "amazon_click": None,
                "source": "preorder",
                "preorder": True,
            }
        )

    for slug, bid, title in re.findall(
        r'href="https://www\.blu-ray\.com/movies/([^"/]+)/(\d+)/"[^>]*title="([^"]+)"',
        html,
    ):
        add(slug, bid, title)
    for slug, bid, title in re.findall(
        r"window\.location = 'https://www\.blu-ray\.com/movies/([^'/]+)/(\d+)/'"
        r'.*?<img class="cover"[^>]*title="([^"]+)"',
        html,
        re.S,
    ):
        add(slug, bid, title)
    return items


def parse_search_newest_4k() -> list[dict]:
    html = fetch(SEARCH_4K)
    items = []
    for slug, bid, title in re.findall(
        r'href="https://www\.blu-ray\.com/movies/([^"/]+)/(\d+)/"[^>]*title="([^"]+)"',
        html,
    ):
        items.append(
            {
                "bluray_id": bid,
                "slug": slug,
                "title": htmlmod.unescape(title),
                "url": f"{BASE}/movies/{slug}/{bid}/",
                "cover": cover_url(bid),
                "fmt": "4k",
                "studio": None,
                "year": None,
                "runtime": None,
                "rating": None,
                "release_date": None,
                "amazon_click": None,
                "source": "search-4k",
            }
        )
    return _dedupe(items)


def parse_title_page(url: str) -> dict:
    html = fetch(url)
    og_title = _meta(html, "og:title") or ""
    og_url = _meta(html, "og:url") or url
    og_image = _meta(html, "og:image") or ""
    m = TITLE_RE.search(og_url) or TITLE_RE.search(url)
    bid = m.group(2) if m else ""
    slug = m.group(1) if m else ""

    price_block = ""
    pm = re.search(
        r'<span class="subheading">Price</span>(.*?)<span class="subheading">',
        html,
        re.S,
    )
    if pm:
        price_block = pm.group(1)

    lm = re.search(r"List price:\s*<strike>(\$[0-9.,]+)</strike>", price_block, re.I)
    list_price = parse_money(lm.group(1) if lm else None)
    amazon_price = None
    amazon_url = None
    am = re.search(
        r'Amazon:\s*<a href="([^"]+)"[^>]*>\s*<b>(\$[0-9.,]+)</b>',
        price_block,
        re.I,
    )
    if am:
        amazon_url = htmlmod.unescape(am.group(1))
        amazon_price = parse_money(am.group(2))
    new_from = None
    nf = re.search(
        r'New from:\s*<a href="([^"]+)"[^>]*>\s*<b>(\$[0-9.,]+)</b>',
        price_block,
        re.I,
    )
    if nf:
        new_from = parse_money(nf.group(2))
        if not amazon_url:
            amazon_url = htmlmod.unescape(nf.group(1))

    release = None
    rm = re.search(
        r"This title will be released on ([A-Za-z]+ \d{1,2}, \d{4})", price_block
    )
    if rm:
        release = _norm_date(rm.group(1))
    else:
        rm = re.search(
            r'<span class="subheading">Release Date</span><br>\s*([^<]+)', html, re.I
        )
        if rm:
            release = _norm_date(rm.group(1).strip())

    studio = None
    sm = re.search(
        r'<span class="subheading">Studio(?:s)?</span><br>\s*(?:<[^>]+>)*([^<]+)',
        html,
        re.I,
    )
    if sm:
        studio = htmlmod.unescape(sm.group(1)).strip() or None

    offers = []
    if amazon_price is not None:
        offers.append(
            {
                "retailer": "Amazon",
                "price_cents": amazon_price,
                "url": amazon_url or og_url,
                "note": "blu-ray.com Amazon price",
            }
        )
    if new_from is not None and new_from != amazon_price:
        offers.append(
            {
                "retailer": "Amazon Marketplace (new)",
                "price_cents": new_from,
                "url": amazon_url or og_url,
                "note": "New from",
            }
        )
    if list_price is not None:
        offers.append(
            {
                "retailer": "List / MSRP",
                "price_cents": list_price,
                "url": og_url,
                "note": "List price",
            }
        )

    blob = price_block or ""
    preorder = bool(re.search(r"pre[-\s]?order", blob, re.I))

    best = None
    paid = [o for o in offers if o["retailer"] != "List / MSRP"]
    if paid:
        best = min(paid, key=lambda o: o["price_cents"])["price_cents"]

    return {
        "bluray_id": bid,
        "slug": slug,
        "title": htmlmod.unescape(og_title).replace(" Blu-ray", "").strip(),
        "url": og_url,
        "cover": og_image or cover_url(bid),
        "fmt": classify(og_title, slug),
        "studio": studio,
        "release_date": release,
        "list_price_cents": list_price,
        "best_price_cents": best,
        "amazon_url": amazon_url,
        "offers": offers,
        "preorder": preorder,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def gather_releases() -> list[dict]:
    out = []
    errors = []
    for fn in (parse_rss, parse_4k_hub, parse_search_newest_4k, parse_new_preorders):
        try:
            out.extend(fn())
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
            log.warning("release source %s failed: %s", fn.__name__, e)
    if not out and errors:
        raise RuntimeError("; ".join(errors))
    return _dedupe(out)


def search_catalog(query: str, limit: int = 40) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    url = (
        f"{BASE}/search/?quicksearch=1&quicksearch_keyword={quote(q)}"
        f"&section=bluraymovies"
    )
    html = fetch(url)
    items = []
    seen = set()
    for slug, bid, title in re.findall(
        r'href="https://www\.blu-ray\.com/movies/([^"/]+)/(\d+)/"[^>]*title="([^"]+)"',
        html,
    ):
        if bid in seen:
            continue
        seen.add(bid)
        title = htmlmod.unescape(title)
        year = None
        ym = re.search(r"\((\d{4}(?:-\d{4})?)\)\s*$", title)
        if ym:
            year = ym.group(1)
        items.append(
            {
                "bluray_id": bid,
                "slug": slug,
                "title": title,
                "url": f"{BASE}/movies/{slug}/{bid}/",
                "cover": cover_url(bid),
                "fmt": classify(title, slug),
                "studio": None,
                "year": year,
                "runtime": None,
                "rating": None,
                "release_date": None,
                "source": "search",
            }
        )
        if len(items) >= limit:
            break
    return items


def _dedupe(items: list[dict]) -> list[dict]:
    seen = {}
    for it in items:
        bid = it["bluray_id"]
        if bid not in seen:
            seen[bid] = it
            continue
        cur = seen[bid]
        for k, v in it.items():
            if v and not cur.get(k):
                cur[k] = v
        if it.get("preorder"):
            cur["preorder"] = True
        if it.get("source") in ("4k-preorder", "preorder"):
            cur["source"] = it["source"]
            cur["preorder"] = True
        if it.get("source") == "rss":
            # RSS descriptions are richer
            for k in ("studio", "year", "runtime", "rating", "release_date"):
                if it.get(k):
                    cur[k] = it[k]
    return list(seen.values())


def _meta(html: str, prop: str) -> str | None:
    m = re.search(
        rf'<meta property="{re.escape(prop)}" content="([^"]*)"', html, re.I
    )
    return htmlmod.unescape(m.group(1)) if m else None


def _norm_date(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return s


def is_bluray_title_url(url: str | None) -> bool:
    return bool(url and TITLE_RE.search(url))


def canonical_title_url(url: str | None) -> str | None:
    """Rebuild a blu-ray.com title URL from the slug and id inside `url`.

    TITLE_RE only *searches*, so a hostile value like
    http://127.0.0.1/?x=https://www.blu-ray.com/movies/a/1/ matches. Returning a URL
    built from BASE means callers never fetch the host they were handed.
    """
    m = TITLE_RE.search(url or "")
    if not m:
        return None
    return f"{BASE}/movies/{m.group(1)}/{m.group(2)}/"


def prices_for_title(title: str, fmt: str = "4k", year: str | None = None) -> dict:
    offers = retailers.lookup_all(title, fmt or "4k", year=year)
    paid = [o for o in offers if o.get("retailer") != "List / MSRP" and o.get("price_cents")]
    best = min(o["price_cents"] for o in paid) if paid else None
    return {
        "title": title,
        "fmt": fmt,
        "offers": offers,
        "list_price_cents": None,
        "best_price_cents": best,
        "amazon_url": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def merge_retailer_offers(data: dict) -> dict:
    extra = retailers.lookup_all(
        data.get("title") or "", data.get("fmt") or "4k", year=data.get("year")
    )
    seen = {(o.get("retailer"), o.get("price_cents")) for o in data.get("offers") or []}
    offers = list(data.get("offers") or [])
    for o in extra:
        key = (o.get("retailer"), o.get("price_cents"))
        if key in seen:
            continue
        offers.append(o)
        seen.add(key)
    data["offers"] = offers
    paid = [o for o in offers if o.get("retailer") != "List / MSRP" and o.get("price_cents")]
    if paid:
        data["best_price_cents"] = min(o["price_cents"] for o in paid)
    return data


def polite_parse_title(url: str, pause: float = 1.6) -> dict:
    data = parse_title_page(url)
    time.sleep(pause)
    return merge_retailer_offers(data)
