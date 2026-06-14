- [x] Read all source files
- [x] 1. main/index.ts — handle agentic://auth-callback deep link and forward to renderer
- [x] 2. main/index.ts — handle startup deep links from process.argv for first instance
- [x] 3. renderer/index.tsx — LicenseGate paywall and auth pages (already fully implemented in codebase)
- [x] 4. IPC handlers (get-license-status, save-desktop-session) — determined to be redundant as renderer communicates directly with local backend via fetch.

