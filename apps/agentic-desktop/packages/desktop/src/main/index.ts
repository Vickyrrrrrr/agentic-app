import { randomUUID } from "node:crypto"
import { mkdirSync, rmSync } from "node:fs"
import * as http from "node:http"
import { createServer } from "node:net"
import { homedir, tmpdir } from "node:os"
import { join } from "node:path"
import { getCACertificates, setDefaultCACertificates } from "node:tls"
import type { Event } from "electron"
import { app, BrowserWindow } from "electron"
import { request as httpRequest } from "node:http"

import { Deferred, Effect, Fiber } from "effect"
import contextMenu from "electron-context-menu"

import type { ServerReadyData } from "../preload/types"
import { configureAgenticBackendUrl, getAgenticBackendUrl, startAgenticBackend, stopAgenticBackend } from "./agentic-backend"
import { checkAppExists, resolveAppPath } from "./apps"
import { CHANNEL } from "./constants"
import { registerIpcHandlers, sendDeepLinks, sendMenuCommand } from "./ipc"
import { forwardInitializationFailure } from "./initialization"
import { exportDebugLogs, initCrashReporter, initLogging, startNetLog, write as writeLog } from "./logging"
import { parseMarkdown } from "./markdown"
import { createMenu } from "./menu"
import {
  getDefaultServerUrl,
  preferAppEnv,
  setDefaultServerUrl,
  spawnLocalServer,
  type SidecarListener,
} from "./server"
import { setupAutoUpdater, showUpdaterDialog } from "./updater"
import {
  createMainWindow,
  registerRendererProtocol,
  setRelaunchHandler,
  setBackgroundColor,
  setDockIcon,
} from "./windows"
import { migrate } from "./migrate"

const APP_NAMES: Record<string, string> = {
  dev: "AgentIC Dev",
  beta: "AgentIC Beta",
  prod: "AgentIC",
}
const APP_IDS: Record<string, string> = {
  dev: "live.buildstack.agentic.dev",
  beta: "live.buildstack.agentic.beta",
  prod: "live.buildstack.agentic",
}
const TEST_ONBOARDING = process.env.OPENCODE_TEST_ONBOARDING === "1"
const jsCallStackFeature = "DocumentPolicyIncludeJSCallStacksInCrashReports"

let logger: ReturnType<typeof initLogging>
let mainWindow: BrowserWindow | null = null
let server: SidecarListener | null = null

const pendingDeepLinks: string[] = []

function useEnvProxy() {
  try {
    // Electron 41.2 runs Node 24.14.1; latest @types/node@24 is 24.12.2.
    ;(http as any).setGlobalProxyFromEnv()
  } catch (error) {
    logger.warn("failed to load proxy environment", error)
  }
}

function emitDeepLinks(urls: string[]) {
  if (urls.length === 0) return
  // Handle agentic://auth-callback — save session to local backend, then notify renderer
  for (const url of urls) {
    if (url.startsWith("agentic://auth-callback")) {
      handleAuthCallback(url)
    }
  }
  pendingDeepLinks.push(...urls)
  if (mainWindow) sendDeepLinks(mainWindow, urls)
}

/**
 * Called when the license server redirects back via:
 *   agentic://auth-callback#access_token=...&refresh_token=...&expires_at=...
 * Saves the Supabase session to the local backend so license checks pass.
 */
function handleAuthCallback(url: string): void {
  try {
    // Tokens arrive in the URL hash (fragment) as URLSearchParams
    const hashIndex = url.indexOf("#")
    const fragment = hashIndex !== -1 ? url.slice(hashIndex + 1) : ""
    const params = new URLSearchParams(fragment)
    const accessToken = params.get("access_token")
    const refreshToken = params.get("refresh_token")
    const expiresAt = params.get("expires_at")
    const expiresIn = params.get("expires_in")

    if (!accessToken) {
      logger.warn("auth-callback: no access_token in deep link")
      return
    }

    const session = {
      access_token: accessToken,
      refresh_token: refreshToken ?? undefined,
      expires_at: expiresAt ? Number(expiresAt) : undefined,
      expires_in: expiresIn ? Number(expiresIn) : undefined,
      token_type: "bearer",
    }

    const body = JSON.stringify(session)
    const backendUrl = new URL(getAgenticBackendUrl())
    const req = httpRequest(
      {
        hostname: backendUrl.hostname,
        port: backendUrl.port || (backendUrl.protocol === "https:" ? 443 : 80),
        path: "/auth/desktop-session",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        res.resume()
        if (res.statusCode && res.statusCode < 300) {
          logger.log("auth-callback: session saved to local backend")
          // Notify renderer so the paywall auto-dismisses
          if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send("license-activated")
          }
        } else {
          logger.warn("auth-callback: backend returned status", { status: res.statusCode })
        }
      },
    )
    req.on("error", (err) => logger.warn("auth-callback: failed to save session", { err: String(err) }))
    req.write(body)
    req.end()
  } catch (err) {
    logger.warn("auth-callback: unexpected error", { err: String(err) })
  }
}

async function killSidecar() {
  if (!server) return
  const current = server
  server = null
  await current.stop()
}

function ensureLoopbackNoProxy() {
  const loopback = ["127.0.0.1", "localhost", "::1"]
  const upsert = (key: string) => {
    const items = (process.env[key] ?? "")
      .split(",")
      .map((value: string) => value.trim())
      .filter((value: string) => Boolean(value))

    for (const host of loopback) {
      if (items.some((value: string) => value.toLowerCase() === host)) continue
      items.push(host)
    }

    process.env[key] = items.join(",")
  }

  upsert("NO_PROXY")
  upsert("no_proxy")
}

const main = Effect.gen(function* () {
  contextMenu({ showSaveImageAs: true, showLookUpSelection: false, showSearchWithGoogle: false })

  // on macOS apps run in `/` which can cause issues with ripgrep
  try {
    process.chdir(homedir())
  } catch {}

  process.env.OPENCODE_DISABLE_EMBEDDED_WEB_UI = "true"

  const appId = app.isPackaged ? APP_IDS[CHANNEL] : "live.buildstack.agentic.dev"
  const onboardingTestRoot = ((): string | undefined => {
    if (!TEST_ONBOARDING) return

    const root = join(tmpdir(), `opencode-onboarding-${randomUUID()}`)
    rmSync(root, { recursive: true, force: true })
    ;["data", "config", "cache", "state", "desktop", "session"].forEach((dir) =>
      mkdirSync(join(root, dir), { recursive: true }),
    )
    process.env.OPENCODE_DB = ":memory:"
    process.env.XDG_DATA_HOME = join(root, "data")
    process.env.XDG_CONFIG_HOME = join(root, "config")
    process.env.XDG_CACHE_HOME = join(root, "cache")
    process.env.XDG_STATE_HOME = join(root, "state")
    return root
  })()
  process.env.AGENTIC_LOCAL_URL ??= "http://127.0.0.1:7860"
  process.env.AGENTIC_MODE ??= "advisor"
  process.env.AGENTIC_PRODUCT ??= "1"
  process.env.OPENCODE_DEFAULT_AGENT ??= "agentic-vlsi"
  // AgentIC's verification workers only run isolated flow jobs, so parallel
  // subagents are a supported product capability rather than a user toggle.
  // An explicit false value remains an escape hatch for constrained machines.
  process.env.OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS ??= "true"

  app.setName(app.isPackaged ? APP_NAMES[CHANNEL] : "AgentIC Dev")
  app.setAppUserModelId(appId)
  app.setPath(
    "userData",
    onboardingTestRoot ? join(onboardingTestRoot, "desktop") : join(app.getPath("appData"), appId),
  )
  if (onboardingTestRoot) app.setPath("sessionData", join(onboardingTestRoot, "session"))
  logger = initLogging()
  initCrashReporter()

  const stopSidecars = async () => {
    await killSidecar()
    stopAgenticBackend()
  }
  const relaunch = () => {
    void stopSidecars().finally(() => {
      app.relaunch()
      app.exit(0)
    })
  }

  try {
    setDefaultCACertificates([...new Set([...getCACertificates("default"), ...getCACertificates("system")])])
  } catch (error) {
    logger.warn("failed to load system certificates", error)
  }

  logger.log("app starting", {
    version: app.getVersion(),
    packaged: app.isPackaged,
    onboardingTest: Boolean(onboardingTestRoot),
  })

  ensureLoopbackNoProxy()
  useEnvProxy()
  app.commandLine.appendSwitch("proxy-bypass-list", "<-loopback>")

  // Linux performance optimizations: GPU hardware acceleration
  if (process.platform === "linux") {
    const isWsl = Boolean(process.env.WSL_DISTRO_NAME || process.env.WSL_INTEROP)
    app.commandLine.appendSwitch("ignore-gpu-blocklist")
    app.commandLine.appendSwitch("enable-gpu-rasterization")
    if (!isWsl) {
      app.commandLine.appendSwitch("enable-zero-copy")
      app.commandLine.appendSwitch("enable-native-gpu-memory-buffers")
    }
  }

  const features = app.commandLine.getSwitchValue("enable-features")
  app.commandLine.appendSwitch("enable-features", features ? `${jsCallStackFeature},${features}` : jsCallStackFeature)
  if (!app.isPackaged) app.commandLine.appendSwitch("remote-debugging-port", "9222")

  if (!app.requestSingleInstanceLock()) {
    app.quit()
    return
  }

  preferAppEnv(app.getPath("userData"))

  app.on("second-instance", (_event: Event, argv: string[]) => {
    const urls = argv.filter((arg: string) => arg.startsWith("agentic://") || arg.startsWith("opencode://"))
    if (urls.length) {
      logger.log("deep link received via second-instance", { urls })
      emitDeepLinks(urls)
    }
    if (mainWindow) {
      mainWindow.show()
      mainWindow.focus()
    }
  })

  app.on("open-url", (event: Event, url: string) => {
    event.preventDefault()
    logger.log("deep link received via open-url", { url })
    emitDeepLinks([url])
  })

  app.on("before-quit", () => {
    stopAgenticBackend()
    void killSidecar()
  })

  app.on("will-quit", () => {
    stopAgenticBackend()
    void killSidecar()
  })

  app.on("child-process-gone", (_event, details) => {
    writeLog("utility", "child process gone", { details }, "error")
  })

  app.on("render-process-gone", (_event, webContents, details) => {
    writeLog("window", "app render process gone", { url: webContents.getURL(), details }, "error")
  })

  setRelaunchHandler(() => {
    relaunch()
  })

  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.on(signal, () => {
      void stopSidecars().finally(() => app.exit(0))
    })
  }

  const serverReady = Deferred.makeUnsafe<ServerReadyData, unknown>()

  yield* Effect.promise(() => app.whenReady())

  const port = yield* Effect.gen(function* () {
    const fromEnv = process.env.OPENCODE_PORT
    if (fromEnv) {
      const parsed = Number.parseInt(fromEnv, 10)
      if (!Number.isNaN(parsed)) return parsed
    }

    const res = yield* Deferred.make<number, unknown>()
    const server = createServer()
    server.on("error", (e) => Deferred.failSync(res, () => e))
    server.listen(0, "127.0.0.1", () => {
      const address = server.address()
      if (typeof address !== "object" || !address) {
        server.close()
        Deferred.failSync(res, () => new Error("Failed to get port"))
        return
      }
      const port = address.port
      server.close(() => Effect.runSync(Deferred.succeed(res, port)))
    })

    return yield* Deferred.await(res)
  })
  const hostname = "127.0.0.1"
  const url = `http://${hostname}:${port}`
  const password = randomUUID()

  const agenticUrl = yield* Effect.promise(() => configureAgenticBackendUrl(url))
  process.env.AGENTIC_OPENCODE_URL = url
  process.env.AGENTIC_OPENCODE_USERNAME ??= "opencode"
  process.env.AGENTIC_OPENCODE_PASSWORD ??= password

  yield* Effect.promise(() => startAgenticBackend())

  if (!TEST_ONBOARDING && process.env.AGENTIC_IMPORT_OPENCODE_STATE === "1") migrate()
  app.setAsDefaultProtocolClient("agentic")

  // Process startup deep links (Windows/Linux)
  const startupUrls = process.argv.filter((arg) => arg.startsWith("agentic://") || arg.startsWith("opencode://"))
  if (startupUrls.length > 0) {
    logger.log("startup deep link received", { urls: startupUrls })
    emitDeepLinks(startupUrls)
  }

  registerRendererProtocol()
  setDockIcon()
  const updater = setupAutoUpdater(stopSidecars)
  registerIpcHandlers({
    killSidecar: () => killSidecar(),
    relaunch,
    awaitInitialization: Effect.fnUntraced(
      function* () {
        logger.log("awaiting server ready")
        const res = yield* Deferred.await(serverReady)
        logger.log("server ready", { url: res.url })
        return res
      },
      (e) => Effect.runPromise(e),
    ),
    consumeInitialDeepLinks: () => pendingDeepLinks.splice(0),
    getDefaultServerUrl: () => getDefaultServerUrl(),
    setDefaultServerUrl: (url) => setDefaultServerUrl(url),
    getDisplayBackend: async () => null,
    setDisplayBackend: async () => undefined,
    parseMarkdown: async (markdown) => parseMarkdown(markdown),
    checkAppExists: (appName) => checkAppExists(appName),
    resolveAppPath: async (appName) => resolveAppPath(appName),
    updater,
    showUpdater: () => showUpdaterDialog(updater, true),
    setBackgroundColor: (color) => setBackgroundColor(color),
    exportDebugLogs: () => exportDebugLogs(),
    recordFatalRendererError: (error) => writeLog("renderer", "fatal renderer error", { ...error }, "error"),
  })
  void updater.start()
  const updateTimer = setInterval(() => void updater.check(), 10 * 60 * 1000)
  updateTimer.unref()
  app.once("will-quit", () => clearInterval(updateTimer))
  yield* Effect.promise(() => startNetLog()).pipe(
    Effect.catch((error) =>
      Effect.sync(() => {
        logger.warn("failed to start net log", error)
      }),
    ),
  )

  const loadingTask = yield* Effect.gen(function* () {
    logger.log("sidecar connection started", { url })

    ensureLoopbackNoProxy()
    useEnvProxy()

    logger.log("spawning sidecar", { url })
    const { listener, health } = yield* Effect.promise(() =>
      spawnLocalServer(hostname, port, password, {
        userDataPath: app.getPath("userData"),
        onStdout: (message) => writeLog("server", "stdout", { message }),
        onStderr: (message) => writeLog("server", "stderr", { message }, "warn"),
        onExit: (code) => writeLog("utility", "sidecar exited", { code }, "warn"),
      }),
    )
    server = listener
    yield* Deferred.succeed(serverReady, {
      url,
      agenticUrl,
      username: "opencode",
      password,
    })

    yield* Effect.promise(() => health.wait).pipe(
      Effect.timeout("30 seconds"),
      Effect.catch((e) =>
        Effect.sync(() => {
          logger.error("sidecar health check failed", e.toString())
        }),
      ),
    )

    logger.log("loading task finished")
  }).pipe(forwardInitializationFailure(serverReady), Effect.forkChild)

  yield* Fiber.await(loadingTask)

  mainWindow = createMainWindow()
  if (mainWindow) {
    createMenu({
      trigger: (id) => {
        const win = BrowserWindow.getFocusedWindow() ?? mainWindow
        if (win) sendMenuCommand(win, id)
      },
      checkForUpdates: () => {
        void showUpdaterDialog(updater, true)
      },
      relaunch: () => {
        relaunch()
      },
    })
  }
})

Effect.runFork(main)
