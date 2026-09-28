#!/bin/bash
# 由 launchd 每分鐘呼叫：同步員工名單、寫入 Ragic、急件提醒、每日早報
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
set -a; source .env; set +a
exec "$APP_DIR/.venv/bin/python" jobs.py
