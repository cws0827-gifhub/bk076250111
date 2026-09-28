#!/bin/bash
# 由 launchd 呼叫：載入 .env 後啟動網頁服務
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
set -a; source .env; set +a
exec "$APP_DIR/.venv/bin/gunicorn" -w 1 --threads 4 -b "0.0.0.0:${PORT:-8765}" \
  --access-logfile - app:app
