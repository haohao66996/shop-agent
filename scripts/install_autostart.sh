#!/bin/bash
set -e

PROJECT_DIR="/root/autodl-tmp/shop_agent"
ENV_DIR="/root/autodl-tmp/envs/shop_agent"
LOCK_FILE="/tmp/shop_agent.boot.lock"
LOG_FILE="$PROJECT_DIR/logs/autoboot.log"

CRON_LINE="@reboot ( sleep 30; until [ -d \"$ENV_DIR\" ]; do sleep 5; done; flock -n $LOCK_FILE $PROJECT_DIR/scripts/start_all.sh >> $LOG_FILE 2>&1 )"

mkdir -p "$PROJECT_DIR/logs"
(crontab -l 2>/dev/null | grep -vF "$PROJECT_DIR/scripts/start_all.sh" || true
 echo "$CRON_LINE") | crontab -

echo "AUTOSTART_INSTALLED"
crontab -l | grep -F "$PROJECT_DIR/scripts/start_all.sh"
