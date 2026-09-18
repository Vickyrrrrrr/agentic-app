# AgentIC: local VLSI design agent

[![CI](https://github.com/Vickyrrrrrr/agentic-app/actions/workflows/ci.yml/badge.svg)](https://github.com/Vickyrrrrrr/agentic-app/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

AgentIC runs an AI chip-design loop on your own machine: it writes Verilog,
drives your installed EDA tools (Yosys, OpenROAD, Verilator, …), reads your
PDKs, and parses the resulting logs into structured results. No accounts,
no license server, no cloud — only your own model API calls leave the box.

## Why not a plain coding agent with a `SKILL.md`?

A `SKILL.md` tells a model *how* to run a tool. It doesn't give it the
infrastructure to survive chip design: gigabyte log outputs, failed
30-minute place-and-route runs, and PDK cells the model invents because
it never saw your library.

| Problem | Plain agent | AgentIC |
| :--- | :--- | :--- |
| **Log volume** | Dumps raw logs into context until it hallucinates. | Local parsers (`agentic_server.report_parsers`, `sta_reports`) compress logs to violations + coordinates. |
| **Failed runs** | Leaves the workspace dirty after a crash. | Per-stage checkpoints and evidence; interrupted runs are marked, resumable via the job queue. |
| **PDK grounding** | Invents cells/macros that don't exist locally. | Indexes your LEF/LIB files; the agent queries the capability graph before writing RTL. |
| **Constraint checking** | Treats tool output as plain text. | Validation schemas check budgets (slack, clock, signoff) at each stage. |

## What it does

* **RTL lint + repair:** Verilog/SystemVerilog syntax and synthesis checks against local compilers, with auto-repair.
* **STA parsing:** setup/hold violations and critical paths from timing reports.
* **Physical verification parsing:** DRC, LVS, antenna violations from tool logs.
* **PDK/capability mapping:** indexes local cell libraries so decisions match installed hardware.

## Who it's for

* Digital IC designers automating lint, constraint tuning, and debug loops.
* Hardware prototypers wanting closed-loop compiler/simulation feedback.
* EDA/CAD developers benchmarking flow configurations.

## Run it

Download the Linux AppImage from
[buildstack releases](https://github.com/Vickyrrrrrr/buildstack/releases)
— engine bundled inside, no Python setup needed. Or from source:

```bash
# Engine only (serves http://localhost:7860)
cd server && ./run.sh

# Desktop dev (needs bun)
cd apps/agentic-desktop
AGENTIC_LOCAL_URL=http://127.0.0.1:7860 bun run dev:agentic

# Tests
cd server && python -m pytest tests/ -q
```

Bring your own OpenAI-compatible model key for the agent loop.
Lint, PDK indexing, parsing, and flow routing need no model.

Docs: `docs/AGENTIC.md` (product), `docs/architecture.md` (design
record), `server/README.md` (engine), `docs/openapi/engine.json`
(generated API contract).
