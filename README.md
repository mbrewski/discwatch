# Discwatch

Source: https://github.com/mbrewski/discwatch (private).

Local 4K / Blu-ray price tracker. It pulls recent discs from [blu-ray.com](https://www.blu-ray.com), lets you track catalog titles or movies with no disc page yet, and checks where they are listed and for how much.

**Live:** https://discwatch.mattbrewer.me (public, no Cloudflare Access)  
**LAN:** http://127.0.0.1:8790 or http://10.145.22.82:8790

## Run

User systemd (preferred):

```bash
systemctl --user enable --now discwatch.service discwatch-releases.timer
systemctl --user status discwatch.service discwatch-releases.timer
```

`discwatch.service` binds `0.0.0.0:8790`. `discwatch-prices.timer` refreshes shop prices every 12 hours (admins can still **Refresh prices now** on Users). After code changes:

```bash
systemctl --user restart discwatch.service
```

The timer pulls the blu-ray.com **release list** every 12 hours (`OnUnitActiveSec=12h`). It does not refresh prices. Pull now:

```bash
systemctl --user start discwatch-releases.service
```

Ad-hoc: `./run.sh` (venv Flask). Port **8787** is funding-desk, not this app.

## Use

First visit opens **Create admin**. **Sign up** on the login page creates a provisional account; an admin **Approves** or **Denies** it on **Users**. Only approved accounts can log in. Each login has its own tracking list; catalog and prices are shared. Existing titles you already tracked move onto the first admin’s list.

- **New releases** — inbox from RSS + 4K hub + newest UHD search. Filter 4K vs Blu-ray, Track.
- **Find title** — search the blu-ray.com catalog, or paste a title-page URL, then Track. Use **Track by name** when there is no blu-ray.com page (pre-release, not listed, or not on disc yet).
- **Tracked** — best/list price, shops, target price, Refresh prices. A target hit only highlights the price (`hit`); there is no email or n8n ping.
- **Users** (admin) — add username + password, optional admin. SQLite is enough for this (one host, a handful of accounts). Postgres is not required.

Logins use Flask sessions (`data/secret_key`). Passwords are hashed (pbkdf2). The site is still on the public internet; anyone can hit `/setup` until the first admin exists, then they need an account.

Name-only titles skip blu-ray.com and still query the other shops. Covers come from blu-ray.com when a page exists; otherwise a placeholder.

## Price sources

| Source | How |
| --- | --- |
| Amazon / list / marketplace | blu-ray.com title “Price” box (when a movie URL exists) |
| GRUV, Valoria Video, Vinegar Syndrome, Orbit DVD | Shopify `search/suggest.json` |
| Walmart | mobile search HTML `__NEXT_DATA__` |
| DiabolikDVD | DuckDuckGo Lite snippets (`site:diabolikdvd.com/product …`); the shop itself is Cloudflare-challenged |

Matching requires the shop title to **start with the same name** (short titles cannot match as a word inside a longer title). Example: *Hope* must not match *Hillsong: Let Hope Rise*. Extra tokens after a 1–2 word query are rejected. Year mismatch is penalized. A miss is omitted.

Best price ignores List/MSRP.

## Layout

```
app.py                 Flask UI and track/add/refresh
scraper.py             blu-ray.com RSS, hub, catalog search, title pages
retailers.py           GRUV / Valoria / VS / Orbit / Walmart / Diabolik
db.py                  SQLite (data/discwatch.sqlite)
refresh_releases.py    12h list pull
templates/             tracked, inbox, find, title
systemd/               user units (symlinked from ~/.config/systemd/user/)
```

Flask sits behind Cloudflare Tunnel with `ProxyFix`, `PREFERRED_URL_SCHEME=https`, and `SESSION_COOKIE_SECURE`.

## Limits

- blu-ray.com has no official API. Requests need a browser User-Agent, `Accept`/`Accept-Language`, and gzip or you get `error42`. Sleep ~1.6s between title pages; only tracked titles are priced. HTML can change.
- Title-page prices are mostly Amazon. Other shops are separate lookups.
- Diabolik is flaky when DuckDuckGo returns 202.
- Valoria Shopify handles are often leftover “copy” slugs; trust title and price.
- The public hostname has no Cloudflare Access. After setup, unauthenticated visitors only see the login page.
- Tunnel origin is `http://10.145.22.82:8790` on Brewski (`httpHostHeader` discwatch.mattbrewer.me). DNS CNAME id `fbe25ca33b2ad01450ca7c1ce262223c`. Do not re-create the deleted Access app; leave shared IP policies on other home apps.

## Dependencies

Python 3 venv: `flask`, `requests` (`requirements.txt`).
