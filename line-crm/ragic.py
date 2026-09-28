"""Ragic 串接：寫入客服處理紀錄、查詢／讀取顧客資料、同步員工名單。

API Key 放在 .env 的 RAGIC_API_KEY，或檔案 RAGIC_KEY_FILE（預設 ~/.ragic_key）。
這個 repo 是公開的：帳號、表單路徑也只寫在 .env，程式裡不放任何實際值。
"""

import json
import os
import urllib.parse
import urllib.request

SERVER = os.environ.get("RAGIC_SERVER", "ap10.ragic.com")
ACCOUNT = os.environ.get("RAGIC_ACCOUNT", "")
LOG_SHEET = os.environ.get("RAGIC_LOG_SHEET", "")            # 例：employee-zone/20
CUSTOMER_SHEET = os.environ.get("RAGIC_CUSTOMER_SHEET", "")  # 例：forms1/1
STAFF_SHEET = os.environ.get("RAGIC_STAFF_SHEET", "")        # 例：forms1/2

# 綁定顧客後，在客人頁面顯示的欄位（只讀，不會修改 Ragic 的顧客資料）
CUSTOMER_FIELDS = [
    "上次消費或來店日", "最後一治療的項目", "最後一次購買的項目", "顧客消費次數",
    "顧客累積消費", "未消耗金額（需驗證）", "負責同仁", "客戶分群", "顧客在意問題/部位",
]


class RagicError(Exception):
    pass


def api_key():
    key = os.environ.get("RAGIC_API_KEY", "").strip()
    if key:
        return key
    path = os.path.expanduser(os.environ.get("RAGIC_KEY_FILE", "~/.ragic_key"))
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def enabled(sheet):
    if sheet and sheet == LOG_SHEET and not os.environ.get("RAGIC_LOG_FIELD_IDS"):
        return False
    return bool(ACCOUNT and sheet and api_key())


def _request(sheet, record_id=None, params=None, body=None, timeout=10):
    path = f"/{ACCOUNT}/{sheet}" + (f"/{record_id}" if record_id else "")
    query = "api&v=3" + ("&" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://{SERVER}{path}?{query}", data=data, method="POST" if data else "GET",
        headers={"Authorization": f"Basic {api_key()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.load(resp)
    except Exception as e:  # 網路、逾時、非 JSON
        raise RagicError(str(e)) from e
    if isinstance(result, dict) and result.get("status") == "ERROR":
        raise RagicError(result.get("msg") or json.dumps(result, ensure_ascii=False)[:200])
    return result


def record_url(sheet, record_id):
    return f"https://{SERVER}/{ACCOUNT}/{sheet}/{record_id}"


# ── 客服處理紀錄 ─────────────────────────────────────────

def log_field_ids():
    """Ragic API 寫入要用欄位編號。RAGIC_LOG_FIELD_IDS 格式：時間=1000001,處理人=1000002,..."""
    pairs = (p.split("=", 1) for p in os.environ.get("RAGIC_LOG_FIELD_IDS", "").split(",") if "=" in p)
    return {name.strip(): fid.strip() for name, fid in pairs}


def push_log(fields):
    """新增一筆處理紀錄，回傳 Ragic 的 ragicId。欄位用中文名稱傳進來，這裡換成欄位編號。"""
    ids = log_field_ids()
    missing = [name for name in fields if name not in ids]
    if missing:
        raise RagicError(f"RAGIC_LOG_FIELD_IDS 缺少欄位：{'、'.join(missing)}")
    result = _request(LOG_SHEET, body={ids[name]: value for name, value in fields.items()})
    if result.get("status") != "SUCCESS":
        raise RagicError(json.dumps(result, ensure_ascii=False)[:200])
    return str(result.get("ragicId", ""))


# ── 顧客 ─────────────────────────────────────────────

def mask_phone(phone):
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) < 7:
        return "***" if digits else ""
    return f"{digits[:4]}***{digits[-3:]}"


def search_customers(q, limit=10):
    rows = _request(CUSTOMER_SHEET, params={"fts": q, "limit": limit, "subtables": 0})
    out = []
    for rid, r in (rows or {}).items():
        out.append({
            "id": str(r.get("_ragicId", rid)),
            "name": r.get("顧客姓名", ""),
            "phone": mask_phone(r.get("連絡電話1") or r.get("連絡電話2")),
            "last_visit": r.get("上次消費或來店日", ""),
            "owner": r.get("負責同仁", ""),
        })
    return out


def get_customer(record_id):
    rows = _request(CUSTOMER_SHEET, record_id=record_id, params={"subtables": 0})
    r = rows.get(str(record_id)) if isinstance(rows, dict) and str(record_id) in rows else rows
    if not isinstance(r, dict) or "顧客姓名" not in r:
        raise RagicError("找不到這位顧客")
    return {"name": r.get("顧客姓名", ""),
            "fields": [(k, r.get(k, "")) for k in CUSTOMER_FIELDS if r.get(k, "") not in ("", None)],
            "url": record_url(CUSTOMER_SHEET, record_id)}


# ── 員工名單（email → 姓名）──────────────────────────────

def fetch_staff():
    """只取在職中、有填登入 Gmail 的同仁的姓名與 email；身分證、帳戶等欄位一律不讀出。"""
    rows = _request(STAFF_SHEET, params={"limit": 1000, "subtables": 0})
    staff = []
    for r in (rows or {}).values():
        email = (r.get("登入Gmail") or "").strip().lower()
        if r.get("在職狀態") == "在職中" and "@" in email:
            staff.append({"email": email, "name": r.get("姓名", ""),
                          "role": r.get("績效系統角色") or r.get("職業類別") or ""})
    return staff
