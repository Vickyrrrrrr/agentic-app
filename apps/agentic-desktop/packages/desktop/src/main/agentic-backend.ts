import { spawn, type ChildProcess } from "node:child_process"
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

export function getAgenticBackendUrl() {
  return process.env.AGENTIC_LOCAL_URL || DEFAULT_AGENTIC_URL
}

export async function startAgenticBackend() {
  if (started || process.env.AGENTIC_MANAGED_BACKEND === "0") return
  started = true

  process.env.AGENTIC_LOCAL_URL ??= DEFAULT_AGENTIC_URL
  process.env.AGENTIC_MODE ??= "advisor"
  process.env.OPENCODE_DEFAULT_AGENT ??= "agentic-vlsi"

  const baseUrl = process.env.AGENTIC_LOCAL_URL
  if (!baseUrl || !isManagedLoopback(baseUrl)) {
    writeLog("agentic-backend", "using externally configured AgentIC backend", { url: baseUrl })
    return
  }

  const hasBundledBinary = app.isPackaged && existsSync(packagedBackendExecutablePath())

  // In dev mode (no bundled binary), attach to an existing dev backend if one is running.
  // In packaged mode (has bundled binary), NEVER attach — always start our own on a
  // dynamic port so there's no conflict with any dev backend on 7860.
  if (!hasBundledBinary && await isBridgeHealthy(baseUrl)) {
    writeLog("agentic-backend", "attached to existing local AgentIC bridge", { url: baseUrl })
    return
  }

  // If the preferred port (7860) is occupied by any process, find a free port.
  if (hasBundledBinary && (await isHealthy(baseUrl) || await isBridgeHealthy(baseUrl))) {
    const managedUrl = await nextManagedBackendUrl(baseUrl)
    process.env.AGENTIC_LOCAL_URL = managedUrl
    writeLog("agentic-backend", "preferred port occupied, allocated dynamic port", {
      preferred: baseUrl,
      managed: managedUrl,
    })
  } else if (await isHealthy(baseUrl)) {
    const managedUrl = await nextManagedBackendUrl(baseUrl)
    process.env.AGENTIC_LOCAL_URL = managedUrl
    writeLog(
      "agentic-backend",
      "existing local backend is missing the AgentIC runtime bridge; starting isolated AgentIC bridge",
      { existing: baseUrl, managed: managedUrl },
      "warn",
    )
  }

  const command = resolveBackendCommand()
  if (!command) {
    writeLog("agentic-backend", "AgentIC backend runtime is unavailable", {}, "warn")
    return
  }

  const env = backendEnvironment()
  writeLog("agentic-backend", "starting AgentIC backend runtime", {
    command: command.executable,
    args: command.args,
    cwd: command.cwd,
  })

  backendProcess = spawn(command.executable, command.args, {
    cwd: command.cwd,
    env,
    stdio: "pipe",
    shell: command.shell,
    windowsHide: true,
  })

  attachProcessLogging(backendProcess)
  void waitForBackendReady(process.env.AGENTIC_LOCAL_URL || baseUrl)
}

export function stopAgenticBackend() {
  if (!backendProcess) return
  const current = backendProcess
  backendProcess = null
  current.kill("SIGTERM")
  setTimeout(() => {
    if (!current.killed) current.kill("SIGKILL")
  }, 3000).unref()
}

function resolveBackendCommand():
  | {
      executable: string
      args: string[]
      cwd: string
      shell?: boolean
    }
  | undefined {
  const bundled = packagedBackendExecutablePath()
  if (app.isPackaged && existsSync(bundled)) {
    return { executable: bundled, args: [], cwd: dirname(bundled) }
  }

  const serverDir = findRepoServerDir()
  if (!serverDir) return

  const runScript = join(serverDir, "run.sh")
  const mainScript = join(serverDir, "main.py")

  if (process.platform !== "win32" && existsSync(runScript)) {
    return { executable: "bash", args: [runScript], cwd: serverDir }
  }

  const python = process.platform === "win32" ? "python" : "python3"
  if (existsSync(mainScript)) return { executable: python, args: [mainScript], cwd: serverDir, shell: process.platform === "win32" }
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
