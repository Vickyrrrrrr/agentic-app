// WSL connections helper — removed for Linux and macOS.
export function readyWslConnections() {
  return []
}

export function availableStartupServer(defaultServer: string | null | undefined) {
  return defaultServer ?? "sidecar"
}
