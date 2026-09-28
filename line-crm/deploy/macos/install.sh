#!/bin/bash
# 在 Mac mini 上執行一次：建立 Python 環境，並註冊三個 launchd 排程
#   com.meizhiyao.line-crm         開機自動啟動網頁服務，當掉自動重啟
#   com.meizhiyao.line-crm-backup  每天凌晨 3:15 備份資料庫
#   com.meizhiyao.line-crm-jobs    每分鐘：同步員工名單、寫入 Ragic、急件提醒、每日早報
# 重跑也安全（會先卸載舊的再重新載入）。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/line-crm"

cd "$APP_DIR"
if [ ! -f .env ]; then
  echo "請先 cp .env.example .env 並填好金鑰與密碼"; exit 1
fi
if ! grep -q '^DASHBOARD_PASSWORD=.\+' .env; then
  echo "請先在 .env 設定 DASHBOARD_PASSWORD"; exit 1
fi

echo "▸ 建立 Python 環境"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
chmod +x deploy/macos/*.sh

mkdir -p "$AGENTS" "$LOG_DIR"

write_plist() {  # $1=label $2=script $3=額外設定
  cat > "$AGENTS/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$APP_DIR/deploy/macos/$2</string></array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>StandardOutPath</key><string>$LOG_DIR/$1.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$1.log</string>
$3
</dict>
</plist>
PLIST
  local domain="gui/$(id -u)"
  launchctl bootout "$domain/$1" 2>/dev/null || true
  # macOS 卸載是非同步的，等舊的服務真的消失再載入，否則會出現 Bootstrap failed: 5
  for _ in $(seq 1 20); do
    launchctl print "$domain/$1" >/dev/null 2>&1 || break
    sleep 0.5
  done
  for attempt in 1 2 3; do
    if launchctl bootstrap "$domain" "$AGENTS/$1.plist" 2>/dev/null; then
      echo "▸ 已載入 $1"
      return 0
    fi
    sleep 2
  done
  echo "❌ 無法載入 $1，請手動執行：launchctl bootstrap $domain $AGENTS/$1.plist"
  return 1
}

write_plist com.meizhiyao.line-crm run.sh \
"  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>"

write_plist com.meizhiyao.line-crm-backup backup.sh \
"  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>15</integer></dict>"

write_plist com.meizhiyao.line-crm-jobs jobs.sh \
"  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>"

PORT="$(grep '^PORT=' .env | cut -d= -f2)"
echo
echo "✅ 完成。管理頁：http://$(scutil --get LocalHostName 2>/dev/null || hostname).local:${PORT:-8765}"
echo "   紀錄檔：$LOG_DIR"
echo "   手動測試備份：bash deploy/macos/backup.sh"
