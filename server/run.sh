#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[scan] Checking environment..."

for tool in docker yosys iverilog verilator opensta openroad gtkwave make python3; do
    if command -v "$tool" &>/dev/null; then
        echo "[scan] Found $tool ✓"
    fi
done

if command -v docker &>/dev/null; then
    images=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | head -5)
    if [ -n "$images" ]; then
        echo "[scan] Docker images available:"
        echo "$images" | while read -r img; do echo "         $img"; done
    fi
fi

echo "[scan] PDK paths are discovered from PDK_ROOT, PDKPATH, PDK_HOME, or AGENTIC_PDK_SEARCH_PATHS."
IFS=':' read -r -a pdk_candidates <<< "${PDK_ROOT:-}:${PDKPATH:-}:${PDK_HOME:-}:${AGENTIC_PDK_SEARCH_PATHS:-}"
for pdk_dir in "${pdk_candidates[@]}"; do
    [ -z "$pdk_dir" ] && continue
    expanded="${pdk_dir/#\~/$HOME}"
    if [ -d "$expanded" ]; then
        echo "[scan] Found configured PDK directory: $expanded"
    fi
done

echo "[scan] Installing Python dependencies..."
pip3 install -q -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null || pip install -q -r "$SCRIPT_DIR/requirements.txt"

echo ""
AGENTIC_PORT="${AGENTIC_PORT:-${PORT:-7860}}"
export AGENTIC_PORT

echo "→ AgentIC Local Server starting at http://localhost:${AGENTIC_PORT}"
echo ""

cd "$SCRIPT_DIR"
export AGENTIC_LICENSE_STATUS_URL=${AGENTIC_LICENSE_STATUS_URL:-"https://api.buildstack.live/license/status"}
python3 main.py
