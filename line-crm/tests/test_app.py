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
        self.assertIn("amy@beauty-keys.com", page.get_data(as_text=True))
        c.post("/customer/U7/update", data={"action": "replied"}, headers=h)
        with crm.app.app_context():
            reason = crm.get_db().execute("SELECT reason FROM customers WHERE user_id='U7'").fetchone()[0]
        self.assertIn("amy@beauty-keys.com", reason)

    def test_forged_or_wrong_audience_rejected(self):
        c = crm.app.test_client()
        for tok in (self.token(key=self.other_key), self.token(aud="other-app"), "garbage"):
            self.assertEqual(c.get("/", headers={"Cf-Access-Jwt-Assertion": tok}).status_code, 302)


if __name__ == "__main__":
    unittest.main()
