#!/usr/bin/env bash
# AgentIC server launcher (Linux + WSL; macOS works).
# ./run.sh    foreground | ./run.sh &   background
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Python version check ──────────────────────────────────────────────────────
_check_python() {
  if ! command -v python3 &>/dev/null; then
    echo "❌  python3 not found. Install Python 3.8+ and try again."
    exit 1
  fi
  local ver
  ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  local major minor
  major="$(echo "$ver" | cut -d. -f1)"
  minor="$(echo "$ver" | cut -d. -f2)"
  if [[ "$major" -lt 3 || ("$major" -eq 3 && "$minor" -lt 8) ]]; then
    echo "❌  Python $ver found, but AgentIC requires Python 3.8+."
    echo "    On RHEL/CentOS: sudo dnf install python3.11"
    echo "    On Ubuntu:      sudo apt install python3.11"
    exit 1
  fi
}

_check_python

# ── Dependencies ──────────────────────────────────────────────────────────────
pip3 install -q -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null \
  || pip install -q -r "$SCRIPT_DIR/requirements.txt"

# ── Start server ──────────────────────────────────────────────────────────────
REQUESTED_PORT="${AGENTIC_PORT:-${PORT:-7860}}"

AGENTIC_PORT="$(python3 -c "
import socket
port = int('$REQUESTED_PORT')
for p in range(port, port + 100):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('0.0.0.0', p))
            print(p)
            break
        except OSError:
            continue
" 2>/dev/null || echo "$REQUESTED_PORT")"
export AGENTIC_PORT

if [ "$AGENTIC_PORT" != "$REQUESTED_PORT" ]; then
  echo "[port] Requested port $REQUESTED_PORT was in use. Using next available free port: $AGENTIC_PORT"
fi

echo ""
echo "→ AgentIC starting at http://localhost:${AGENTIC_PORT}"
echo "  (like \`virtuoso &\` — Ctrl-C or kill to stop)"
echo ""

# ── Auto-open browser ─────────────────────────────────────────────────────────
# Non-blocking: opens the UI in the default browser after a short delay
# so the server has time to start first.
_open_browser() {
  local url="http://localhost:${AGENTIC_PORT}"
  sleep 2
  if command -v xdg-open &>/dev/null; then
    xdg-open "$url" &>/dev/null &   # Linux / WSL (if DISPLAY or WAYLAND_DISPLAY set)
  elif command -v open &>/dev/null; then
    open "$url" &>/dev/null &       # macOS
  fi
}

# Only auto-open if we have a display (skip in headless CI/SSH without X)
if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ] || [ "$(uname)" = "Darwin" ]; then
  _open_browser &
fi

cd "$SCRIPT_DIR"
exec python3 main.py
