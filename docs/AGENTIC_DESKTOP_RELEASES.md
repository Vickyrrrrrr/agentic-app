# AgentIC Desktop Release Paths

AgentIC currently has two desktop release paths in this repository.

## Current Production Desktop

Path:

```text
desktop/
web/
server/
```

Workflow:

```text
.github/workflows/release.yml
```

This is the existing React/Electron desktop app. It publishes to GitHub Releases through `electron-builder --publish always`.

Existing installed users update through this identity:

```text
appId: live.buildstack.agentic
publish: github / Vickyrrrrrr / buildstack
```

Do not change this workflow until the OpenCode-derived desktop is tested and approved as the replacement.

## OpenCode-Derived AgentIC Desktop Preview

Path:

```text
apps/agentic-desktop/
```

Workflow:

```text
.github/workflows/agentic-desktop-preview.yml
```

This builds the new AgentIC Desktop shell based on the OpenCode app/runtime architecture with `agentic-vlsi` as the default agent.

It uploads preview installers as GitHub Actions artifacts only. It does not publish releases and does not auto-update existing users.

## Promoting Preview To Production

Only after manual testing:

1. Set the OpenCode-derived app identity to match production:

```text
appId: live.buildstack.agentic
productName: AgentIC
publish: github / Vickyrrrrrr / buildstack
```

2. Bump the desktop version above the current production version.
3. Switch the production release workflow to package `apps/agentic-desktop/packages/desktop`.
4. Keep the same update feed so existing users receive the update.

Until then, the preview workflow is intentionally isolated.
