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
            db.preorder_subscribers(self.con), ["dliban2000@yahoo.com"]
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
        self.assertIn("discwatch.mattbrewer.me/inbox", body)

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


if __name__ == "__main__":
    unittest.main()
