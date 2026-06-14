# AgentIC Desktop Fork

AgentIC Desktop is an OpenCode-derived desktop/runtime shell for AgentIC's VLSI agent.

The fork keeps the mature agent-IDE mechanics:

- project folder selection
- sessions and resumable conversations
- file tree, editor tabs, review flow, and terminal panels
- streaming agent/tool events
- OpenCode-compatible tool/plugin runtime

AgentIC owns the differentiating silicon layer:

- VLSI planning and approval policy
- PDK/IP/tool/license discovery
- SRAM/macro/library/corner awareness
- RTL, testbench, synthesis, PnR, STA, DRC/LVS, and signoff report reasoning
- local AgentIC backend bridge and license gate

## Local Runtime Contract

Run the AgentIC backend first:

```bash
cd /home/vickynishad/AgentIC-app/server
AGENTIC_LICENSE_BYPASS=1 ./run.sh
```

Run this desktop fork in development mode:

```bash
cd /home/vickynishad/AgentIC-desktop
AGENTIC_LOCAL_URL=http://127.0.0.1:7860 AGENTIC_MODE=advisor bun run dev:agentic
```

Builder mode is explicit:

```bash
AGENTIC_MODE=builder bun run dev:agentic
```

## Product Boundary

Do not move the current AgentIC Python VLSI kernel into this fork as ad hoc TypeScript.

This repo is the IDE/runtime shell. The AgentIC backend remains the source of truth for:

- VLSI capability graph
- tool adapters
- license state
- design-state ledger
- report parsers and repair loops
- workspace write policy

## First Stable Target

The first stable AgentIC Desktop release should:

1. default to the `agentic-vlsi` agent
2. connect to the local AgentIC backend
3. expose OpenCode-quality project/session/file/editor UX
4. keep chat compact and put large artifacts into workspace files
5. route VLSI-specific choices through AgentIC bridge tools
