"""Smoke tests: temp sqlite, no live blu-ray.com, no real SMTP."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import mail
import scraper
from refresh_releases import pull_releases

HUB_ROW = """
<td><a href="https://www.blu-ray.com/movies/Dune-4K-Blu-ray/111/">
 Dune 4K</a> <font>(2021)</font></td>
<td align="right"> Oct 1, 2026 </td>
"""

GRID_HTML = """
<a href="https://www.blu-ray.com/movies/Indera-Blu-ray/423633/" title="Indera (2024)">x</a>
<script>window.location = 'https://www.blu-ray.com/movies/Signs-4K-Blu-ray/369232/';</script>
<img class="cover" title="Signs 4K (Blu-ray)">
"""


def _item(**kw):
    base = {
        "bluray_id": "111",
        "slug": "Dune-4K-Blu-ray",
        "title": "Dune 4K",
        "url": "https://www.blu-ray.com/movies/Dune-4K-Blu-ray/111/",
        "cover": None,
        "fmt": "4k",
        "studio": None,
        "year": "2021",
        "runtime": None,
        "rating": None,
        "release_date": "2026-10-01",
        "source": "rss",
        "preorder": False,
    }
    base.update(kw)
    return base


class TempDb(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "t.sqlite"
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        db.DB_PATH = self._orig
        self._tmp.cleanup()


class TestShareDedupe(TempDb):
    def test_share_reuses_same_film_different_id(self):
        db.upsert_release(
            self.con,
            _item(
                bluray_id="m-old",
                title="Never after dark",
                fmt="bluray",
                source="manual-name",
            ),
        )
        db.upsert_release(
            self.con,
            _item(
                bluray_id="m-new",
                title="Never After Dark",
                fmt="bluray",
                year="2026",
                source="manual-name",
            ),
        )
        db.upsert_release(
            self.con,
            _item(
                bluray_id="m-4k",
                title="Never After Dark",
                fmt="4k",
                year="2026",
                source="manual-name",
            ),
        )
        db.set_watch(self.con, 1, "m-old", True)
        db.set_watch(self.con, 1, "m-new", True, shared_by=2)
        rows = self.con.execute(
            "SELECT bluray_id, shared_by FROM watches WHERE user_id=1 ORDER BY bluray_id"
        ).fetchall()
        self.assertEqual([r["bluray_id"] for r in rows], ["m-old"])
        self.assertEqual(rows[0]["shared_by"], 2)
        db.set_watch(self.con, 1, "m-4k", True, shared_by=2)
        ids = [
            r["bluray_id"]
            for r in self.con.execute(
                "SELECT bluray_id FROM watches WHERE user_id=1 ORDER BY bluray_id"
            )
        ]
        self.assertEqual(ids, ["m-4k", "m-old"])

    def test_title_watch_key_strips_year(self):
        self.assertEqual(
            db.title_watch_key("Never After Dark (2026)", "bluray"),
            db.title_watch_key("never after dark", "Blu-ray"),
        )


class TestPreorderDb(TempDb):
    def test_upsert_marks_new_preorder_once(self):
        first = db.upsert_release(self.con, _item())
        self.assertFalse(first)
        became = db.upsert_release(
            self.con, _item(source="preorder", preorder=True)
        )
        self.assertTrue(became)
        again = db.upsert_release(
            self.con, _item(source="preorder", preorder=True)
        )
        self.assertFalse(again)
        row = self.con.execute(
            "SELECT preorder FROM titles WHERE bluray_id='111'"
        ).fetchone()
        self.assertEqual(row["preorder"], 1)

    def test_apply_prices_preorder_transition(self):
        db.upsert_release(self.con, _item())
        data = {
            "bluray_id": "111",
            "title": "Dune 4K",
            "preorder": True,
            "offers": [],
            "list_price_cents": None,
            "best_price_cents": None,
            "amazon_url": None,
        }
        self.assertTrue(db.apply_prices(self.con, data))
        self.assertFalse(db.apply_prices(self.con, data))

    def test_subscribers_need_email_and_flag(self):
        uid = db.create_user(self.con, "david", "hash", False)
        db.create_user(self.con, "quiet", "hash", False)
        db.set_user_alerts(self.con, uid, "dliban2000@yahoo.com", True)
        self.assertEqual(
            [r["email"] for r in db.preorder_subscribers(self.con)],
            ["dliban2000@yahoo.com"],
        )


class TestScraperFixtures(unittest.TestCase):
    def test_preorder_table_rows(self):
        items = scraper.parse_preorder_tables(HUB_ROW, "4k-preorder")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["bluray_id"], "111")
        self.assertEqual(items[0]["release_date"], "2026-10-01")
        self.assertEqual(items[0]["fmt"], "4k")

    def test_new_preorders_grid(self):
        with mock.patch.object(scraper, "fetch", return_value=GRID_HTML):
            items = scraper.parse_new_preorders()
        ids = {i["bluray_id"] for i in items}
        self.assertEqual(ids, {"423633", "369232"})
        self.assertTrue(all(i.get("preorder") for i in items))
        self.assertTrue(all(i.get("source") == "preorder" for i in items))

    def test_dedupe_promotes_preorder(self):
        merged = scraper._dedupe(
            [
                _item(source="rss", preorder=False),
                _item(source="preorder", preorder=True, studio="WB"),
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["preorder"])
        self.assertEqual(merged[0]["source"], "preorder")


class TestMailDigest(unittest.TestCase):
    def test_digest_copy(self):
        subj, body = mail.format_preorder_digest(
            [_item(title="Dune 4K", fmt="4k", url="https://example/dune")]
        )
        self.assertEqual(subj, "Discwatch: 1 new pre-order")
        self.assertIn("Dune 4K (4K, 2026-10-01)", body)
        self.assertIn("https://example/dune", body)
        self.assertIn("discwatch.mattbrewer.me/", body)
        self.assertIn("tracked list", body)

    def test_notify_skips_empty(self):
        self.assertEqual(mail.notify_preorders([], [_item()]), 0)
        self.assertEqual(mail.notify_preorders(["a@b.c"], []), 0)

    def test_notify_sends_one_mail_per_address_without_cc(self):
        smtp = mock.MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        cfg = {
            "host": "smtp.mail.me.com",
            "port": 587,
            "user": "mbrewski@me.com",
            "password": "xxxx-xxxx-xxxx-xxxx",
            "from_addr": "mbrewski@me.com",
        }
        with (
            mock.patch.object(mail, "smtp_settings", return_value=cfg),
            mock.patch("smtplib.SMTP", return_value=smtp) as ctor,
        ):
            mail.notify_preorders(
                ["dliban2000@yahoo.com", "matt@mattbrewer.me"],
                [_item()],
            )
        self.assertEqual(ctor.call_count, 2)
        smtp.login.assert_called_with("mbrewski@me.com", "xxxx-xxxx-xxxx-xxxx")
        tos = [c[0][0]["To"] for c in smtp.send_message.call_args_list]
        ccs = [c[0][0]["Cc"] for c in smtp.send_message.call_args_list]
        self.assertEqual(tos, ["dliban2000@yahoo.com", "matt@mattbrewer.me"])
        self.assertEqual(ccs, [None, None])
        self.assertEqual(smtp.send_message.call_args_list[0][0][0]["From"], "mbrewski@me.com")


class TestPullReleasesAlert(TempDb):
    def test_emails_only_new_preorders(self):
        db.upsert_release(self.con, _item(source="preorder", preorder=True))
        uid = db.create_user(self.con, "david", "hash", False)
        db.set_user_alerts(self.con, uid, "d@x.com", True)
        db.upsert_release(
            self.con,
            _item(
                bluray_id="222",
                slug="New-4K-Blu-ray",
                title="Brand New",
                url="https://www.blu-ray.com/movies/New-4K-Blu-ray/222/",
            ),
        )
        db.set_watch(self.con, uid, "222", True)
        self.con.commit()
        self.con.close()
        fresh = _item(
            bluray_id="222",
            slug="New-4K-Blu-ray",
            title="Brand New",
            url="https://www.blu-ray.com/movies/New-4K-Blu-ray/222/",
            source="preorder",
            preorder=True,
        )
        already = _item(source="preorder", preorder=True)
        with (
            mock.patch.object(
                scraper, "gather_releases", return_value=[already, fresh]
            ),
            mock.patch.object(mail, "notify_preorders", return_value=1) as notify,
        ):
            n = pull_releases()
        self.assertEqual(n, 2)
        notify.assert_called_once()
        addrs, items = notify.call_args[0]
        self.assertEqual(addrs, ["d@x.com"])
        self.assertEqual([i["bluray_id"] for i in items], ["222"])
        self.con = db.connect()

    def test_skips_untracked_new_preorders(self):
        uid = db.create_user(self.con, "david", "hash", False)
        db.set_user_alerts(self.con, uid, "d@x.com", True)
        self.con.commit()
        self.con.close()
        fresh = _item(source="preorder", preorder=True)
        with (
            mock.patch.object(scraper, "gather_releases", return_value=[fresh]),
            mock.patch.object(mail, "notify_preorders", return_value=1) as notify,
        ):
            pull_releases()
        notify.assert_not_called()
        self.con = db.connect()

    def test_per_user_tracked_subset(self):
        db.upsert_release(self.con, _item())
        db.upsert_release(
            self.con,
            _item(
                bluray_id="222",
                slug="New-4K-Blu-ray",
                title="Brand New",
                url="https://www.blu-ray.com/movies/New-4K-Blu-ray/222/",
            ),
        )
        david = db.create_user(self.con, "david", "hash", False)
        matt = db.create_user(self.con, "matt", "hash", False)
        db.set_user_alerts(self.con, david, "d@x.com", True)
        db.set_user_alerts(self.con, matt, "m@x.com", True)
        db.set_watch(self.con, david, "111", True)
        db.set_watch(self.con, matt, "222", True)
        self.con.commit()
        self.con.close()
        a = _item(source="preorder", preorder=True)
        b = _item(
            bluray_id="222",
            slug="New-4K-Blu-ray",
            title="Brand New",
            url="https://www.blu-ray.com/movies/New-4K-Blu-ray/222/",
            source="preorder",
            preorder=True,
        )
        with (
            mock.patch.object(scraper, "gather_releases", return_value=[a, b]),
            mock.patch.object(mail, "notify_preorders", return_value=1) as notify,
        ):
            pull_releases()
        calls = {c[0][0][0]: [i["bluray_id"] for i in c[0][1]] for c in notify.call_args_list}
        self.assertEqual(calls["d@x.com"], ["111"])
        self.assertEqual(calls["m@x.com"], ["222"])
        self.con = db.connect()

    def test_watched_among_matches_film_format(self):
        uid = db.create_user(self.con, "david", "hash", False)
        db.upsert_release(self.con, _item())
        db.set_watch(self.con, uid, "111", True)
        alias = _item(
            bluray_id="999",
            title="Dune 4K (2021)",
            source="preorder",
            preorder=True,
        )
        db.upsert_release(self.con, alias)
        mine = db.watched_among(self.con, uid, [alias])
        self.assertEqual([i["bluray_id"] for i in mine], ["999"])


class TestFlaskSmoke(TempDb):
    def setUp(self):
        super().setUp()
        import app as appmod

        self.appmod = appmod
        self.appmod._login_fails.clear()
        self.client = appmod.app.test_client()
        r = self.client.post(
            "/setup",
            data={"username": "mbrewski", "password": "password1"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)

    def test_login_and_users_alerts(self):
        r = self.client.post(
            "/users",
            data={"username": "david", "password": "password1"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        david = db.get_user_by_name(self.con, "david")
        r = self.client.post(
            f"/users/{david['id']}/alerts",
            data={
                "email": "dliban2000@yahoo.com",
                "notify_preorder": "1",
            },
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Pre-order alerts", r.data)
        self.assertIn(b"dliban2000@yahoo.com", r.data)
        self.con.close()
        self.con = db.connect()
        david = db.get_user_by_name(self.con, "david")
        self.assertEqual(david["email"], "dliban2000@yahoo.com")
        self.assertEqual(david["notify_preorder"], 1)

    def test_inbox_preorder_pill(self):
        db.upsert_release(
            self.con, _item(source="preorder", preorder=True, inbox_hidden=0)
        )
        self.con.commit()
        r = self.client.get("/inbox")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Pre-order", r.data)
        self.assertIn(b"Dune 4K", r.data)

    def test_test_mail_mocked(self):
        with mock.patch.object(self.appmod.mail, "send_mail") as send:
            r = self.client.post(
                "/users/test-mail",
                data={"email": "matt@mattbrewer.me"},
                follow_redirects=True,
            )
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Test mail sent", r.data)
        send.assert_called_once()
        self.assertEqual(send.call_args[0][0], ["matt@mattbrewer.me"])

    def test_non_admin_cannot_users(self):
        db.create_user(self.con, "david", "x" * 20, False, status="approved")
        # werkzeug hash for login
        from werkzeug.security import generate_password_hash

        self.con.execute(
            "UPDATE users SET password_hash=? WHERE username='david'",
            (generate_password_hash("password1"),),
        )
        self.con.commit()
        self.client.get("/logout")
        r = self.client.post(
            "/login",
            data={"username": "david", "password": "password1"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        r = self.client.get("/users")
        self.assertEqual(r.status_code, 403)


ARROW_SEARCH_HTML = """
<div data-testid="product-grid">
  <div class="product-card" data-testid="product-item">
    <a href="/p/donnie-darko-blu-ray/12946757/">x</a>
    <a href="/p/donnie-darko-blu-ray/12946757/" data-testid="product-title">Donnie Darko Blu-ray</a>
    <span class="sr-only">Current price:  $17.00.</span>
  </div>
  <div class="product-card" data-testid="product-item">
    <a href="/p/dvd/donnie-darko/13175323/">x</a>
    <a href="/p/dvd/donnie-darko/13175323/" data-testid="product-title">Donnie Darko</a>
    <span class="sr-only">Current price:  $20.00.</span>
  </div>
  <div class="product-card" data-testid="product-item">
    <a href="/p/donnie-darko-limited-edition-4k-uhd/12857875/">x</a>
    <a href="/p/donnie-darko-limited-edition-4k-uhd/12857875/" data-testid="product-title">Donnie Darko Limited Edition 4K UHD</a>
    <span class="sr-only">Current price:  $45.00.</span>
  </div>
</div>
"""


class TestShopSearch(unittest.TestCase):
    def test_search_shops_maps_lookup(self):
        import retailers

        hit = {
            "retailer": "Arrow Video",
            "price_cents": 3500,
            "url": "https://www.arrowvideo.com/p/4k/bullitt-limited-edition-4k-uhd/18077895/",
            "note": "Arrow Video US",
            "product_title": "Bullitt Limited Edition 4K UHD",
        }
        with mock.patch.object(retailers, "lookup_all", return_value=[hit]) as lookup:
            rows = retailers.search_shops("Bullitt 4K", year="1968")
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args[0][1], "4k")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["retailer"], "Arrow Video")
        self.assertEqual(rows[0]["fmt"], "4k")
        self.assertEqual(rows[0]["track_title"], "Bullitt Limited Edition")
        self.assertEqual(rows[0]["year"], "1968")


class TestFindShopFallback(TempDb):
    def setUp(self):
        super().setUp()
        import app as appmod

        self.appmod = appmod
        self.appmod._login_fails.clear()
        self.client = appmod.app.test_client()
        r = self.client.post(
            "/setup",
            data={"username": "mbrewski", "password": "password1"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)

    def test_shops_when_catalog_empty(self):
        shop = [
            {
                "retailer": "Arrow Video",
                "title": "Bullitt Limited Edition 4K UHD",
                "track_title": "Bullitt",
                "url": "https://www.arrowvideo.com/p/4k/bullitt/1/",
                "price_cents": 3500,
                "fmt": "4k",
                "year": "1968",
                "note": None,
                "source": "shop",
            }
        ]
        with mock.patch.object(scraper, "search_catalog", return_value=[]), mock.patch(
            "retailers.search_shops", return_value=shop
        ) as shops:
            r = self.client.get("/find?q=Bullitt")
        self.assertEqual(r.status_code, 200)
        shops.assert_called_once()
        self.assertIn(b"Arrow Video", r.data)
        self.assertIn(b"$35.00", r.data)
        self.assertIn(b"Track by name", r.data)
        self.assertIn(b"Shop listings", r.data)

    def test_skips_shops_when_catalog_hits(self):
        rows = [
            {
                "bluray_id": "1",
                "slug": "Bullitt-4K-Blu-ray",
                "title": "Bullitt 4K",
                "url": "https://www.blu-ray.com/movies/Bullitt-4K-Blu-ray/1/",
                "cover": "/static/placeholder.svg",
                "fmt": "4k",
                "year": "1968",
            }
        ]
        with mock.patch.object(scraper, "search_catalog", return_value=rows), mock.patch(
            "retailers.search_shops"
        ) as shops:
            r = self.client.get("/find?q=Bullitt")
        self.assertEqual(r.status_code, 200)
        shops.assert_not_called()
        self.assertIn(b"Bullitt 4K", r.data)
        self.assertNotIn(b"Shop listings", r.data)


class TestArrowSearch(unittest.TestCase):
    def test_parse_and_prefer_4k(self):
        import retailers

        items = retailers._parse_arrow_search(ARROW_SEARCH_HTML)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["price_cents"], 1700)

        class FakeResp:
            text = ARROW_SEARCH_HTML
            def raise_for_status(self):
                return None

        class FakeSess:
            def get(self, url, headers=None, timeout=None):
                self.url = url
                return FakeResp()

        sess = FakeSess()
        hit = retailers.lookup_arrow(sess, "Donnie Darko", "4k", year="2001")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["retailer"], "Arrow Video")
        self.assertEqual(hit["price_cents"], 4500)
        self.assertIn("4k-uhd", hit["url"])
        self.assertIn("q=Donnie+Darko+4K", sess.url)


if __name__ == "__main__":
    unittest.main()

