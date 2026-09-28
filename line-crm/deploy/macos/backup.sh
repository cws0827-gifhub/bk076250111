#!/bin/bash
# 每晚備份資料庫：先在 Mac mini 本機做一份，再複製到 Synology。
# 用 SQLite 的線上備份，程式運作中也能安全複製，不會拿到寫一半的檔案。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
set -a; source .env; set +a

DB="${DB_PATH:-$APP_DIR/line_crm.db}"
LOCAL_DIR="${LOCAL_BACKUP_DIR:-$HOME/line-crm-backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
STAMP="$(date +%Y%m%d-%H%M)"
FILE="line_crm-$STAMP.db"

log() { echo "$(date '+%F %T') $*"; }

mkdir -p "$LOCAL_DIR"
"$APP_DIR/.venv/bin/python" - "$DB" "$LOCAL_DIR/$FILE" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close(); src.close()
if ok != "ok":
    sys.exit(f"備份檔檢查失敗：{ok}")
PY
gzip -f "$LOCAL_DIR/$FILE"
log "本機備份完成：$LOCAL_DIR/$FILE.gz"
find "$LOCAL_DIR" -name 'line_crm-*.db.gz' -mtime +"$KEEP_DAYS" -delete

# ── 複製到 Synology ───────────────────────────────
if [ -z "${NAS_BACKUP_DIR:-}" ]; then
  log "未設定 NAS_BACKUP_DIR，只保留本機備份"
  exit 0
fi

# 共用資料夾沒掛載時，嘗試用鑰匙圈裡存的帳密自動掛載
if [ ! -d "$NAS_BACKUP_DIR" ] && [ -n "${NAS_SMB_URL:-}" ]; then
  osascript -e "mount volume \"$NAS_SMB_URL\"" >/dev/null 2>&1 || true
  sleep 5
fi

if [ ! -d "$NAS_BACKUP_DIR" ]; then
  log "❌ 找不到 NAS 資料夾 $NAS_BACKUP_DIR（NAS 沒開機或共用資料夾沒掛載）"
  exit 1
fi

cp "$LOCAL_DIR/$FILE.gz" "$NAS_BACKUP_DIR/"
find "$NAS_BACKUP_DIR" -name 'line_crm-*.db.gz' -mtime +"$KEEP_DAYS" -delete
log "已複製到 NAS：$NAS_BACKUP_DIR/$FILE.gz"
