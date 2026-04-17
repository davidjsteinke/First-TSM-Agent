#!/usr/bin/env bash
# Refresh Live AH data: fetch all 5 realms, filter, save to live_ah.db,
# regenerate the Live AH dashboard tab.
# Called by tsm-live-ah.timer every 5 minutes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/live_ah.log"
PYTHON=/usr/bin/python3

mkdir -p "$LOG_DIR"

ts() { while IFS= read -r line; do printf '[%s] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$line"; done; }

{
    echo "════════════════════════════════════"
    echo "  LIVE AH REFRESH — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "════════════════════════════════════"

    echo "--- Fetching AH data + saving to live_ah.db"
    "$PYTHON" "$SCRIPT_DIR/run_live_ah_refresh.py" 2>&1

    echo "--- Regenerating dashboard (Live AH tab)"
    "$PYTHON" "$SCRIPT_DIR/generate_dashboard.py" 2>&1

    echo "--- done"
} 2>&1 | ts | tee -a "$LOG_FILE"
