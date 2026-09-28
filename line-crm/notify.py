"""發通知到 Synology Chat（DS225 自架，憑證是自簽的，所以不驗證 TLS）。"""

import json
import os
import ssl
import urllib.parse
import urllib.request

WEBHOOK = os.environ.get("SYNOLOGY_CHAT_WEBHOOK", "")


def enabled():
    return bool(WEBHOOK)


def send_chat(text):
    if not WEBHOOK:
        return False
    data = urllib.parse.urlencode({"payload": json.dumps({"text": text})}).encode()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(WEBHOOK, data=data)
    with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
        body = json.load(resp)
    if not body.get("success", False):
        raise RuntimeError(f"Synology Chat 回應失敗：{body}")
    return True
