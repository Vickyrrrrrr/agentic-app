#!/usr/bin/env bash
set -euo pipefail

export AGENTIC_LOCAL_URL="${AGENTIC_LOCAL_URL:-http://127.0.0.1:7860}"
export AGENTIC_MODE="${AGENTIC_MODE:-advisor}"
export OPENCODE_DEFAULT_AGENT="${OPENCODE_DEFAULT_AGENT:-agentic-vlsi}"

cd "$(dirname "$0")/.."
bun run dev:agentic
