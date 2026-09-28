"""美之耀 LINE 官方帳號 客戶狀況追蹤

流程：
    客人傳 LINE → LINE 平台呼叫 /callback（webhook）
    → 驗證簽章 → 分類（立即回覆／需要回覆／需要追蹤／無需處理）→ 存進 SQLite
    → 諮詢師打開 / 管理頁，依優先順序處理，按「已回覆」或「設定追蹤日」

注意：在 LINE Official Account Manager 聊天室手動回覆的訊息「不會」送到 webhook，
所以回覆完要在管理頁按一下「已回覆」，系統才知道這位客人處理好了。
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import jwt
from flask import Flask, Response, abort, g, redirect, render_template, request, session, url_for

import classifier as clf

TZ = ZoneInfo("Asia/Taipei")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "line_crm.db"))
FOLLOW_UP_DAYS = int(os.environ.get("FOLLOW_UP_DAYS", "3"))
# Cloudflare Access：設定後，通過 email 驗證的同仁直接進入，不用再輸入密碼
CF_ACCESS_TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "")  # 例：xxx.cloudflareaccess.com
CF_ACCESS_AUD = os.environ.get("CF_ACCESS_AUD", "")


def _secret_key():
    """登入狀態用的簽章金鑰：第一次啟動時產生並存檔，重啟後同仁不必重新登入。"""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        with open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write(key)
        return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    user_id         TEXT PRIMARY KEY,
    display_name    TEXT,
    status          TEXT NOT NULL DEFAULT 'reply',
    reason          TEXT,
    key_message     TEXT,
    tags            TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    follow_up_at    TEXT,
    last_message    TEXT,
    last_message_at TEXT,
    blocked         INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    event_id    TEXT UNIQUE,
    kind        TEXT NOT NULL,
    text        TEXT,
    status      TEXT,
    reason      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, created_at);
"""


def now():
    return datetime.now(TZ)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.executescript(SCHEMA)
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ── LINE webhook ──────────────────────────────────────────────


def verify_signature(body: bytes, signature: str) -> bool:
    if not CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def fetch_display_name(user_id):
    """向 LINE 取得客人暱稱；失敗就回傳 None，不影響記錄。"""
    if not CHANNEL_ACCESS_TOKEN:
        return None
    req = urllib.request.Request(
        f"https://api.line.me/v2/bot/profile/{user_id}",
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp).get("displayName")
    except Exception:
        return None


def describe_message(msg):
    kind = msg.get("type", "unknown")
    if kind == "text":
        return kind, msg.get("text", "")
    placeholder = {
        "image": "［照片］", "video": "［影片］", "audio": "［語音］",
        "sticker": "［貼圖］", "location": "［位置］", "file": "［檔案］",
    }
    return kind, placeholder.get(kind, f"［{kind}］")


def record_incoming(db, user_id, event_id, kind, text, status, reason, tags, ts):
    """寫入一則客人訊息並更新客人目前狀態。重送的事件（同 event_id）會被忽略。"""
    cur = db.execute(
        "INSERT OR IGNORE INTO messages (user_id, event_id, kind, text, status, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, event_id, kind, text, status, reason, fmt(ts)),
    )
    if cur.rowcount == 0:
        return

    row = db.execute("SELECT * FROM customers WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        name = fetch_display_name(user_id)
        follow_up = fmt(ts + timedelta(days=FOLLOW_UP_DAYS))[:10] if status == clf.FOLLOW else None
        db.execute(
            "INSERT INTO customers (user_id, display_name, status, reason, key_message, tags, follow_up_at,"
            " last_message, last_message_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, status, reason, text, ",".join(tags), follow_up, text, fmt(ts), fmt(ts)),
        )
        return

    merged = clf.merge_status(row["status"], status)
    # 狀態由這則訊息決定時，記下它；否則保留原本那則關鍵訊息（例如術後不適）
    if merged == status:
        new_reason, key_message = reason, text
    else:
        new_reason, key_message = row["reason"], row["key_message"]
    old_tags = [t for t in row["tags"].split(",") if t]
    all_tags = old_tags + [t for t in tags if t not in old_tags]
    follow_up = row["follow_up_at"]
    if merged == clf.FOLLOW and not follow_up:
        follow_up = fmt(ts + timedelta(days=FOLLOW_UP_DAYS))[:10]
    name = row["display_name"] or fetch_display_name(user_id)
    db.execute(
        "UPDATE customers SET display_name=?, status=?, reason=?, key_message=?, tags=?, follow_up_at=?,"
        " last_message=?, last_message_at=?, blocked=0, updated_at=? WHERE user_id=?",
        (name, merged, new_reason, key_message, ",".join(all_tags), follow_up, text, fmt(ts), fmt(ts), user_id),
    )


def handle_event(db, event):
    source = event.get("source", {})
    user_id = source.get("userId")
    if source.get("type") != "user" or not user_id:
        return  # 群組訊息不追蹤
    ts = datetime.fromtimestamp(event.get("timestamp", 0) / 1000, TZ) if event.get("timestamp") else now()
    event_id = event.get("webhookEventId")
    etype = event.get("type")

    if etype == "message":
        kind, text = describe_message(event.get("message", {}))
        status, reason, tags = clf.classify_event(kind, text if kind == "text" else None)
        record_incoming(db, user_id, event_id, kind, text, status, reason, tags, ts)
    elif etype == "follow":
        record_incoming(db, user_id, event_id, "follow", "［加入好友］", clf.REPLY,
                        "新好友：可以開始了解膚況與需求", [], ts)
    elif etype == "unfollow":
        db.execute("UPDATE customers SET blocked=1, updated_at=? WHERE user_id=?", (fmt(ts), user_id))


@app.post("/callback")
def callback():
    body = request.get_data()
    if not verify_signature(body, request.headers.get("X-Line-Signature", "")):
        abort(400)
    db = get_db()
    for event in json.loads(body or b"{}").get("events", []):
        handle_event(db, event)
    db.commit()
    return "OK"


# ── 管理頁登入 ───────────────────────────────────────────
#
# 1. 從 crm.beauty-keys.com 進來：Cloudflare Access 已驗證 email，
#    這裡再驗一次 Cloudflare 簽發的 JWT（防止偽造），通過就直接登入。
# 2. 在家用 localhost 開啟：顯示登入頁，用 .env 的帳號密碼登入。

_jwks_client = None


def access_user():
    """回傳 Cloudflare Access 驗證過的 email；沒有或驗證失敗則回傳 None。"""
    global _jwks_client
    token = request.headers.get("Cf-Access-Jwt-Assertion")
    if not (token and CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD):
        return None
    try:
        if _jwks_client is None:
            _jwks_client = jwt.PyJWKClient(f"https://{CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs")
        key = _jwks_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=CF_ACCESS_AUD,
                            issuer=f"https://{CF_ACCESS_TEAM_DOMAIN}")
        return claims.get("email")
    except Exception as e:
        app.logger.warning("Cloudflare Access 憑證驗證失敗：%s", e)
        return None


def current_user():
    return access_user() or session.get("user")


def safe_next(target):
    """只允許跳轉到本站路徑，避免被利用成轉址到外部網站。"""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("dashboard")


def require_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        # 跨站送出的表單一律拒絕
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] != request.host:
                abort(403)
        g.user = user
        g.via_access = access_user() is not None
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = safe_next(request.values.get("next"))
    if current_user():
        return redirect(next_url)
    error = None
    if request.method == "POST":
        ok = (
            DASHBOARD_PASSWORD
            and hmac.compare_digest(request.form.get("username", ""), DASHBOARD_USER)
            and hmac.compare_digest(request.form.get("password", ""), DASHBOARD_PASSWORD)
        )
        if ok:
            session.clear()
            session.permanent = True
            session["user"] = DASHBOARD_USER
            return redirect(next_url)
        error = "帳號或密碼不正確"
    return render_template("login.html", error=error, next_url=next_url), (401 if error else 200)


@app.get("/logout")
def logout():
    session.clear()
    if access_user():
        return redirect("/cdn-cgi/access/logout")
    return redirect(url_for("login"))


def group_customers(rows):
    today = now().strftime("%Y-%m-%d")
    groups = {"urgent": [], "reply": [], "due": [], "follow": [], "done": []}
    for r in rows:
        c = dict(r)
        c["tag_list"] = [t for t in c["tags"].split(",") if t]
        if c["status"] == clf.FOLLOW and c["follow_up_at"] and c["follow_up_at"] <= today:
            groups["due"].append(c)
        else:
            groups.get(c["status"], groups["reply"]).append(c)
    return groups


@app.get("/")
@require_login
def dashboard():
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM customers WHERE blocked = 0"
    params = []
    if q:
        sql += " AND (display_name LIKE ? OR note LIKE ? OR tags LIKE ? OR last_message LIKE ?)"
        params = [f"%{q}%"] * 4
    rows = get_db().execute(sql + " ORDER BY last_message_at DESC", params).fetchall()
    return render_template("dashboard.html", groups=group_customers(rows), q=q,
                           labels=clf.STATUS_LABEL, follow_days=FOLLOW_UP_DAYS)


@app.get("/customer/<user_id>")
@require_login
def customer(user_id):
    db = get_db()
    c = db.execute("SELECT * FROM customers WHERE user_id = ?", (user_id,)).fetchone()
    if c is None:
        abort(404)
    msgs = db.execute(
        "SELECT * FROM messages WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 200", (user_id,)
    ).fetchall()
    default_follow = fmt(now() + timedelta(days=FOLLOW_UP_DAYS))[:10]
    return render_template("customer.html", c=c, msgs=msgs, labels=clf.STATUS_LABEL,
                           default_follow=default_follow)


@app.post("/customer/<user_id>/update")
@require_login
def update_customer(user_id):
    db = get_db()
    action = request.form.get("action")
    note = request.form.get("note")
    ts = fmt(now())
    if action == "replied":
        db.execute("UPDATE customers SET status=?, reason=?, follow_up_at=NULL, updated_at=? WHERE user_id=?",
                   (clf.DONE, f"已回覆（{g.user}，{ts}）", ts, user_id))
    elif action == "follow":
        date = request.form.get("follow_up_at") or fmt(now() + timedelta(days=FOLLOW_UP_DAYS))[:10]
        db.execute("UPDATE customers SET status=?, reason=?, follow_up_at=?, updated_at=? WHERE user_id=?",
                   (clf.FOLLOW, f"已回覆（{g.user}），{date} 追蹤", date, ts, user_id))
    elif action == "reopen":
        db.execute("UPDATE customers SET status=?, reason=?, updated_at=? WHERE user_id=?",
                   (clf.REPLY, f"{g.user} 改回需要回覆", ts, user_id))
    if note is not None:
        db.execute("UPDATE customers SET note=?, updated_at=? WHERE user_id=?", (note.strip(), ts, user_id))
    db.commit()
    return redirect(safe_next(request.form.get("next")))


@app.get("/export.csv")
@require_login
def export_csv():
    rows = get_db().execute("SELECT * FROM customers ORDER BY last_message_at DESC").fetchall()
    buf = io.StringIO()
    buf.write("﻿")  # 讓 Excel 正確顯示中文
    w = csv.writer(buf)
    w.writerow(["暱稱", "狀態", "原因", "療程/階段標籤", "追蹤日", "最後訊息", "最後訊息時間", "備註", "已封鎖", "LINE userId"])
    for r in rows:
        w.writerow([r["display_name"], clf.STATUS_LABEL.get(r["status"], r["status"]), r["reason"], r["tags"],
                    r["follow_up_at"], r["last_message"], r["last_message_at"], r["note"],
                    "是" if r["blocked"] else "", r["user_id"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=line_customers.csv"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8765")))
