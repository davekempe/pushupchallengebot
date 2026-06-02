#!/usr/bin/env bash
# Cron wrapper: runs the monitor and appends output to a log.
set -euo pipefail
cd "$(dirname "$0")"
ts="$(date '+%Y-%m-%d %H:%M:%S')"
echo "[$ts] running: pushup_monitor.py $*" >> monitor.log
python3 pushup_monitor.py "$@" >> monitor.log 2>&1
