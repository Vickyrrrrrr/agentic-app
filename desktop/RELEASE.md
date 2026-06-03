# AgentIC Desktop Release Flow

AgentIC Desktop must not require WSL, Python, or pip on a customer machine.
The Electron shell launches a bundled local backend runtime when present:

```text
resources/backend/<platform>-<arch>/agentic-backend(.exe)
```

The Python source under `resources/server` is kept as a development/fallback
path, not as the production runtime.

## Build Per Platform

Build the backend executable on the same OS/architecture you are packaging:

```bash
cd desktop
npm run build:win
npm run build:mac
npm run build:linux
```

For Windows customers, run `npm run build:win` on Windows CI or a Windows
builder so PyInstaller produces:

```text
desktop/resources/backend/win32-x64/agentic-backend.exe
```

Do not build the Windows release from WSL if you expect the backend executable
to be present. WSL can produce a Linux backend, not a Windows backend.

## Runtime Model

- Electron starts the bundled backend.
- The backend listens on `localhost:7860`.
- Workspace files stay under `~/AgentIC-workspace`.
- License, checkout, and usage-count APIs go to the AgentIC license server.
- EDA tools are discovered from the user's machine: native PATH, Docker,
  WSL if present, configured PDK environment variables, and proprietary tool
  environment variables.

WSL is optional. Users with proprietary Windows-native tools can use those
tools if they are available on PATH or configured through environment setup.

## Updates

Publish signed installers to the configured Electron Builder provider. For
GitHub releases, upload the installer plus `latest.yml` generated in `dist/`.
The app id and update channel must stay stable:

```text
appId: live.buildstack.agentic
productName: AgentIC
```

Production release checklist:

- Build on each target OS.
- Code-sign Windows/macOS artifacts.
- Verify `resources/backend/.../agentic-backend(.exe)` exists inside the package.
- Verify the app opens without Python or WSL installed.
- Verify `agentic://auth-callback` opens the installed app.
- Verify backend logs are local only under the Electron user data directory.
