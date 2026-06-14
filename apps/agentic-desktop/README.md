# AgentIC Desktop

AgentIC Desktop is the OpenCode-class IDE/runtime shell for AgentIC's VLSI agent.

This tree keeps the pieces required for a local agent workbench:

- project folder selection
- persistent sessions
- chat, planning, and tool-event streaming
- file tree, editor tabs, review, and terminal panels
- AgentIC VLSI bridge tools for PDK, IP, flow, reports, and workspace actions

The silicon intelligence remains in the AgentIC backend. This desktop shell should call the backend instead of duplicating VLSI policy in TypeScript.

## Local Development

Start the AgentIC backend:

```bash
cd /home/vickynishad/AgentIC-app/server
AGENTIC_LICENSE_BYPASS=1 ./run.sh
```

Start the desktop shell:

```bash
cd /home/vickynishad/AgentIC-app/apps/agentic-desktop
AGENTIC_LOCAL_URL=http://127.0.0.1:7860 AGENTIC_MODE=advisor bun run dev:agentic
```

Use builder mode only when file writes and tool execution should be allowed:

```bash
AGENTIC_MODE=builder bun run dev:agentic
```

## Architecture Boundary

AgentIC Desktop owns the IDE surface and agent-runtime bridge.

AgentIC backend owns:

- license checks
- VLSI capability graph
- tool and PDK adapters
- design-state ledger
- report parsers
- repair policies
- workspace write policy

That boundary keeps the product from becoming a thin prompt wrapper while still reusing a mature agent harness.
