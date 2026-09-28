"""背景工作：由 launchd 每分鐘執行一次（deploy/macos/jobs.sh）。

1. 同步員工名單：每天一次，從 Ragic 員工表取 email → 姓名
2. 處理紀錄寫進 Ragic：把還沒同步的操作逐筆送出，失敗下次再試
3. 術後急件提醒：術後關懷超過 URGENT_ALERT_MINUTES 分鐘沒人處理 → Synology Chat
4. 每日早報：每天 MORNING_REPORT_TIME 發一則摘要到 Synology Chat

每一項各自獨立，一項出錯不影響其他項。
"""

import os
import sys
import traceback
from datetime import datetime, timedelta

import app as crm
import classifier as clf
import notify
import ragic

URGENT_ALERT_MINUTES = int(os.environ.get("URGENT_ALERT_MINUTES", "15"))
ALERT_HOURS = os.environ.get("ALERT_HOURS", "9-21")          # 這段時間內才發急件提醒；空白＝24 小時
MORNING_REPORT_TIME = os.environ.get("MORNING_REPORT_TIME", "09:00")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")     # 例：https://crm.beauty-keys.com
WEEKDAYS = "一二三四五六日"


def log(msg):
    print(f"{crm.fmt(crm.now())} {msg}", flush=True)


def kv_get(db, key):
    row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(db, key, value):
    db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, value))
    db.commit()


def link(path=""):
    return f"{PUBLIC_URL}{path}" if PUBLIC_URL else ""


# ── 1. 員工名單 ──────────────────────────────────────────

def sync_staff(db, at):
    if not ragic.enabled(ragic.STAFF_SHEET):
        return
    today = at.strftime("%Y-%m-%d")
    if kv_get(db, "staff_synced") == today:
        return
    staff = ragic.fetch_staff()
    if not staff:
        log("員工名單是空的，保留舊名單")
        return
    with db:
        db.execute("DELETE FROM staff")
        db.executemany("INSERT INTO staff (email, name, role, updated_at) VALUES (?, ?, ?, ?)",
                       [(s["email"], s["name"], s["role"], crm.fmt(at)) for s in staff])
    kv_set(db, "staff_synced", today)
    log(f"員工名單已同步：{len(staff)} 人")


# ── 2. 寫進 Ragic ───────────────────────────────────────

def ragic_fields(r):
    label = clf.STATUS_LABEL
    return {
        "時間": r["created_at"].replace("-", "/"),  # Ragic 日期欄位用 yyyy/MM/dd
        "處理人": r["staff_name"] or r["staff"],
        "處理人Email": r["staff"],
        "動作": crm.ACTION_LABEL.get(r["action"], r["action"]),
        "客人暱稱": r["customer_name"] or "",
        "原狀態": label.get(r["from_status"], r["from_status"] or ""),
        "新狀態": label.get(r["to_status"], r["to_status"] or ""),
        "追蹤日": r["follow_up_at"] or "",
        "客人等待分鐘": "" if r["response_minutes"] is None else str(r["response_minutes"]),
        "說明": r["detail"] or "",
        "LINE userId": r["user_id"],
        "看板紀錄編號": str(r["id"]),
    }


def push_ragic(db, limit=50):
    if not ragic.enabled(ragic.LOG_SHEET):
        return
    rows = db.execute("SELECT * FROM actions WHERE ragic_synced_at IS NULL ORDER BY id LIMIT ?",
                      (limit,)).fetchall()
    sent = 0
    for r in rows:
        try:
            rid = ragic.push_log(ragic_fields(r))
        except ragic.RagicError as e:
            db.execute("UPDATE actions SET ragic_error = ? WHERE id = ?", (str(e)[:200], r["id"]))
            db.commit()
            log(f"Ragic 寫入失敗（紀錄 {r['id']}）：{e}")
            break  # 多半是連線或設定問題，下一分鐘再試
        db.execute("UPDATE actions SET ragic_synced_at = ?, ragic_id = ?, ragic_error = NULL WHERE id = ?",
                   (crm.fmt(crm.now()), rid, r["id"]))
        db.commit()
        sent += 1
    if sent:
        log(f"Ragic 已寫入 {sent} 筆")


# ── 3. 術後急件提醒 ──────────────────────────────────────

def in_alert_hours(at):
    if not ALERT_HOURS.strip():
        return True
    start, end = (int(x) for x in ALERT_HOURS.split("-"))
    return start <= at.hour < end


def urgent_alerts(db, at):
    if not notify.enabled() or not in_alert_hours(at):
        return
    cutoff = crm.fmt(at - timedelta(minutes=URGENT_ALERT_MINUTES))
    rows = db.execute(
        "SELECT * FROM customers WHERE status = 'urgent' AND blocked = 0 AND urgent_alerted_at IS NULL"
        " AND urgent_since IS NOT NULL AND urgent_since <= ? ORDER BY urgent_since", (cutoff,)).fetchall()
    if not rows:
        return
    lines = [f"🚨 *術後關懷超過 {URGENT_ALERT_MINUTES} 分鐘還沒回覆*（{len(rows)} 位）"]
    for r in rows:
        waited = crm.human_minutes(crm.minutes_between(r["urgent_since"], at))
        who = f"・🙋 {crm.staff_name(r['claimed_by'], db)} 處理中" if crm.claim_active(r, at) else ""
        url = link(f"/customer/{r['user_id']}")
        name = r["display_name"] or "（未取得暱稱）"
        lines.append(f"• {f'<{url}|{name}>' if url else name}　已等 {waited}{who}")
    lines.append("請醫師或護理師優先確認，回覆後記得在看板按「已回覆」。")
    notify.send_chat("\n".join(lines))
    db.executemany("UPDATE customers SET urgent_alerted_at = ? WHERE user_id = ?",
                   [(crm.fmt(at), r["user_id"]) for r in rows])
    db.commit()
    log(f"已發急件提醒：{len(rows)} 位")


# ── 4. 每日早報 ─────────────────────────────────────────

def morning_report_text(db, at):
    today = at.strftime("%Y-%m-%d")
    yesterday = (at - timedelta(days=1)).date()
    waiting = crm.waiting_customers(db, at)
    urgent = [c for c in waiting if c["status"] == clf.URGENT]
    oldest = max((c["waited"] or 0 for c in waiting), default=0)
    due = db.execute("SELECT COUNT(*) FROM customers WHERE blocked = 0 AND status = 'follow'"
                     " AND follow_up_at <= ?", (today,)).fetchone()[0]
    people = crm.staff_summary(db, yesterday, yesterday)
    unsynced = db.execute("SELECT COUNT(*) FROM actions WHERE ragic_synced_at IS NULL").fetchone()[0]

    lines = [f"☀️ *LINE 客服早報* {today}（週{WEEKDAYS[at.weekday()]}）"]
    if waiting:
        lines.append(f"待回覆：術後關懷 {len(urgent)} 位・需要回覆 {len(waiting) - len(urgent)} 位"
                     f"（最久已等 {crm.human_minutes(oldest)}）")
    else:
        lines.append("待回覆：0 位 👍")
    lines.append(f"今天該追蹤：{due} 位")
    handled = [p for p in people if p["handled"]]
    if handled:
        total = sum(p["handled"] for p in handled)
        detail = "、".join(f"{p['name']} {p['handled']}" for p in handled)
        lines.append(f"昨天處理 {total} 位：{detail}")
        waits = [w for p in handled for w in p["waits"]]
        if waits:
            waits.sort()
            lines.append(f"昨天回覆等待中位數：{crm.human_minutes(waits[len(waits) // 2])}")
    else:
        lines.append("昨天沒有處理紀錄")
    if ragic.enabled(ragic.LOG_SHEET) and unsynced:
        lines.append(f"⚠️ Ragic 還有 {unsynced} 筆沒同步，請檢查")
    if PUBLIC_URL:
        lines.append(f"<{link('/')}|打開看板>・<{link('/report')}|處理報表>")
    return "\n".join(lines)


def morning_report(db, at):
    if not notify.enabled():
        return
    today = at.strftime("%Y-%m-%d")
    # 只在早報時間到中午之間發；Mac 那天早上沒開機就跳過，不會半夜才補發「早報」
    if not (MORNING_REPORT_TIME <= at.strftime("%H:%M") < "12:00") or kv_get(db, "morning_report") == today:
        return
    notify.send_chat(morning_report_text(db, at))
    kv_set(db, "morning_report", today)
    log("已發每日早報")


def run(at=None):
    at = at or crm.now()
    db = crm.connect()
    ok = True
    try:
        for job in (sync_staff, lambda d, _a: push_ragic(d), urgent_alerts, morning_report):
            try:
                job(db, at)
            except Exception:
                ok = False
                log("工作失敗：\n" + traceback.format_exc())
    finally:
        db.close()
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
