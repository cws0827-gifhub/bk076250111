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
import ragic

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
# 「我來回」認領多久後自動失效（分鐘）
CLAIM_MINUTES = int(os.environ.get("CLAIM_MINUTES", "30"))


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
-- 同仁每按一次按鈕就新增一筆，不覆蓋；報表與 Ragic 同步都從這裡來
CREATE TABLE IF NOT EXISTS actions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           TEXT NOT NULL,
    customer_name     TEXT,
    action            TEXT NOT NULL,
    from_status       TEXT,
    to_status         TEXT,
    staff             TEXT NOT NULL,
    staff_name        TEXT,
    follow_up_at      TEXT,
    response_minutes  INTEGER,
    detail            TEXT,
    created_at        TEXT NOT NULL,
    ragic_synced_at   TEXT,
    ragic_id          TEXT,
    ragic_error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_time ON actions(created_at);
-- 從 Ragic 員工表同步：只存 email、姓名、角色
CREATE TABLE IF NOT EXISTS staff (
    email       TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

# 舊資料庫升級：customers 補上新欄位
CUSTOMER_NEW_COLUMNS = {
    "pending_since": "TEXT",       # 開始等待回覆的時間（算回覆時效用）
    "urgent_since": "TEXT",        # 變成術後關懷的時間（急件提醒用）
    "urgent_alerted_at": "TEXT",
    "claimed_by": "TEXT",
    "claimed_at": "TEXT",
    "ragic_customer_id": "TEXT",
    "ragic_customer_name": "TEXT",
}


def migrate(db):
    have = {r[1] for r in db.execute("PRAGMA table_info(customers)")}
    added = False
    for col, typ in CUSTOMER_NEW_COLUMNS.items():
        if col not in have:
            db.execute(f"ALTER TABLE customers ADD COLUMN {col} {typ}")
            added = True
    if added:
        # 升級當下還沒處理的客人，從最後一則訊息開始計時
        db.execute("UPDATE customers SET pending_since = last_message_at"
                   " WHERE pending_since IS NULL AND status IN ('urgent', 'reply')")
        db.execute("UPDATE customers SET urgent_since = last_message_at"
                   " WHERE urgent_since IS NULL AND status = 'urgent'")
    db.commit()


def connect():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    migrate(db)
    return db


def now():
    return datetime.now(TZ)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def parse(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ) if text else None


def minutes_between(start_text, end):
    start = parse(start_text)
    return max(0, int((end - start).total_seconds() // 60)) if start else None


def get_db():
    if "db" not in g:
        g.db = connect()
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


WAITING = (clf.URGENT, clf.REPLY)  # 這兩種狀態代表客人在等我們回覆


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
        pending = fmt(ts) if status in WAITING else None
        urgent = fmt(ts) if status == clf.URGENT else None
        db.execute(
            "INSERT INTO customers (user_id, display_name, status, reason, key_message, tags, follow_up_at,"
            " last_message, last_message_at, updated_at, pending_since, urgent_since)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, status, reason, text, ",".join(tags), follow_up, text, fmt(ts), fmt(ts),
             pending, urgent),
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
    # 開始等待回覆的時間：從「沒在等」變成「在等」的那一則訊息起算
    pending = row["pending_since"] or (fmt(ts) if merged in WAITING else None)
    urgent = row["urgent_since"] if row["status"] == clf.URGENT else (fmt(ts) if merged == clf.URGENT else None)
    db.execute(
        "UPDATE customers SET display_name=?, status=?, reason=?, key_message=?, tags=?, follow_up_at=?,"
        " last_message=?, last_message_at=?, blocked=0, updated_at=?, pending_since=?, urgent_since=?"
        " WHERE user_id=?",
        (name, merged, new_reason, key_message, ",".join(all_tags), follow_up, text, fmt(ts), fmt(ts),
         pending, urgent, user_id),
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
    history = db.execute("SELECT * FROM actions WHERE user_id = ? ORDER BY id DESC LIMIT 30",
                         (user_id,)).fetchall()

    # Ragic 顧客：已綁定就讀出摘要；沒綁定且有搜尋字就查詢
    ragic_on = ragic.enabled(ragic.CUSTOMER_SHEET)
    bound, results, ragic_error = None, None, None
    q = request.args.get("rq", "").strip()
    if ragic_on:
        try:
            if c["ragic_customer_id"]:
                bound = ragic.get_customer(c["ragic_customer_id"])
            elif q:
                results = ragic.search_customers(q)
        except ragic.RagicError as e:
            app.logger.warning("Ragic 讀取失敗：%s", e)
            ragic_error = "暫時讀不到 Ragic，請稍後再試"
    return render_template("customer.html", c=c, msgs=msgs, labels=clf.STATUS_LABEL,
                           default_follow=default_follow, history=history, ragic_on=ragic_on,
                           bound=bound, results=results, rq=q, ragic_error=ragic_error)


ACTION_LABEL = {
    "replied": "已回覆", "follow": "回覆並設追蹤", "reopen": "改回需要回覆", "note": "修改備註",
    "claim": "我來回（認領）", "unclaim": "取消認領", "bind": "綁定 Ragic 顧客", "unbind": "解除綁定",
}
HANDLED = ("replied", "follow")  # 算「處理完成」的動作


def staff_name(email, db=None):
    """email → 同仁姓名（Ragic 員工表同步來的）；找不到就顯示 email 前半段。"""
    if not email:
        return ""
    if email == DASHBOARD_USER:
        return "管理員（本機登入）"
    db = db or get_db()
    row = db.execute("SELECT name FROM staff WHERE email = ?", (email.lower(),)).fetchone()
    return row["name"] if row else email.split("@")[0]


@app.context_processor
def template_helpers():
    return {"who": staff_name, "claim_active": claim_active, "action_labels": ACTION_LABEL}


def claim_active(c, at=None):
    """認領在 CLAIM_MINUTES 內有效；過期視同沒人認領。"""
    if not c["claimed_by"] or not c["claimed_at"]:
        return False
    return minutes_between(c["claimed_at"], at or now()) < CLAIM_MINUTES


def log_action(db, c, action, to_status, follow_up_at=None, response_minutes=None, detail=None, ts=None):
    db.execute(
        "INSERT INTO actions (user_id, customer_name, action, from_status, to_status, staff, staff_name,"
        " follow_up_at, response_minutes, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (c["user_id"], c["display_name"], action, c["status"], to_status, g.user, staff_name(g.user, db),
         follow_up_at, response_minutes, detail, ts or fmt(now())),
    )


@app.post("/customer/<user_id>/update")
@require_login
def update_customer(user_id):
    db = get_db()
    c = db.execute("SELECT * FROM customers WHERE user_id = ?", (user_id,)).fetchone()
    if c is None:
        abort(404)
    action = request.form.get("action")
    note = request.form.get("note")
    at = now()
    ts = fmt(at)
    who = staff_name(g.user, db)
    clear = "pending_since=NULL, urgent_since=NULL, urgent_alerted_at=NULL, claimed_by=NULL, claimed_at=NULL"

    if action in HANDLED:
        waited = minutes_between(c["pending_since"], at)
        if action == "replied":
            date = None
            db.execute(f"UPDATE customers SET status=?, reason=?, follow_up_at=NULL, updated_at=?, {clear}"
                       " WHERE user_id=?", (clf.DONE, f"已回覆（{who}，{ts}）", ts, user_id))
            to = clf.DONE
        else:
            date = request.form.get("follow_up_at") or fmt(at + timedelta(days=FOLLOW_UP_DAYS))[:10]
            db.execute(f"UPDATE customers SET status=?, reason=?, follow_up_at=?, updated_at=?, {clear}"
                       " WHERE user_id=?", (clf.FOLLOW, f"已回覆（{who}），{date} 追蹤", date, ts, user_id))
            to = clf.FOLLOW
        log_action(db, c, action, to, follow_up_at=date, response_minutes=waited, ts=ts)
    elif action == "reopen":
        db.execute("UPDATE customers SET status=?, reason=?, updated_at=?, pending_since=?, urgent_since=NULL,"
                   " urgent_alerted_at=NULL, claimed_by=NULL, claimed_at=NULL WHERE user_id=?",
                   (clf.REPLY, f"{who} 改回需要回覆", ts, ts, user_id))
        log_action(db, c, action, clf.REPLY, ts=ts)
    elif action == "claim":
        db.execute("UPDATE customers SET claimed_by=?, claimed_at=? WHERE user_id=?", (g.user, ts, user_id))
        log_action(db, c, action, c["status"], ts=ts)
    elif action == "unclaim":
        db.execute("UPDATE customers SET claimed_by=NULL, claimed_at=NULL WHERE user_id=?", (user_id,))
        log_action(db, c, action, c["status"], ts=ts)
    elif action == "bind":
        rid, rname = request.form.get("ragic_id", "").strip(), request.form.get("ragic_name", "").strip()
        if rid.isdigit():
            db.execute("UPDATE customers SET ragic_customer_id=?, ragic_customer_name=? WHERE user_id=?",
                       (rid, rname, user_id))
            log_action(db, c, action, c["status"], detail=f"{rname}（Ragic #{rid}）", ts=ts)
    elif action == "unbind":
        db.execute("UPDATE customers SET ragic_customer_id=NULL, ragic_customer_name=NULL WHERE user_id=?",
                   (user_id,))
        log_action(db, c, action, c["status"], detail=c["ragic_customer_name"], ts=ts)

    if note is not None and note.strip() != (c["note"] or ""):
        db.execute("UPDATE customers SET note=?, updated_at=? WHERE user_id=?", (note.strip(), ts, user_id))
        current = db.execute("SELECT status FROM customers WHERE user_id = ?", (user_id,)).fetchone()[0]
        log_action(db, c, "note", current, ts=ts)
    db.commit()
    return redirect(safe_next(request.form.get("next")))


# ── 報表 ────────────────────────────────────────────


def report_range():
    """預設最近 7 天；可用 ?start=YYYY-MM-DD&end=YYYY-MM-DD 指定。"""
    today = now().date()
    try:
        end = datetime.strptime(request.args.get("end", ""), "%Y-%m-%d").date()
    except ValueError:
        end = today
    try:
        start = datetime.strptime(request.args.get("start", ""), "%Y-%m-%d").date()
    except ValueError:
        start = end - timedelta(days=6)
    return min(start, end), max(start, end)


def staff_summary(db, start, end):
    """每位同仁在期間內各動作次數、處理件數、回覆等待時間（中位數／平均）。"""
    rows = db.execute(
        "SELECT staff, staff_name, action, from_status, response_minutes FROM actions"
        " WHERE substr(created_at, 1, 10) BETWEEN ? AND ?", (str(start), str(end))).fetchall()
    people = {}
    for r in rows:
        p = people.setdefault(r["staff"], {"staff": r["staff"], "name": r["staff_name"] or r["staff"],
                                           "counts": {}, "handled": 0, "urgent": 0, "waits": []})
        p["counts"][r["action"]] = p["counts"].get(r["action"], 0) + 1
        if r["action"] in HANDLED:
            p["handled"] += 1
            if r["from_status"] == clf.URGENT:
                p["urgent"] += 1
            if r["response_minutes"] is not None:
                p["waits"].append(r["response_minutes"])
    for p in people.values():
        w = sorted(p["waits"])
        p["median"] = w[len(w) // 2] if w else None
        p["avg"] = round(sum(w) / len(w)) if w else None
    return sorted(people.values(), key=lambda p: -p["handled"])


def waiting_customers(db, at=None):
    at = at or now()
    rows = db.execute("SELECT * FROM customers WHERE blocked = 0 AND status IN ('urgent', 'reply')"
                      " ORDER BY status = 'urgent' DESC, pending_since").fetchall()
    out = []
    for r in rows:
        c = dict(r)
        c["waited"] = minutes_between(c["pending_since"], at)
        c["claimed"] = claim_active(r, at)
        out.append(c)
    return out


def human_minutes(m):
    if m is None:
        return "—"
    if m < 60:
        return f"{m} 分鐘"
    if m < 1440:
        return f"{m // 60} 小時 {m % 60} 分"
    return f"{m // 1440} 天 {m % 1440 // 60} 小時"


app.jinja_env.filters["mins"] = human_minutes


@app.get("/report")
@require_login
def report():
    db = get_db()
    start, end = report_range()
    recent = db.execute("SELECT * FROM actions WHERE substr(created_at, 1, 10) BETWEEN ? AND ?"
                        " ORDER BY id DESC LIMIT 300", (str(start), str(end))).fetchall()
    sync = db.execute("SELECT COUNT(*) AS n, MAX(ragic_error) AS err FROM actions"
                      " WHERE ragic_synced_at IS NULL").fetchone()
    return render_template("report.html", start=start, end=end, people=staff_summary(db, start, end),
                           waiting=waiting_customers(db), recent=recent, labels=clf.STATUS_LABEL,
                           sync=sync, ragic_on=ragic.enabled(ragic.LOG_SHEET))


@app.get("/report.csv")
@require_login
def report_csv():
    start, end = report_range()
    rows = get_db().execute("SELECT * FROM actions WHERE substr(created_at, 1, 10) BETWEEN ? AND ?"
                            " ORDER BY id", (str(start), str(end))).fetchall()
    buf = io.StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf)
    w.writerow(["時間", "處理人", "處理人 email", "動作", "客人暱稱", "原狀態", "新狀態", "追蹤日",
                "客人等待（分鐘）", "說明", "LINE userId", "已同步 Ragic"])
    for r in rows:
        w.writerow([r["created_at"], r["staff_name"], r["staff"], ACTION_LABEL.get(r["action"], r["action"]),
                    r["customer_name"], clf.STATUS_LABEL.get(r["from_status"], r["from_status"] or ""),
                    clf.STATUS_LABEL.get(r["to_status"], r["to_status"] or ""), r["follow_up_at"],
                    r["response_minutes"], r["detail"], r["user_id"], "是" if r["ragic_synced_at"] else ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=line_actions_{start}_{end}.csv"})


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
