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

for pdk_dir in "$HOME/.ciel" "$HOME/.pdk" /usr/share/pdk /usr/local/share/pdk; do
    if [ -d "$pdk_dir" ]; then
        echo "[scan] Found PDK directory: $pdk_dir"
        for d in "$pdk_dir"/*/; do
            [ -d "$d" ] && echo "         - $(basename "$d")"
        done
    fi
done

echo "[scan] Installing Python dependencies..."
pip3 install -q -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null || pip install -q -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "→ AgentIC Local Server starting at http://localhost:7860"
echo ""

cd "$SCRIPT_DIR"
python3 main.py
