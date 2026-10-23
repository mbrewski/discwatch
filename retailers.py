"""Price lookups for boutique disc shops and big-box retailers."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.parse
from html import unescape
from typing import Callable

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

STOP = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with",
    "4k", "uhd", "ultra", "hd", "blu", "ray", "bluray", "bluray", "disc",
    "edition", "limited", "collector", "collectors", "steelbook", "steel",
    "book", "digital", "copy", "pre", "order", "preorder", "region",
    "slipcover", "standard", "special", "set", "collection", "from",
    "movie", "film", "plus", "combo",
}

FORMAT_4K = re.compile(r"\b(4k|uhd|ultra\s*hd)\b", re.I)
FORMAT_BD = re.compile(r"\bblu[\s-]*ray\b", re.I)

# Backstop against a parse that goes badly wrong. Generous on purpose: premium box sets
# do reach four figures, so this only catches clearly absurd values.
MAX_PLAUSIBLE_CENTS = 1_000_000  # $10,000

log = logging.getLogger("discwatch.retailers")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def _token_list(s: str) -> list[str]:
    s = s.lower()
    s = re.sub(r"[’']", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t and t not in STOP and len(t) > 1]


def _core_title(title: str) -> str:
    t = re.sub(r"^\s*PRE-ORDER:\s*", "", title, flags=re.I)
    t = re.split(r"\s*[(\[]", t, maxsplit=1)[0]
    t = re.split(r"\s+\d{4}\s*$", t)[0]
    t = re.split(r"\s+(4K|UHD|Ultra HD|Blu-ray|Blu ray)\b", t, maxsplit=1, flags=re.I)[0]
    return re.sub(r"\s+", " ", t).strip()


def _score(
    query_title: str,
    product_title: str,
    want_4k: bool,
    year: str | None = None,
) -> float:
    """Shop title must be the same film, not a different title that shares a word."""
    core_q = _core_title(query_title).lower()
    core_p = _core_title(product_title).lower()
    if not core_q or not core_p:
        return 0.0
    qseq = _token_list(core_q)
    pseq = _token_list(core_p)
    if not qseq or not pseq:
        return 0.0
    if qseq == pseq:
        score = 1.0
    elif pseq[: len(qseq)] == qseq:
        extra = pseq[len(qseq) :]
        if extra and len(qseq) <= 2:
            return 0.0
        score = 0.92 - 0.06 * min(len(extra), 5)
    else:
        return 0.0

    years_q = set(re.findall(r"\b(?:19|20)\d{2}\b", query_title))
    if year:
        years_q.add(str(year))
    years_p = set(re.findall(r"\b(?:19|20)\d{2}\b", product_title))
    if years_q and years_p and years_q.isdisjoint(years_p):
        score -= 0.4

    p4k = bool(FORMAT_4K.search(product_title))
    q4k = want_4k or bool(FORMAT_4K.search(query_title))
    if q4k and p4k:
        score += 0.08
    elif q4k and not p4k and FORMAT_BD.search(product_title):
        score -= 0.06
    return score


def _money_to_cents(v) -> int | None:
    """Every caller supplies dollars: Shopify sends a decimal string, Walmart a float
    plus string priceLines, the DDG parser a "$12.99". So never treat a bare number as
    cents - that read an int 1200 as $12.00 while a float 1200.0 became $1200.00."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        cents = int(round(float(v) * 100))
    else:
        s = str(v).replace(",", "").replace("$", "").strip()
        m = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", s)
        if not m:
            return None
        cents = int(round(float(m.group(1)) * 100))
    if cents <= 0 or cents > MAX_PLAUSIBLE_CENTS:
        return None
    return cents


def _best_shopify(
    products: list[dict],
    title: str,
    fmt: str,
    year: str | None = None,
    min_score: float = 0.7,
):
    want_4k = fmt == "4k"
    ranked = []
    for p in products:
        sc = _score(title, p.get("title") or "", want_4k, year=year)
        ranked.append((sc, p))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked or ranked[0][0] < min_score:
        return None, 0.0
    return ranked[0][1], ranked[0][0]


def shopify_suggest(session: requests.Session, origin: str, query: str) -> list[dict]:
    url = (
        f"{origin}/search/suggest.json?"
        + urllib.parse.urlencode({"q": query})
        + "&resources%5Btype%5D=product&resources%5Blimit%5D=10"
    )
    r = session.get(url, headers={"Accept": "application/json"}, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data.get("resources", {}).get("results", {}).get("products") or []


def lookup_shopify(
    session: requests.Session,
    retailer: str,
    origin: str,
    title: str,
    fmt: str,
    year: str | None = None,
) -> dict | None:
    q = _core_title(title)
    if fmt == "4k":
        q = f"{q} 4K"
    else:
        q = f"{q} Blu-ray"
    products = shopify_suggest(session, origin, q)
    if not products and fmt == "4k":
        products = shopify_suggest(session, origin, _core_title(title))
    hit, sc = _best_shopify(products, title, fmt, year=year)
    if not hit:
        return None
    cents = _money_to_cents(hit.get("price"))
    if cents is None:
        return None
    path = hit.get("url") or f"/products/{hit.get('handle')}"
    path = path.split("?")[0]
    note = "in stock" if hit.get("available") else "unavailable / sold out"
    if sc < 0.75:
        note += f" · match {sc:.2f}"
    return {
        "retailer": retailer,
        "price_cents": cents,
        "url": origin + path,
        "note": note,
        "product_title": hit.get("title"),
    }


def lookup_walmart(
    session: requests.Session, title: str, fmt: str, year: str | None = None
) -> dict | None:
    q = _core_title(title)
    q = f"{q} 4K Blu-ray" if fmt == "4k" else f"{q} Blu-ray"
    url = "https://www.walmart.com/search?" + urllib.parse.urlencode({"q": q})
    r = session.get(
        url,
        headers={
            "User-Agent": MOBILE_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=30,
    )
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text)
    if not m:
        return None
    data = json.loads(m.group(1))
    try:
        items = data["props"]["pageProps"]["initialData"]["searchResult"]["itemStacks"][0]["items"]
    except (KeyError, IndexError, TypeError):
        return None
    want_4k = fmt == "4k"
    ranked = []
    for it in items:
        name = it.get("name") or ""
        if not name:
            continue
        sc = _score(title, name, want_4k, year=year)
        ranked.append((sc, it))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked or ranked[0][0] < 0.7:
        return None
    it = ranked[0][1]
    cents = _money_to_cents(it.get("price"))
    if cents is None:
        pi = it.get("priceInfo") or {}
        lines = ((pi.get("priceDetails") or {}).get("priceLines") or [])
        for line in lines:
            for val in line.get("values") or []:
                if val.get("key") == "PRICE":
                    cents = _money_to_cents(val.get("value"))
                    break
    if cents is None:
        return None
    path = it.get("canonicalUrl") or f"/ip/{it.get('usItemId')}"
    path = path.split("?")[0]
    avail = it.get("availabilityStatus") or ""
    note = avail.replace("_", " ").title() if avail else "Walmart search"
    return {
        "retailer": "Walmart",
        "price_cents": cents,
        "url": "https://www.walmart.com" + path,
        "note": note,
        "product_title": it.get("name"),
    }


def lookup_diabolik(
    session: requests.Session, title: str, fmt: str, year: str | None = None
) -> dict | None:
    """Diabolik is behind a Cloudflare JS challenge; use DDG lite product hits."""
    q = _core_title(title)
    extra = "4K" if fmt == "4k" else "Blu-ray"
    query = f"site:diabolikdvd.com/product {q} {extra}"
    url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query})
    html = _curl_get(url)
    if "result-link" not in html:
        r = session.get(
            url,
            headers={"Accept": "text/html", "User-Agent": UA},
            timeout=25,
        )
        html = r.text
    products = _parse_ddg_lite_diabolik(html)
    if not products:
        return None
    want_4k = fmt == "4k"
    ranked = [(_score(title, p["title"], want_4k, year=year), p) for p in products]
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked or ranked[0][0] < 0.7:
        return None
    sc, hit = ranked[0]
    note = "DuckDuckGo listing"
    if sc < 0.75:
        note += f" · match {sc:.2f}"
    return {
        "retailer": "DiabolikDVD",
        "price_cents": hit["price_cents"],
        "url": hit["url"],
        "note": note,
        "product_title": hit["title"],
    }


def _curl_get(url: str) -> str:
    p = subprocess.run(
        [
            "curl", "-sL", "-A", UA, "-H", "Accept: text/html",
            "--compressed", "--max-time", "25", url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return p.stdout or ""


def _parse_ddg_lite_diabolik(html: str) -> list[dict]:
    items = []
    for m in re.finditer(
        r'<a[^>]+href="([^"]+)"[^>]*class=\'result-link\'[^>]*>(.*?)</a>'
        r'(.*?)<span class=\'link-text\'>([^<]+)</span>',
        html,
        re.S | re.I,
    ):
        href, title_html, blob, path = m.group(1), m.group(2), m.group(3), m.group(4)
        href = unescape(href)
        dest = ""
        if "uddg=" in href:
            parsed = urllib.parse.urlparse("https:" + href if href.startswith("//") else href)
            dest = unescape(urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0])
        if not dest:
            dest = "https://" + path.lstrip("/")
        dest = dest.split("&")[0]
        if "/product/" not in dest.lower():
            continue
        title = unescape(re.sub(r"<[^>]+>", " ", title_html))
        title = re.sub(r"\s+", " ", title)
        title = re.sub(r"\s*[–-]\s*DiabolikDVD\s*$", "", title).strip()
        pm = re.search(r"\$([0-9]+(?:\.[0-9]{2})?)", blob)
        cents = _money_to_cents(pm.group(0) if pm else None)
        if cents is None:
            continue
        items.append({"title": title, "url": dest, "price_cents": cents})
    return items


STORES: list[tuple[str, Callable]] = [
    ("GRUV", lambda s, t, f, y: lookup_shopify(s, "GRUV", "https://gruv.com", t, f, y)),
    (
        "Valoria Video",
        lambda s, t, f, y: lookup_shopify(s, "Valoria Video", "https://valoriavideo.com", t, f, y),
    ),
    (
        "Vinegar Syndrome",
        lambda s, t, f, y: lookup_shopify(
            s, "Vinegar Syndrome", "https://vinegarsyndrome.com", t, f, y
        ),
    ),
    ("Walmart", lookup_walmart),
    (
        "Orbit DVD",
        lambda s, t, f, y: lookup_shopify(s, "Orbit DVD", "https://www.orbitdvd.com", t, f, y),
    ),
    ("DiabolikDVD", lookup_diabolik),
]


def lookup_all(
    title: str, fmt: str = "4k", year: str | None = None, pause: float = 0.7
) -> list[dict]:
    session = _session()
    offers = []
    for name, fn in STORES:
        try:
            hit = fn(session, title, fmt, year)
        except Exception as e:
            log.warning("store %s failed for %r: %s", name, title, e)
            hit = None
        if hit:
            offers.append(hit)
        time.sleep(pause)
    return offers
