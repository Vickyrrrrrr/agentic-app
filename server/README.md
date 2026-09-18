# AgentIC engine (Python backend)

Local-first VLSI agent runtime. No accounts, no license server, no billing —
it runs entirely on your machine next to your EDA tools and PDKs.

## Run (dev)

```bash
./run.sh            # foreground, like `virtuoso &`
./run.sh &          # background
```

Needs Python 3.10+ and `pip install -r requirements.txt` (run.sh does it).
Serves `http://localhost:7860` (`AGENTIC_PORT` overrides). Health:
`GET /health`, bridge: `GET /opencode/bridge/health`.

## Importable package

```bash
pip install -e .            # editable install of `agentic_server`
python -c "from agentic_server import main"
```

`server/main.py` is a thin shim kept for the desktop launcher and run.sh;
all code lives in `src/agentic_server/`.

## Test

```bash
python -m pytest tests/ -q
```

## Layout rules (`src/agentic_server/`)

- `api/*` (today: `main.py`) → may import agent, vlsi, runtime, schemas
- `agent/*` → may import vlsi, runtime, schemas (never api)
- `vlsi/*` → schemas only (+ stdlib; pydantic for lint)
- `runtime/*` → may import vlsi, schemas (never agent/api)
