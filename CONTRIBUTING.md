# Contributing

## Setup

Engine (Python 3.10+):

```bash
cd server
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install pytest
python -m pytest tests/ -q
```

Desktop (needs bun):

```bash
cd apps/agentic-desktop
bun install
AGENTIC_LOCAL_URL=http://127.0.0.1:7860 bun run dev:agentic
```

## Rules

- Engine changes need tests: `server/tests/` runs hermetic (no model,
  network, tools, or PDK scan). Fixtures must be probed live before pinning.
- Import direction in `server/src/agentic_server/` is one-way (see
  `docs/architecture.md`). No new cycles.
- No new cloud dependencies in the engine. It runs offline except for
  the user's own model API calls.
- Keep it terse: no marketing copy in code or docs, no ASCII banners,
  no commented-out code.
- CI (`ci.yml`) must stay green: pytest, boot smoke, oxlint.
- API changes require regenerating `docs/openapi/engine.json` in a
  pinned env (`pip install -r server/requirements.txt`, then
  `server/scripts/export_openapi.py`) — the freshness gate compares
  byte-for-byte, and unpinned FastAPI/Pydantic render different schemas.

## PRs

Small, one concern per PR. Describe behavior change + how you verified it
(tests, live boot log, or WSL run). Update `docs/architecture.md` if you
change a structural rule.
