import { spawn, spawnSync, type ChildProcess } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
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

export type BackendMode = "wsl" | "windows-native" | "linux" | "dev" | "unknown"
let backendMode: BackendMode = process.platform === "win32" ? "windows-native" : "linux"

export function getBackendMode(): BackendMode {
  return backendMode
}

export function getAgenticBackendUrl() {
  return process.env.AGENTIC_LOCAL_URL || DEFAULT_AGENTIC_URL
}

export async function startAgenticBackend() {
  // Python backend server is consolidated into the Hono Sidecar.
  // Bypassing spawning of the separate Python server process.
  writeLog("agentic-backend", "Python backend consolidated; spawning bypassed.")
  return
}


export function stopAgenticBackend() {
  if (!backendProcess) return
  const current = backendProcess
  backendProcess = null
  // Force-kill immediately. On Windows, kill() calls TerminateProcess (force).
  // On Linux/Mac, SIGKILL is immediate. No setTimeout — the app may exit before it fires.
  try {
    if (process.platform === "win32") {
      // Kill the entire process tree (PyInstaller may spawn child processes like wsl.exe)
      const { execFileSync } = require("node:child_process")
      execFileSync("taskkill", ["/PID", String(current.pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      })
    } else {
      current.kill("SIGKILL")
    }
  } catch {
    // Process may have already exited
    try { current.kill() } catch {}
  }
}

function resolveBackendCommand(env?: NodeJS.ProcessEnv):
  | {
      executable: string
      args: string[]
      cwd: string
      shell?: boolean
    }
  | undefined {
  // Priority 1: WSL (Windows) — run backend natively inside WSL where EDA tools + PDKs live.
  if (process.platform === "win32") {
    const wslCommand = tryWslBackendCommand(env)
    if (wslCommand) {
      backendMode = "wsl"
      return wslCommand
    }
  }

  // Priority 2: Bundled exe (packaged, Windows-native fallback — limited, no EDA tools)
  const bundled = packagedBackendExecutablePath()
  if (app.isPackaged && existsSync(bundled)) {
    backendMode = "windows-native"
    writeLog("agentic-backend", "using bundled Windows-native backend (limited — no EDA tools without WSL)", {}, "warn")
    return { executable: bundled, args: [], cwd: dirname(bundled) }
  }

  // Priority 3: Dev mode — find server dir in the repo
  const serverDir = findRepoServerDir()
  if (!serverDir) return

  const runScript = join(serverDir, "run.sh")
  const mainScript = join(serverDir, "main.py")

  if (process.platform !== "win32" && existsSync(runScript)) {
    backendMode = process.platform === "linux" ? "linux" : "dev"
    return { executable: "bash", args: [runScript], cwd: serverDir }
  }

  const python = process.platform === "win32" ? "python" : "python3"
  if (existsSync(mainScript)) {
    backendMode = "dev"
    return { executable: python, args: [mainScript], cwd: serverDir, shell: process.platform === "win32" }
  }
}

function tryWslBackendCommand(env?: NodeJS.ProcessEnv):
  | {
      executable: string
      args: string[]
      cwd: string
      shell?: boolean
    }
  | undefined {
  const wslExe = existsSync("C:\\Windows\\System32\\wsl.exe")
    ? "C:\\Windows\\System32\\wsl.exe"
    : "wsl"

  // Get default WSL distro
  let distro: string
  try {
    // Don't set encoding — we need the raw Buffer because WSL -l -q outputs UTF-16LE on Windows.
    // If we decode as utf-8 first, the bytes are already corrupted and can't be recovered.
    const result = spawnSync(wslExe, ["-l", "-q"], {
      windowsHide: true,
      timeout: 5000,
    })
    if (result.status !== 0 || !result.stdout) {
      writeLog("agentic-backend", "WSL detection: wsl -l -q failed", { status: result.status, error: result.error?.message }, "warn")
      return undefined
    }
    // result.stdout is a Buffer — decode properly
    let output: string
    const buf = Buffer.isBuffer(result.stdout) ? result.stdout : Buffer.from(result.stdout as string)
    // Detect UTF-16LE: null byte at odd positions
    if (buf.length >= 2 && buf[0] !== 0 && buf[1] === 0) {
      output = buf.toString("utf-16le")
    } else {
      output = buf.toString("utf-8")
    }
    const lines = output.split("\n").map((l) => l.trim().replace(/\r/g, "").replace(/\x00/g, "")).filter(Boolean)
    if (lines.length === 0) {
      writeLog("agentic-backend", "WSL detection: no distros found", { rawLength: buf.length, output: output.slice(0, 200) }, "warn")
      return undefined
    }
    // Default distro has * prefix
    const defaultLine = lines.find((l) => l.includes("*"))
    distro = defaultLine ? defaultLine.replace(/\*/g, "").trim() : lines[0]
    if (!distro) {
      writeLog("agentic-backend", "WSL detection: could not parse distro name", { lines }, "warn")
      return undefined
    }
    writeLog("agentic-backend", "WSL detection: found distro", { distro, allDistros: lines })
  } catch (err) {
    writeLog("agentic-backend", "WSL detection: exception getting distros", { error: String(err) }, "warn")
    return undefined
  }

  // Check python3 exists in WSL
  try {
    const result = spawnSync(wslExe, ["-d", distro, "--", "bash", "-c", "command -v python3"], {
      windowsHide: true,
      timeout: 5000,
    })
    const pythonOut = result.stdout ? (Buffer.isBuffer(result.stdout) ? result.stdout.toString("utf-8") : result.stdout as string) : ""
    if (result.status !== 0 || !pythonOut.trim()) {
      writeLog("agentic-backend", "WSL detection: python3 not found in WSL", { distro, status: result.status }, "warn")
      return undefined
    }
  } catch (err) {
    writeLog("agentic-backend", "WSL detection: exception checking python3", { error: String(err) }, "warn")
    return undefined
  }

  // Find server directory in WSL (common locations — no hardcoded user paths)
  const serverLocations = [
    "$HOME/AgentIC-app/server",
    "$HOME/.agentic/server",
    "/opt/agentic/server",
  ]
  let serverDir: string | undefined
  try {
    const checkScript = serverLocations
      .map((loc) => `[ -f "${loc}/main.py" ] && echo "${loc}" && exit 0`)
      .join("; ")
    const result = spawnSync(wslExe, ["-d", distro, "--", "bash", "-c", checkScript], {
      windowsHide: true,
      timeout: 5000,
    })
    const serverOut = result.stdout ? (Buffer.isBuffer(result.stdout) ? result.stdout.toString("utf-8") : result.stdout as string) : ""
    serverDir = serverOut.trim()
    if (!serverDir) {
      writeLog("agentic-backend", "WSL detection: server not found in common locations", { distro, searched: serverLocations }, "warn")
      return undefined
    }
  } catch {
    return undefined
  }

  writeLog("agentic-backend", "WSL backend found — running natively inside WSL", { distro, serverDir })

  // Build env exports for the WSL bash command.
  // Source .bashrc exports so PDK_ROOT, PATH (EDA tools), etc. are available.
  const envExports = env
    ? Object.entries(env)
        .filter(([k]) => k.startsWith("AGENTIC_") || k === "PYTHONUNBUFFERED" || k === "OPENCODE_DEFAULT_AGENT" || k === "OPENCODE_CHANNEL")
        .map(([k, v]) => `export ${k}='${String(v).replace(/'/g, "'\\''")}'`)
        .join("; ")
    : ""

  const cmd = [
    "for f in ~/.bashrc ~/.profile ~/.bash_profile; do [ -f \"$f\" ] && eval \"$(grep '^export ' \"$f\" 2>/dev/null)\"; done",
    envExports,
    `cd ${serverDir}`,
    "exec python3 main.py",
  ]
    .filter(Boolean)
    .join("; ")

  return {
    executable: wslExe,
    args: ["-d", distro, "--", "bash", "-c", cmd],
    cwd: ".",
    shell: false,
  }
}

function packagedBackendExecutablePath() {
  const platformKey = `${process.platform}-${process.arch}`
  const executable = process.platform === "win32" ? "agentic-backend.exe" : "agentic-backend"
  return join(process.resourcesPath, "backend", platformKey, executable)
}

function findRepoServerDir() {
  const roots = [
    process.cwd(),
    app.getAppPath(),
    dirname(fileURLToPath(import.meta.url)),
    resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../.."),
  ]

  for (const root of roots) {
    let current = resolve(root)
    for (let depth = 0; depth < 8; depth += 1) {
      const candidate = join(current, "server")
      if (existsSync(join(candidate, "main.py"))) return candidate
      const parent = dirname(current)
      if (parent === current) break
      current = parent
    }
  }
}

function backendEnvironment(): NodeJS.ProcessEnv {
  const licenseConfig = readLicenseConfig()
  const localUrl = process.env.AGENTIC_LOCAL_URL || DEFAULT_AGENTIC_URL
  const localPort = portFromLocalUrl(localUrl)
  const licenseServerUrl = (
    process.env.AGENTIC_LICENSE_SERVER_URL ||
    process.env.VITE_AGENTIC_LICENSE_SERVER_URL ||
    licenseConfig.license_server_url ||
    "https://api.buildstack.live"
  ).replace(/\/$/, "")

  const entitlementPublicKey =
    process.env.AGENTIC_ENTITLEMENT_PUBLIC_KEY || licenseConfig.entitlement_public_key || ""

  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    AGENTIC_WORKSPACE: process.env.AGENTIC_WORKSPACE || join(homedir(), "AgentIC-workspace"),
    AGENTIC_LICENSE_SERVER_URL: licenseServerUrl,
    AGENTIC_LICENSE_STATUS_URL: process.env.AGENTIC_LICENSE_STATUS_URL || `${licenseServerUrl}/license/status`,
    AGENTIC_CHECKOUT_URL: process.env.AGENTIC_CHECKOUT_URL || `${licenseServerUrl}/checkout/create`,
    AGENTIC_USAGE_URL: process.env.AGENTIC_USAGE_URL || `${licenseServerUrl}/usage/build`,
    AGENTIC_PORT: localPort || process.env.AGENTIC_PORT || "7860",
    AGENTIC_ENTITLEMENT_PUBLIC_KEY: entitlementPublicKey,
    AGENTIC_REQUIRE_SIGNED_ENTITLEMENT:
      process.env.AGENTIC_REQUIRE_SIGNED_ENTITLEMENT || (entitlementPublicKey ? "true" : "false"),
  }

  stripCloudOnlySecrets(env)
  if (app.isPackaged) {
    delete env.AGENTIC_LICENSE_BYPASS
    delete env.AGENTIC_ALLOW_HS256_ENTITLEMENTS
    delete env.AGENTIC_ENTITLEMENT_SECRET
  }

  return env
}

function readLicenseConfig(): { license_server_url?: string; entitlement_public_key?: string } {
  const candidates = app.isPackaged
    ? [join(process.resourcesPath, "license.json")]
    : [join(app.getAppPath(), "resources", "license.json")]

  for (const filePath of candidates) {
    try {
      const parsed = JSON.parse(readFileSync(filePath, "utf8"))
      return {
        license_server_url: typeof parsed.license_server_url === "string" ? parsed.license_server_url : undefined,
        entitlement_public_key:
          typeof parsed.entitlement_public_key === "string" ? parsed.entitlement_public_key : undefined,
      }
    } catch {
      // Environment config is enough in development.
    }
  }
  return {}
}

function stripCloudOnlySecrets(env: NodeJS.ProcessEnv) {
  for (const key of Object.keys(env)) {
    const upper = key.toUpperCase()
    if (
      upper.startsWith("LEMON_SQUEEZY_") ||
      upper === "SUPABASE_SERVICE_ROLE_KEY" ||
      upper === "SUPABASE_JWT_SECRET" ||
      upper === "DATABASE_URL" ||
      upper === "POSTGRES_URL" ||
      upper === "POSTGRES_PRISMA_URL" ||
      upper === "POSTGRES_URL_NON_POOLING" ||
      upper === "ENTITLEMENT_JWT_PRIVATE_KEY" ||
      upper === "ENTITLEMENT_JWT_PRIVATE_KEY_FILE" ||
      upper === "ENTITLEMENT_JWT_SECRET"
    ) {
      delete env[key]
    }
  }
}

function attachProcessLogging(child: ChildProcess) {
  child.stdout?.on("data", (data: Buffer) => writeLog("agentic-backend", data.toString().trimEnd()))
  child.stderr?.on("data", (data: Buffer) => writeLog("agentic-backend", data.toString().trimEnd(), {}, "warn"))
  child.on("exit", (code, signal) => {
    writeLog("agentic-backend", "AgentIC backend exited", { code, signal }, code === 0 ? "info" : "warn")
    if (backendProcess === child) backendProcess = null
  })
}

async function waitForBackendReady(baseUrl: string) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (!backendProcess) return
    if (await isBridgeHealthy(baseUrl)) {
      writeLog("agentic-backend", "AgentIC bridge is ready", { url: baseUrl })
      return
    }
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  writeLog("agentic-backend", "AgentIC bridge did not report ready before timeout", { url: baseUrl }, "warn")
}

function isHealthy(baseUrl: string) {
  return getJsonHealth(baseUrl, HEALTH_PATH, () => true)
}

function isBridgeHealthy(baseUrl: string) {
  return getJsonHealth(baseUrl, BRIDGE_HEALTH_PATH, (body) => body?.bridge === true)
}

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
