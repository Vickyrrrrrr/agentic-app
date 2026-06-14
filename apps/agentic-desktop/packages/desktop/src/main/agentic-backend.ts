import { spawn, type ChildProcess } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { get as httpGet } from "node:http"
import { homedir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { app } from "electron"

import { write as writeLog } from "./logging"

const DEFAULT_AGENTIC_URL = "http://127.0.0.1:7860"
const HEALTH_PATH = "/health"

let backendProcess: ChildProcess | null = null
let started = false

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

  if (await isHealthy(baseUrl)) {
    writeLog("agentic-backend", "attached to existing local AgentIC backend", { url: baseUrl })
    return
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
  void waitForBackendReady(baseUrl)
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
  const licenseServerUrl = (
    process.env.AGENTIC_LICENSE_SERVER_URL ||
    process.env.VITE_AGENTIC_LICENSE_SERVER_URL ||
    licenseConfig.license_server_url ||
    "https://api.buildstack.live"
  ).replace(/\/$/, "")

  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    AGENTIC_WORKSPACE: process.env.AGENTIC_WORKSPACE || join(homedir(), "AgentIC-workspace"),
    AGENTIC_LICENSE_SERVER_URL: licenseServerUrl,
    AGENTIC_LICENSE_STATUS_URL: process.env.AGENTIC_LICENSE_STATUS_URL || `${licenseServerUrl}/license/status`,
    AGENTIC_CHECKOUT_URL: process.env.AGENTIC_CHECKOUT_URL || `${licenseServerUrl}/checkout/create`,
    AGENTIC_USAGE_URL: process.env.AGENTIC_USAGE_URL || `${licenseServerUrl}/usage/build`,
    AGENTIC_ENTITLEMENT_PUBLIC_KEY:
      process.env.AGENTIC_ENTITLEMENT_PUBLIC_KEY || licenseConfig.entitlement_public_key || "",
    AGENTIC_REQUIRE_SIGNED_ENTITLEMENT: process.env.AGENTIC_REQUIRE_SIGNED_ENTITLEMENT || "true",
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
    if (await isHealthy(baseUrl)) {
      writeLog("agentic-backend", "AgentIC backend is ready", { url: baseUrl })
      return
    }
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  writeLog("agentic-backend", "AgentIC backend did not report ready before timeout", { url: baseUrl }, "warn")
}

function isHealthy(baseUrl: string) {
  return new Promise<boolean>((resolve) => {
    const request = httpGet(`${baseUrl.replace(/\/$/, "")}${HEALTH_PATH}`, (response) => {
      response.resume()
      resolve(Boolean(response.statusCode && response.statusCode >= 200 && response.statusCode < 500))
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
