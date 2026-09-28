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

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}


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

        self.assertEqual(self.client.get("/").status_code, 401)
        page = self.client.get("/", headers=AUTH).get_data(as_text=True)
        self.assertIn("打完肉毒頭暈怎麼辦", page)  # 顯示關鍵訊息，不只最後一句

        self.client.post("/customer/U1/update", data={"action": "follow", "follow_up_at": "2026-10-01"}, headers=AUTH)
        self.assertEqual(self.status_of("U1"), clf.FOLLOW)
        self.client.post("/customer/U1/update", data={"action": "replied"}, headers=AUTH)
        self.assertEqual(self.status_of("U1"), clf.DONE)

        self.assertEqual(self.client.get("/customer/U1", headers=AUTH).status_code, 200)
        csv = self.client.get("/export.csv", headers=AUTH).get_data(as_text=True)
        self.assertIn("U1", csv)


if __name__ == "__main__":
    unittest.main()
