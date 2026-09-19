import { spawn, type ChildProcess } from "node:child_process"
import { chmodSync, existsSync } from "node:fs"
import { get as httpGet } from "node:http"
import { createServer } from "node:net"
import { homedir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { app } from "electron"

import { write as writeLog } from "./logging"

const DEFAULT_AGENTIC_URL = "http://127.0.0.1:7860"
const HEALTH_PATH = "/health"
const BRIDGE_HEALTH_PATH = "/opencode/bridge/health"

let backendProcess: ChildProcess | null = null
let started = false

// On Linux and macOS the backend is always "native" — EDA tools live on the user's PATH.
export type BackendMode = "linux" | "macos" | "dev" | "unknown"
let backendMode: BackendMode = process.platform === "linux" ? "linux" : process.platform === "darwin" ? "macos" : "dev"

export type BackendStatus = {
  mode: BackendMode
  started: boolean
  ready: boolean
  degraded: boolean
  message: string
  url: string
}

let backendStatus: BackendStatus = {
  mode: backendMode,
  started: false,
  ready: false,
  degraded: false,
  message: "Backend has not started yet.",
  url: DEFAULT_AGENTIC_URL,
}

export function getBackendMode(): BackendMode {
  return backendMode
}

export function getBackendStatus(): BackendStatus {
  return { ...backendStatus }
}

export function getAgenticBackendUrl() {
  return process.env.AGENTIC_LOCAL_URL || DEFAULT_AGENTIC_URL
}

export async function configureAgenticBackendUrl(conflictingUrl: string) {
  let url = process.env.AGENTIC_LOCAL_URL || DEFAULT_AGENTIC_URL
  if (sameLoopbackOrigin(url, conflictingUrl)) {
    url = await nextManagedBackendUrl(conflictingUrl)
  }
  process.env.AGENTIC_LOCAL_URL = url
  backendStatus = { ...backendStatus, url }
  return url
}

export async function startAgenticBackend() {
  let baseUrl = getAgenticBackendUrl()
  backendStatus = { ...backendStatus, url: baseUrl, started: false, ready: false }

  // If backend is already running (dev mode), skip spawning.
  if (await isHealthy(baseUrl)) {
    writeLog("agentic-backend", "Backend already running at", { url: baseUrl })
    started = true
    backendStatus = {
      ...backendStatus,
      mode: backendMode,
      started: true,
      ready: true,
      degraded: false,
      message: "AgentIC backend is already running.",
    }
    if (await isBridgeHealthy(baseUrl)) {
      writeLog("agentic-backend", "Bridge is healthy — backend fully ready")
    }
    return
  }

  // If target port (e.g. 7860) is occupied by another non-AgentIC process, find next free port
  const preferredPort = Number(portFromLocalUrl(baseUrl)) || 7860
  const freePort = await findFreeLoopbackPort(preferredPort)
  if (freePort !== preferredPort) {
    baseUrl = `http://127.0.0.1:${freePort}`
    process.env.AGENTIC_LOCAL_URL = baseUrl
    process.env.AGENTIC_PORT = String(freePort)
    backendStatus = { ...backendStatus, url: baseUrl }
    writeLog("agentic-backend", "Preferred port was occupied; auto-assigned free port", { freePort, url: baseUrl })
  }

  // Resolve backend candidates in order: bundled binary → run.sh → main.py.
  // Each is tried until one becomes healthy (the binary can fail to start
  // on hosts it wasn't built for — older glibc, missing loader deps).
  const env = backendEnvironment()
  const commands = resolveBackendCommands()
  if (commands.length === 0) {
    writeLog("agentic-backend", "No backend command found. Start the backend manually.", {}, "warn")
    backendStatus = {
      ...backendStatus,
      mode: "unknown",
      started: false,
      ready: false,
      degraded: true,
      message: "AgentIC backend could not be started. Reinstall AgentIC or run the backend manually.",
    }
    return
  }

  for (const command of commands) {
    if (await tryStartBackend(command, baseUrl, env)) return
    writeLog("agentic-backend", "Backend candidate failed, trying next", { executable: command.executable }, "warn")
  }

  writeLog("agentic-backend", "No backend candidate became ready", { url: baseUrl, mode: backendMode }, "warn")
  backendStatus = {
    ...backendStatus,
    mode: backendMode,
    started,
    ready: false,
    degraded: true,
    message: "AgentIC backend started but did not become ready. Check backend logs for startup errors.",
  }
}

async function tryStartBackend(
  command: BackendCommand,
  baseUrl: string,
  env: NodeJS.ProcessEnv,
): Promise<boolean> {
  writeLog("agentic-backend", "Spawning backend", { mode: backendMode, executable: command.executable, args: command.args.join(" ") })

  let child: ChildProcess
  try {
    child = spawn(command.executable, command.args, {
      env,
      cwd: command.cwd,
      shell: command.shell,
      stdio: ["ignore", "pipe", "pipe"],
    })
  } catch (error) {
    writeLog("agentic-backend", "Failed to spawn backend", { error: String(error) }, "error")
    return false
  }
  backendProcess = child
  started = true
  backendStatus = {
    ...backendStatus,
    mode: backendMode,
    started: true,
    ready: false,
    degraded: false,
  }
  attachProcessLogging(child)

  // A binary that cannot run here (missing loader deps, bad arch) exits
  // almost immediately — fail fast instead of burning the full timeout.
  let exitedEarly = false
  child.once("exit", () => {
    exitedEarly = true
  })

  // Wait up to ~40s for the backend to become ready
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (exitedEarly || backendProcess !== child) return false
    if (await isBridgeHealthy(baseUrl)) {
      writeLog("agentic-backend", "Backend ready", { url: baseUrl, mode: backendMode })
      backendStatus = {
        ...backendStatus,
        mode: backendMode,
        started: true,
        ready: true,
        degraded: false,
        message: "AgentIC backend is running.",
      }
      return true
    }
    if (await isHealthy(baseUrl)) {
      writeLog("agentic-backend", "Backend HTTP ready (waiting for bridge)", { url: baseUrl })
    }
    await new Promise((resolve) => setTimeout(resolve, 500))
  }

  writeLog("agentic-backend", "Backend candidate did not become ready within timeout", { url: baseUrl, mode: backendMode }, "warn")
  try {
    child.kill("SIGKILL")
  } catch {}
  if (backendProcess === child) backendProcess = null
  return false
}


export function stopAgenticBackend() {
  if (!backendProcess) return
  const current = backendProcess
  backendProcess = null
  try {
    if (current.pid) process.kill(-current.pid, "SIGKILL")
    else current.kill("SIGKILL")
  } catch {
    try { current.kill() } catch {}
  }
}


type BackendCommand = {
  executable: string
  args: string[]
  cwd: string
  shell?: boolean
}

function resolveBackendCommands(): BackendCommand[] {
  const commands: BackendCommand[] = []
  // 1. Frozen production binary (Linux AppImage / packaged builds).
  //    No system python, no pip, no network needed at first launch.
  const bundled = findBundledBackend()
  if (bundled) {
    backendMode = process.platform === "linux" ? "linux" : process.platform === "darwin" ? "macos" : "dev"
    writeLog("agentic-backend", "Using bundled backend binary", { executable: bundled.executable })
    commands.push({ executable: bundled.executable, args: [], cwd: bundled.cwd })
  }

  // 2-3. System python launcher, then bare main.py. These double as the
  // rescue path when the frozen binary cannot start on the host
  // (older glibc, missing loader deps) — startAgenticBackend tries each
  // candidate in order until one becomes healthy.
  const serverDir = findServerDir()
  if (!serverDir) return commands

  const runScript = join(serverDir, "run.sh")
  const mainScript = join(serverDir, "main.py")

  if (existsSync(runScript)) {
    backendMode = process.platform === "linux" ? "linux" : process.platform === "darwin" ? "macos" : "dev"
    writeLog("agentic-backend", "Using system python backend launcher", { runScript, serverDir })
    commands.push({ executable: "bash", args: [runScript], cwd: serverDir })
  }

  if (existsSync(mainScript)) {
    backendMode = "dev"
    writeLog("agentic-backend", "Using python3 main.py directly", { mainScript, serverDir })
    commands.push({ executable: "python3", args: [mainScript], cwd: serverDir })
  }
  return commands
}

function findBundledBackend(): { executable: string; cwd: string } | undefined {
  const binaryName = process.platform === "win32" ? "agentic-backend.exe" : "agentic-backend"
  const candidates = app.isPackaged
    ? [join(process.resourcesPath, "backend", "agentic-backend", binaryName)]
    : [join(app.getAppPath(), "resources", "backend", "agentic-backend", binaryName)]

  for (const executable of candidates) {
    if (!existsSync(executable)) continue
    try {
      // Transports (zips/tarballs) can strip the exec bit; restore it.
      chmodSync(executable, 0o755)
    } catch {
      // Best effort — spawn will surface real permission errors.
    }
    return { executable, cwd: dirname(executable) }
  }
  return undefined
}

function findServerDir() {
  const roots = [
    join(process.resourcesPath, "server"),
    process.cwd(),
    app.getAppPath(),
    dirname(fileURLToPath(import.meta.url)),
    resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../.."),
  ]

  for (const root of roots) {
    let current = resolve(root)
    for (let depth = 0; depth < 8; depth += 1) {
      if (existsSync(join(current, "main.py"))) return current
      const candidate = join(current, "server")
      if (existsSync(join(candidate, "main.py"))) return candidate
      const parent = dirname(current)
      if (parent === current) break
      current = parent
    }
  }
}

function backendEnvironment(): NodeJS.ProcessEnv {
  // Local mode: the Python backend needs no license server or billing
  // configuration. Only workspace + port are forwarded.
  const localUrl = process.env.AGENTIC_LOCAL_URL || DEFAULT_AGENTIC_URL
  const localPort = portFromLocalUrl(localUrl)

  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    AGENTIC_WORKSPACE: process.env.AGENTIC_WORKSPACE || join(homedir(), "AgentIC-workspace"),
    AGENTIC_PORT: localPort || process.env.AGENTIC_PORT || "7860",
  }

  return env
}

function attachProcessLogging(child: ChildProcess) {
  child.stdout?.on("data", (data: Buffer) => writeLog("agentic-backend", data.toString().trimEnd()))
  child.stderr?.on("data", (data: Buffer) => writeLog("agentic-backend", data.toString().trimEnd(), {}, "warn"))
  // Without this, a spawn failure (bad path, permissions) throws an
  // uncaught 'error' event and takes down the main process.
  child.on("error", (error) => {
    writeLog("agentic-backend", "AgentIC backend process error", { error: String(error) }, "error")
    if (backendProcess === child) backendProcess = null
  })
  child.on("exit", (code, signal) => {
    writeLog("agentic-backend", "AgentIC backend exited", { code, signal }, code === 0 ? "info" : "warn")
    if (backendProcess === child) backendProcess = null
  })
}

function isHealthy(baseUrl: string) {
  return getJsonHealth(baseUrl, HEALTH_PATH, () => true)
}

function isBridgeHealthy(baseUrl: string) {
  return getJsonHealth(baseUrl, BRIDGE_HEALTH_PATH, (body) => body?.bridge === true)
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function getJsonHealth(baseUrl: string, path: string, validate: (body: any) => boolean) {
  return new Promise<boolean>((resolve) => {
    const request = httpGet(`${baseUrl.replace(/\/$/, "")}${path}`, (response) => {
      let data = ""
      response.setEncoding("utf8")
      response.on("data", (chunk) => {
        data += chunk
      })
      response.on("end", () => {
        if (!response.statusCode || response.statusCode < 200 || response.statusCode >= 300) {
          resolve(false)
          return
        }
        try {
          resolve(validate(data ? JSON.parse(data) : {}))
        } catch {
          resolve(false)
        }
      })
    })
    request.on("error", () => resolve(false))
    request.setTimeout(1000, () => {
      request.destroy()
      resolve(false)
    })
  })
}

function isManagedLoopback(value: string) {
  try {
    const url = new URL(value)
    return url.hostname === "127.0.0.1" || url.hostname === "localhost" || url.hostname === "::1"
  } catch {
    return false
  }
}

function sameLoopbackOrigin(left: string, right: string) {
  try {
    const a = new URL(left)
    const b = new URL(right)
    return (
      isManagedLoopback(left) &&
      isManagedLoopback(right) &&
      a.protocol === b.protocol &&
      a.hostname === b.hostname &&
      portFromLocalUrl(left) === portFromLocalUrl(right)
    )
  } catch {
    return false
  }
}

async function nextManagedBackendUrl(baseUrl: string) {
  const preferred = portFromLocalUrl(baseUrl)
  const start = preferred ? Number(preferred) + 1 : 7861
  const port = await findFreeLoopbackPort(start)
  return `http://127.0.0.1:${port}`
}

function portFromLocalUrl(value: string) {
  try {
    const url = new URL(value)
    return url.port || (url.protocol === "https:" ? "443" : "80")
  } catch {
    return ""
  }
}

function findFreeLoopbackPort(start: number) {
  return new Promise<number>((resolve, reject) => {
    const tryPort = (port: number) => {
      const server = createServer()
      server.once("error", () => {
        server.close()
        tryPort(port + 1)
      })
      server.listen(port, "127.0.0.1", () => {
        server.close((error) => {
          if (error) {
            reject(error)
            return
          }
          resolve(port)
        })
      })
    }
    tryPort(start)
  })
}
