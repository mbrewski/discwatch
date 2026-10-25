# Discwatch

Source: https://github.com/mbrewski/discwatch (private).

Local 4K / Blu-ray price tracker. It pulls recent discs from [blu-ray.com](https://www.blu-ray.com), lets you track catalog titles or movies with no disc page yet, and checks where they are listed and for how much.

**Live:** https://discwatch.mattbrewer.me (public, no Cloudflare Access)  
**LAN:** http://127.0.0.1:8790 or http://10.145.22.82:8790

## Run

User systemd (preferred):

```bash
systemctl --user enable --now discwatch.service discwatch-releases.timer discwatch-prices.timer
systemctl --user status discwatch.service discwatch-releases.timer discwatch-prices.timer
```

`discwatch.service` binds `0.0.0.0:8790`. Two timers run every 12 hours:

- `discwatch-releases.timer` — blu-ray.com release list (RSS, 4K hub, newest UHD search, **new pre-orders**). New pre-orders trigger email.
- `discwatch-prices.timer` — shop prices for watched titles. Admins can still **Refresh prices now** on Users.

After code changes:

```bash
systemctl --user daemon-reload
systemctl --user restart discwatch.service
```

Pull the list now: `systemctl --user start discwatch-releases.service`

Ad-hoc: `./run.sh` (venv Flask). Port **8787** is funding-desk, not this app.

## Tests

Smoke tests in `tests/test_smoke.py` use a temp sqlite file, fixture HTML, and mocked SMTP. They do not hit blu-ray.com or send mail.

```bash
./venv/bin/python -m unittest discover -s tests -v
```

Covers pre-order upsert (alert once), price-page transition, subscriber flags, pre-order parsers, digest/CC SMTP, release-pull alerting, Flask setup/login/Users alerts, inbox pill, mocked test-mail, and non-admin 403.

## Use

First visit opens **Create admin**. **Sign up** on the login page creates a provisional account; an admin **Approves** or **Denies** it on **Users**. Only approved accounts can log in. Each login has its own tracking list; catalog and prices are shared. Existing titles you already tracked move onto the first admin’s list.

- **New releases** — inbox from RSS + 4K hub + newest UHD search + blu-ray.com **New Pre-orders**. Filter 4K vs Blu-ray, Track. A **Pre-order** pill marks titles that are on a pre-order list.
- **Find title** — search the blu-ray.com catalog, or paste a title-page URL, then Track. Use **Track by name** when there is no blu-ray.com page (pre-release, not listed, or not on disc yet).
- **Tracked** — best/list price, shops, target price, Refresh prices, share with another user. A target hit only highlights the price (`hit`); there is no email for price hits. Pre-order discs show the same pill.
- **Users** (admin) — add username + password, optional admin. Set an email and **Pre-order alerts** per account. **Send test email** checks SMTP. SQLite is enough for this (one host, a handful of accounts). Postgres is not required.

### Pre-order email

When a title is **newly** marked pre-order (first time on the new-preorders page, the 4K hub pre-order table, or a title page that says pre-order), Discwatch emails every approved user with an address and **Pre-order alerts** on. Titles already flagged in the DB are not re-sent. The 12h price job can also fire if a watched title page newly says pre-order.

SMTP is `smtp.mail.me.com:587` (STARTTLS). `data/smtp.env` (gitignored) holds:

```
SMTP_USER=mbrewski@me.com
SMTP_FROM=mbrewski@me.com
```

The app-specific password is read from `~/secrets/apple.env` (`PASSWORD`), then Vault `secret/apple`. Units load `EnvironmentFile=-%h/develop/discwatch/data/smtp.env`. Do not put the password in git.

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
scraper.py             blu-ray.com RSS, hub, new pre-orders, catalog search, title pages
retailers.py           GRUV / Valoria / VS / Orbit / Walmart / Diabolik
db.py                  SQLite (data/discwatch.sqlite); titles.preorder, users.email
mail.py                iCloud SMTP + pre-order digest
refresh_releases.py    12h list pull; emails new pre-orders
refresh_prices.py      12h shop prices; emails if a title page newly says pre-order
templates/             tracked, inbox, find, title, users
tests/                 unittest smoke suite
systemd/               user units (symlinked from ~/.config/systemd/user/)
```

Flask sits behind Cloudflare Tunnel with `ProxyFix`, `PREFERRED_URL_SCHEME=https`, and `SESSION_COOKIE_SECURE`.

## Limits

- blu-ray.com has no official API. Requests need a browser User-Agent, `Accept`/`Accept-Language`, and gzip or you get `error42`. Sleep ~1.6s between title pages; only tracked titles are priced. HTML can change.
- Title-page prices are mostly Amazon. Other shops are separate lookups.
- Diabolik is flaky when DuckDuckGo returns 202.
- Valoria Shopify handles are often leftover “copy” slugs; trust title and price.
- The public hostname has no Cloudflare Access. After setup, unauthenticated visitors only see the login page.
- iCloud SMTP is for a handful of household alerts. Apple can revoke the app-specific password.
- Tunnel origin is `http://10.145.22.82:8790` on Brewski (`httpHostHeader` discwatch.mattbrewer.me). DNS CNAME id `fbe25ca33b2ad01450ca7c1ce262223c`. Do not re-create the deleted Access app; leave shared IP policies on other home apps.

## Dependencies

Python 3 venv: `flask`, `requests` (`requirements.txt`).
