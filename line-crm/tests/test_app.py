import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["LINE_CHANNEL_SECRET"] = "test-secret"
os.environ["DASHBOARD_PASSWORD"] = "pw"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

import app as crm  # noqa: E402
import classifier as clf  # noqa: E402




def sign(body):
    return base64.b64encode(hmac.new(b"test-secret", body, hashlib.sha256).digest()).decode()


def text_event(uid, text, eid, ts=1_790_000_000_000):
    return {"type": "message", "webhookEventId": eid, "timestamp": ts,
            "source": {"type": "user", "userId": uid}, "message": {"type": "text", "text": text}}


class ClassifierTest(unittest.TestCase):
    def check(self, text, status):
        self.assertEqual(clf.classify_text(text)[0], status, text)

    def test_cases(self):
        self.check("昨天打完玻尿酸，今天還是很腫正常嗎", clf.URGENT)
        self.check("皮秒一次多少錢？", clf.REPLY)
        self.check("想預約下週六", clf.REPLY)
        self.check("我再考慮一下", clf.FOLLOW)
        self.check("我回去問一下老公再說", clf.FOLLOW)
        self.check("我再考慮看看，那音波價格是多少", clf.REPLY)
        self.check("好的謝謝～", clf.DONE)
        self.check("收到 👍", clf.DONE)

    def test_tags(self):
        _, _, tags = clf.classify_text("想問音波多少錢")
        self.assertIn("音波", tags)
        self.assertIn("詢價", tags)

    def test_merge_never_downgrades(self):
        self.assertEqual(clf.merge_status(clf.URGENT, clf.DONE), clf.URGENT)
        self.assertEqual(clf.merge_status(clf.FOLLOW, clf.REPLY), clf.REPLY)


class WebhookTest(unittest.TestCase):
    def setUp(self):
        self.client = crm.app.test_client()
        self.staff = crm.app.test_client()
        self.staff.post("/login", data={"username": "admin", "password": "pw"})

    def post(self, events):
        body = json.dumps({"events": events}).encode()
        return self.client.post("/callback", data=body, headers={"X-Line-Signature": sign(body)})

    def status_of(self, uid):
        with crm.app.app_context():
            return crm.get_db().execute("SELECT status FROM customers WHERE user_id=?", (uid,)).fetchone()[0]

    def test_bad_signature_rejected(self):
        r = self.client.post("/callback", data=b"{}", headers={"X-Line-Signature": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_flow(self):
        self.assertEqual(self.post([text_event("U1", "打完肉毒頭暈怎麼辦", "e1")]).status_code, 200)
        self.post([text_event("U1", "謝謝", "e2")])
        self.assertEqual(self.status_of("U1"), clf.URGENT)  # 謝謝不會蓋掉緊急
        self.post([text_event("U1", "謝謝", "e2")])          # LINE 重送同一事件
        with crm.app.app_context():
            n = crm.get_db().execute("SELECT COUNT(*) FROM messages WHERE user_id='U1'").fetchone()[0]
        self.assertEqual(n, 2)

        self.assertEqual(self.client.get("/").status_code, 302)  # 未登入 → 登入頁
        page = self.staff.get("/").get_data(as_text=True)
        self.assertIn("打完肉毒頭暈怎麼辦", page)  # 顯示關鍵訊息，不只最後一句

        self.staff.post("/customer/U1/update", data={"action": "follow", "follow_up_at": "2026-10-01"})
        self.assertEqual(self.status_of("U1"), clf.FOLLOW)
        self.staff.post("/customer/U1/update", data={"action": "replied"})
        self.assertEqual(self.status_of("U1"), clf.DONE)

        self.assertEqual(self.staff.get("/customer/U1").status_code, 200)
        csv = self.staff.get("/export.csv").get_data(as_text=True)
        self.assertIn("U1", csv)


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.client = crm.app.test_client()

    def test_login_page_and_wrong_password(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertIn("登入", self.client.get("/login").get_data(as_text=True))
        r = self.client.post("/login", data={"username": "admin", "password": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("不正確", r.get_data(as_text=True))

    def test_login_then_logout(self):
        r = self.client.post("/login", data={"username": "admin", "password": "pw", "next": "/export.csv"})
        self.assertEqual(r.headers["Location"], "/export.csv")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.get("/logout")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_no_open_redirect(self):
        for bad in ("//evil.example", "https://evil.example", "/\\evil.example"):
            r = self.client.post("/login", data={"username": "admin", "password": "pw", "next": bad})
            self.assertEqual(r.headers["Location"], "/", bad)
            self.client.get("/logout")

    def test_cross_site_post_rejected(self):
        self.client.post("/login", data={"username": "admin", "password": "pw"})
        r = self.client.post("/customer/U1/update", data={"action": "replied"},
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)


class CloudflareAccessTest(unittest.TestCase):
    """模擬 Cloudflare Access：用自己產生的金鑰簽 JWT，確認真的會驗證簽章與對象。"""

    @classmethod
    def setUpClass(cls):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.jwt = jwt
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        class FakeJWKS:
            def get_signing_key_from_jwt(_self, token):
                return type("K", (), {"key": cls.key.public_key()})()

        crm.CF_ACCESS_TEAM_DOMAIN = "team.cloudflareaccess.com"
        crm.CF_ACCESS_AUD = "aud123"
        crm._jwks_client = FakeJWKS()

    @classmethod
    def tearDownClass(cls):
        crm.CF_ACCESS_TEAM_DOMAIN = crm.CF_ACCESS_AUD = ""
        crm._jwks_client = None

    def token(self, key=None, aud="aud123", email="amy@beauty-keys.com"):
        import time
        claims = {"email": email, "aud": aud, "iss": "https://team.cloudflareaccess.com",
                  "exp": int(time.time()) + 300}
        return self.jwt.encode(claims, key or self.key, algorithm="RS256")

    def test_valid_token_logs_in_and_records_who(self):
        c = crm.app.test_client()
        h = {"Cf-Access-Jwt-Assertion": self.token()}
        body = json.dumps({"events": [text_event("U7", "請問皮秒多少錢", "a7")]}).encode()
        c.post("/callback", data=body, headers={"X-Line-Signature": sign(body)})
        page = c.get("/", headers=h)
        self.assertEqual(page.status_code, 200)
        self.assertIn("顧問式服務看板・amy", page.get_data(as_text=True))  # 名單沒有這個 email → 顯示前半段
        c.post("/customer/U7/update", data={"action": "replied"}, headers=h)
        with crm.app.app_context():
            staff = crm.get_db().execute(
                "SELECT staff FROM actions WHERE user_id='U7' AND action='replied'").fetchone()[0]
        self.assertEqual(staff, "amy@beauty-keys.com")

    def test_forged_or_wrong_audience_rejected(self):
        c = crm.app.test_client()
        for tok in (self.token(key=self.other_key), self.token(aud="other-app"), "garbage"):
            self.assertEqual(c.get("/", headers={"Cf-Access-Jwt-Assertion": tok}).status_code, 302)


# ── 操作紀錄、認領、報表、背景工作 ─────────────────────────

import time  # noqa: E402
from datetime import timedelta  # noqa: E402

import jobs  # noqa: E402
import ragic  # noqa: E402


def ms_ago(minutes):
    return int((time.time() - minutes * 60) * 1000)


class ActionLogTest(unittest.TestCase):
    def setUp(self):
        self.client = crm.app.test_client()
        self.staff = crm.app.test_client()
        self.staff.post("/login", data={"username": "admin", "password": "pw"})

    def post(self, events):
        body = json.dumps({"events": events}).encode()
        self.client.post("/callback", data=body, headers={"X-Line-Signature": sign(body)})

    def db(self):
        return crm.connect()

    def actions(self, uid):
        return self.db().execute("SELECT * FROM actions WHERE user_id=? ORDER BY id", (uid,)).fetchall()

    def test_every_click_is_logged_with_wait_time(self):
        self.post([text_event("A1", "請問皮秒多少錢", "x1", ts=ms_ago(40))])
        self.staff.post("/customer/A1/update", data={"action": "claim"})
        self.assertIn("處理中", self.staff.get("/").get_data(as_text=True))
        self.staff.post("/customer/A1/update", data={"action": "replied"})
        acts = self.actions("A1")
        self.assertEqual([a["action"] for a in acts], ["claim", "replied"])
        self.assertEqual(acts[1]["from_status"], clf.REPLY)
        self.assertEqual(acts[1]["to_status"], clf.DONE)
        self.assertGreaterEqual(acts[1]["response_minutes"], 39)
        c = self.db().execute("SELECT * FROM customers WHERE user_id='A1'").fetchone()
        self.assertIsNone(c["pending_since"])
        self.assertIsNone(c["claimed_by"])  # 回覆後認領自動解除

        # 客人又問問題 → 重新計時；改回待回覆也會記錄
        self.post([text_event("A1", "那音波呢？", "x2", ts=ms_ago(5))])
        self.assertIsNotNone(self.db().execute(
            "SELECT pending_since FROM customers WHERE user_id='A1'").fetchone()[0])
        self.staff.post("/customer/A1/update", data={"action": "follow", "follow_up_at": "2026-12-01"})
        self.staff.post("/customer/A1/update", data={"action": "reopen"})
        self.assertEqual([a["action"] for a in self.actions("A1")][-2:], ["follow", "reopen"])

    def test_note_logged_only_when_changed(self):
        self.post([text_event("A2", "想預約", "y1")])
        self.staff.post("/customer/A2/update", data={"action": "note", "note": "油肌、在意毛孔"})
        self.staff.post("/customer/A2/update", data={"action": "note", "note": "油肌、在意毛孔"})
        self.assertEqual([a["action"] for a in self.actions("A2")], ["note"])

    def test_claim_expires(self):
        self.post([text_event("A3", "想預約", "z1")])
        self.staff.post("/customer/A3/update", data={"action": "claim"})
        c = self.db().execute("SELECT * FROM customers WHERE user_id='A3'").fetchone()
        self.assertTrue(crm.claim_active(c))
        self.assertFalse(crm.claim_active(c, crm.now() + timedelta(minutes=crm.CLAIM_MINUTES + 1)))

    def test_report_page_and_csv(self):
        self.post([text_event("A4", "請問多少錢", "r1", ts=ms_ago(20))])
        self.staff.post("/customer/A4/update", data={"action": "replied"})
        page = self.staff.get("/report").get_data(as_text=True)
        self.assertIn("同仁處理統計", page)
        self.assertIn("管理員（本機登入）", page)
        csv_text = self.staff.get("/report.csv").get_data(as_text=True)
        self.assertIn("已回覆", csv_text)
        self.assertEqual(self.client.get("/report").status_code, 302)  # 要登入

    def test_staff_name_from_ragic_list(self):
        db = self.db()
        db.execute("INSERT OR REPLACE INTO staff (email, name) VALUES ('amy@x.com', '張菱')")
        db.commit()
        with crm.app.app_context():
            self.assertEqual(crm.staff_name("Amy@x.com"), "張菱")
            self.assertEqual(crm.staff_name("bob@x.com"), "bob")


class MigrationTest(unittest.TestCase):
    def test_old_database_upgraded(self):
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.executescript("""CREATE TABLE customers (user_id TEXT PRIMARY KEY, display_name TEXT,
            status TEXT NOT NULL DEFAULT 'reply', reason TEXT, key_message TEXT, tags TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '', follow_up_at TEXT, last_message TEXT, last_message_at TEXT,
            blocked INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
            INSERT INTO customers (user_id, status, last_message_at) VALUES ('O1', 'urgent', '2026-09-28 10:00');""")
        old.commit()
        old.close()
        saved, crm.DB_PATH = crm.DB_PATH, path
        try:
            row = crm.connect().execute("SELECT * FROM customers WHERE user_id='O1'").fetchone()
        finally:
            crm.DB_PATH = saved
        self.assertEqual(row["pending_since"], "2026-09-28 10:00")
        self.assertEqual(row["urgent_since"], "2026-09-28 10:00")


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._orig = (jobs.notify.enabled, jobs.notify.send_chat, ragic.enabled, ragic.push_log, ragic.fetch_staff)
        jobs.notify.enabled = lambda: True
        jobs.notify.send_chat = lambda text: self.sent.append(text) or True
        jobs.ALERT_HOURS = ""
        jobs.PUBLIC_URL = "https://crm.example"
        self.client = crm.app.test_client()

    def tearDown(self):
        (jobs.notify.enabled, jobs.notify.send_chat, ragic.enabled, ragic.push_log, ragic.fetch_staff) = self._orig

    def post(self, events):
        body = json.dumps({"events": events}).encode()
        self.client.post("/callback", data=body, headers={"X-Line-Signature": sign(body)})

    def test_urgent_alert_once_after_threshold(self):
        self.post([text_event("J1", "打完雷射很腫正常嗎", "j1", ts=ms_ago(20))])
        self.post([text_event("J2", "打完玻尿酸有硬塊", "j2", ts=ms_ago(3))])
        db = crm.connect()
        jobs.urgent_alerts(db, crm.now())
        alert = [t for t in self.sent if "🚨" in t]
        self.assertEqual(len(alert), 1)
        self.assertIn("/customer/J1", alert[0])
        self.assertNotIn("/customer/J2", alert[0])  # 還沒到 15 分鐘
        jobs.urgent_alerts(db, crm.now())
        self.assertEqual(len([t for t in self.sent if "🚨" in t]), 1)  # 不重複提醒

    def test_alert_hours(self):
        jobs.ALERT_HOURS = "9-21"
        at = crm.now().replace(hour=23)
        self.assertFalse(jobs.in_alert_hours(at))
        self.assertTrue(jobs.in_alert_hours(at.replace(hour=9)))

    def test_morning_report_once_a_day_and_no_message_text(self):
        self.post([text_event("J3", "想問皮秒價錢", "j3", ts=ms_ago(60))])
        db = crm.connect()
        db.execute("DELETE FROM kv WHERE key='morning_report'")
        db.commit()
        at = crm.now().replace(hour=9, minute=5)
        jobs.morning_report(db, at)
        jobs.morning_report(db, at.replace(minute=6))
        reports = [t for t in self.sent if "早報" in t]
        self.assertEqual(len(reports), 1)
        self.assertIn("待回覆", reports[0])
        self.assertNotIn("皮秒", reports[0])  # 早報不帶客人訊息內容
        db.execute("DELETE FROM kv WHERE key='morning_report'")
        db.commit()
        jobs.morning_report(db, at.replace(hour=22))  # 晚上不補發
        self.assertEqual(len([t for t in self.sent if "早報" in t]), 1)

    def test_push_ragic_marks_synced_and_retries_failures(self):
        staff = crm.app.test_client()
        staff.post("/login", data={"username": "admin", "password": "pw"})
        self.post([text_event("J4", "想預約", "j4")])
        staff.post("/customer/J4/update", data={"action": "replied"})
        ragic.enabled = lambda sheet: True
        db = crm.connect()
        db.execute("UPDATE actions SET ragic_synced_at='x' WHERE user_id != 'J4'")  # 只測這一筆
        db.commit()

        def fail(fields):
            raise ragic.RagicError("down")
        ragic.push_log = fail
        jobs.push_ragic(db)
        row = db.execute("SELECT * FROM actions WHERE user_id='J4'").fetchone()
        self.assertIsNone(row["ragic_synced_at"])
        self.assertEqual(row["ragic_error"], "down")

        pushed = []
        ragic.push_log = lambda fields: pushed.append(fields) or "99"
        jobs.push_ragic(db)
        row = db.execute("SELECT * FROM actions WHERE user_id='J4'").fetchone()
        self.assertEqual(row["ragic_id"], "99")
        mine = [f for f in pushed if f["LINE userId"] == "J4"]
        self.assertEqual(mine[0]["動作"], "已回覆")
        self.assertNotIn("想預約", json.dumps(mine[0], ensure_ascii=False))  # 不送客人訊息內容

    def test_sync_staff_daily(self):
        ragic.enabled = lambda sheet: True
        ragic.fetch_staff = lambda: [{"email": "a@x.com", "name": "甲", "role": "員工"}]
        db = crm.connect()
        db.execute("DELETE FROM kv WHERE key='staff_synced'")
        db.commit()
        jobs.sync_staff(db, crm.now())
        self.assertEqual(db.execute("SELECT name FROM staff WHERE email='a@x.com'").fetchone()[0], "甲")
        ragic.fetch_staff = lambda: self.fail("同一天不該再抓")
        jobs.sync_staff(db, crm.now())


class RagicBindingTest(unittest.TestCase):
    def setUp(self):
        self._orig = (ragic.enabled, ragic.search_customers, ragic.get_customer)
        ragic.enabled = lambda sheet: True
        ragic.search_customers = lambda q: [{"id": "123", "name": "王小美", "phone": "0912***678",
                                             "last_visit": "2026-09-01", "owner": "孫珮芬"}]
        ragic.get_customer = lambda rid: {"name": "王小美", "fields": [("顧客累積消費", "52000")],
                                          "url": "https://ragic.example/123"}
        self.client = crm.app.test_client()
        self.staff = crm.app.test_client()
        self.staff.post("/login", data={"username": "admin", "password": "pw"})
        body = json.dumps({"events": [text_event("B1", "想預約", "b1")]}).encode()
        self.client.post("/callback", data=body, headers={"X-Line-Signature": sign(body)})

    def tearDown(self):
        ragic.enabled, ragic.search_customers, ragic.get_customer = self._orig

    def test_search_bind_show_unbind(self):
        page = self.staff.get("/customer/B1?rq=王").get_data(as_text=True)
        self.assertIn("0912***678", page)
        self.staff.post("/customer/B1/update", data={"action": "bind", "ragic_id": "123", "ragic_name": "王小美"})
        page = self.staff.get("/customer/B1").get_data(as_text=True)
        self.assertIn("52000", page)
        self.staff.post("/customer/B1/update", data={"action": "unbind"})
        row = crm.connect().execute("SELECT ragic_customer_id FROM customers WHERE user_id='B1'").fetchone()
        self.assertIsNone(row[0])

    def test_bind_rejects_non_numeric_id(self):
        self.staff.post("/customer/B1/update", data={"action": "bind", "ragic_id": "../83", "ragic_name": "x"})
        row = crm.connect().execute("SELECT ragic_customer_id FROM customers WHERE user_id='B1'").fetchone()
        self.assertIsNone(row[0])

    def test_push_log_uses_field_ids(self):
        sent = {}
        orig_req = ragic._request
        os.environ["RAGIC_LOG_FIELD_IDS"] = "時間=101,動作=102"
        ragic._request = lambda sheet, body=None, **kw: sent.update(body) or {"status": "SUCCESS", "ragicId": 5}
        try:
            self.assertEqual(ragic.push_log({"時間": "2026/09/29 10:00", "動作": "已回覆"}), "5")
            self.assertEqual(sent, {"101": "2026/09/29 10:00", "102": "已回覆"})
            with self.assertRaises(ragic.RagicError):
                ragic.push_log({"說明": "x"})  # 沒設定編號的欄位要報錯，不能默默丟掉
        finally:
            ragic._request = orig_req
            del os.environ["RAGIC_LOG_FIELD_IDS"]

    def test_mask_phone(self):
        self.assertEqual(ragic.mask_phone("0912-345-678"), "0912***678")
        self.assertEqual(ragic.mask_phone(""), "")


if __name__ == "__main__":
    unittest.main()
