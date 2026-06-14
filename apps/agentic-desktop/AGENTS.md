# AgentIC Desktop Development Notes

This tree is a trimmed OpenCode-derived runtime shell embedded inside AgentIC.

Keep these boundaries sharp:

- Do not duplicate VLSI intelligence in desktop TypeScript.
- Route PDK, IP, tool, report, and write-policy decisions through the AgentIC backend bridge.
- Keep OpenCode-style runtime mechanics: sessions, project folders, tool calls, streaming events, file tree, editor, and terminal UX.
- Keep UI changes minimal, dense, and workbench-like. Avoid marketing screens inside the desktop workbench.
- Preserve license and model-key checks through AgentIC product services.

Useful local commands:

```bash
bun run dev:agentic
bun run build:desktop
bun run package:desktop
```

Before changing package structure, verify that all `workspace:*` dependencies still point at packages kept in this tree.
