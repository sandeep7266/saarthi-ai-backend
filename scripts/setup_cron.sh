#!/bin/bash
# Saarthi-AI nightly cron setup — Linux/Mac
set -e

API_URL="${SAARTHI_API_URL:-https://saarthi-ai-api.railway.app}"
CRON_SECRET="${CRON_SECRET:-}"
LOG_FILE="/var/log/saarthi_cron.log"

if [ -z "$CRON_SECRET" ]; then
  echo "ERROR: export CRON_SECRET=your_secret  then run again."
  exit 1
fi

CRON_CMD="0 0 * * * curl -s -X POST '${API_URL}/api/v1/cron/run-daily-sync' -H 'X-Cron-Secret: ${CRON_SECRET}' >> ${LOG_FILE} 2>&1"

crontab -l 2>/dev/null | grep -v "run-daily-sync" | crontab - 2>/dev/null || true
(crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -

echo "Cron installed — runs at midnight daily."
echo "Test: curl -X POST '${API_URL}/api/v1/cron/run-daily-sync' -H 'X-Cron-Secret: ${CRON_SECRET}'"
