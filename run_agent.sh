#!/usr/bin/env bash
# TSM pipeline: parse → analyse → snapshot
# Logs all output with timestamps to ~/tsm-agent/logs/agent.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$HOME/tsm-agent/logs"
LOG_FILE="$LOG_DIR/agent.log"
PYTHON=/usr/bin/python3

mkdir -p "$LOG_DIR"

# Prefix every line of output with a UTC timestamp
ts() { while IFS= read -r line; do printf '[%s] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$line"; done; }

{
    echo "════════════════════════════════════════════════════════════"
    echo "  TSM AGENT RUN — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "════════════════════════════════════════════════════════════"

    echo "--- [1/3] tsm_parser.py"
    "$PYTHON" "$SCRIPT_DIR/tsm_parser.py" 2>&1

    echo "--- [2/3] agent.py"
    "$PYTHON" "$SCRIPT_DIR/agent.py" 2>&1

    echo "--- [3/4] arbitrage.py"
    "$PYTHON" "$SCRIPT_DIR/arbitrage.py" 2>&1

    echo "--- [4/4] generate_dashboard.py"
    "$PYTHON" "$SCRIPT_DIR/generate_dashboard.py" 2>&1

    echo "--- done"
} 2>&1 | ts | tee -a "$LOG_FILE"
