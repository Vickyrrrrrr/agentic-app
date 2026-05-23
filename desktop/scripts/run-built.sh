#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$ROOT/.." && pwd)"
cd "$ROOT"

pkill -f "$ROOT/node_modules/electron/dist/electron" 2>/dev/null || true
pkill -f "$ROOT/node_modules/.bin/electron-vite" 2>/dev/null || true
pkill -f "$REPO_ROOT/web/node_modules/vite/bin/vite.js" 2>/dev/null || true
pkill -f "$REPO_ROOT/web/node_modules/.vite" 2>/dev/null || true

unset ELECTRON_RUN_AS_NODE

npm run build
exec "$ROOT/node_modules/.bin/electron" . --no-sandbox
