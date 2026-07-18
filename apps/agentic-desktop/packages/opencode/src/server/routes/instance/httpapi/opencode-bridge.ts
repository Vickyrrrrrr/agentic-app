import { spawn, type ChildProcess } from "node:child_process"
import { existsSync, readFileSync, readdirSync, mkdirSync, writeFileSync } from "node:fs"
import { createHash } from "node:crypto"

import { dirname, join, resolve, basename } from "node:path"
import { fileURLToPath } from "node:url"
import { Effect } from "effect"
import { HttpRouter, HttpServerResponse } from "effect/unstable/http"
import { runYosys } from "@yowasp/yosys"
import { runDesignContractTool } from "../../../../agentic/design-contract"

// ─── Constants ────────────────────────────────────────────────────────────────
const BRIDGE_TIMEOUT_MS = 120_000 // 2 minutes max per bridge call
const WSL_EXE = "C:\\Windows\\System32\\wsl.exe"

// ─── WebAssembly Yosys Synthesizer ───────────────────────────────────────────
export async function runYosysWasm(workspaceRoot: string, filePath: string, moduleName: string) {
  const absolutePath = resolve(workspaceRoot, filePath)
  if (!existsSync(absolutePath)) {
    return { available: false, reason: `File not found: ${filePath}` }
  }

  const fileContent = readFileSync(absolutePath, "utf8")

  // Auto-detect module name if empty
  let targetModule = moduleName
  if (!targetModule) {
    const match = /module\s+(\w+)\s*[#(]/g.exec(fileContent)
    if (!match) {
      return { available: false, reason: "No module declaration found" }
    }
    targetModule = match[1]
  }

  const digest = createHash("sha256").update(fileContent).digest("hex").slice(0, 16)
  const cacheDir = join(workspaceRoot, ".agentic", "cache", "schematics")
  const cachePath = join(cacheDir, `${targetModule}_${digest}.json`)

  if (existsSync(cachePath)) {
    try {
      const cached = readFileSync(cachePath, "utf8")
      return JSON.parse(cached)
    } catch {
      // ignore and rebuild
    }
  }

  // Gather other Verilog files in the same directory as dependencies
  const fileDir = dirname(absolutePath)
  const filesIn: Record<string, string> = {}
  const verilogFiles: string[] = []

  try {
    const items = readdirSync(fileDir)
    for (const item of items) {
      if ((item.endsWith(".v") || item.endsWith(".sv")) && item !== basename(filePath)) {
        const fullItemPath = join(fileDir, item)
        const content = readFileSync(fullItemPath, "utf8")
        
        // Skip other files declaring the same targetModule to prevent redefinition errors
        const duplicateRegex = new RegExp(`\\bmodule\\s+${targetModule}\\b`, "i")
        if (duplicateRegex.test(content)) {
          continue
        }

        const nameInVirtualFs = item
        filesIn[nameInVirtualFs] = content
        verilogFiles.push(nameInVirtualFs)
      } else if (item === basename(filePath)) {
        filesIn[item] = fileContent
        verilogFiles.push(item)
      }
    }
  } catch (e) {
    // Fall back to just the single file
    filesIn[basename(filePath)] = fileContent
    verilogFiles.push(basename(filePath))
  }

  try {
    // Run the WebAssembly Yosys compiler
    const commands = [
      `hierarchy -check -top ${targetModule}`,
      `prep -top ${targetModule}`,
      `write_json output.json`
    ].join("; ")

    console.log(`[YoWASP] Running synthesis on top module '${targetModule}' with files:`, verilogFiles)

    const filesOut = await runYosys(
      ["-p", commands, ...verilogFiles],
      filesIn
    )

    if (!filesOut) {
      return { available: false, reason: "YoWASP Yosys execution returned no outputs." }
    }

    const jsonFile = filesOut["output.json"]
    if (!jsonFile) {
      return { available: false, reason: "Yosys Wasm compiled successfully but output.json was not created." }
    }

    let jsonStr: string
    if (jsonFile instanceof Uint8Array) {
      jsonStr = new TextDecoder().decode(jsonFile)
    } else if (typeof jsonFile === "string") {
      jsonStr = jsonFile
    } else {
      return { available: false, reason: "output.json is not a valid file output." }
    }

    const yosysJson = JSON.parse(jsonStr)
    const modules = yosysJson.modules || {}
    const modData = modules[targetModule] || {}
    const portsCount = Object.keys(modData.ports || {}).length
    const cellsCount = Object.keys(modData.cells || {}).length

    const result = {
      available: true,
      module: targetModule,
      yosys_json: yosysJson,
      ports: portsCount,
      cells: cellsCount
    }

    try {
      mkdirSync(cacheDir, { recursive: true })
      writeFileSync(cachePath, JSON.stringify(result, null, 2), "utf8")
    } catch {
      // ignore write errors
    }

    return result
  } catch (err: any) {
    console.error(`[YoWASP] Yosys failed for ${absolutePath}:`, err)
    return { available: false, reason: `YoWASP Yosys failed: ${err.message || err}` }
  }
}



// ─── WSL availability (cached after first check) ─────────────────────────────
let _wslAvailable: boolean | undefined
function isWslAvailable(): boolean {
  if (_wslAvailable !== undefined) return _wslAvailable
  try {
    const { execFileSync } = require("node:child_process") as typeof import("node:child_process")
    execFileSync(WSL_EXE, ["--status"], { timeout: 5000, stdio: "pipe" })
    _wslAvailable = true
  } catch {
    _wslAvailable = false
  }
  return _wslAvailable
}

// ─── Python dep bootstrap (run once per WSL distro per app session) ───────────
const _bootstrappedDistros = new Set<string>()
function ensureWslDeps(distro: string, serverDir: string): void {
  if (_bootstrappedDistros.has(distro)) return
  _bootstrappedDistros.add(distro)
  // Convert Windows serverDir → /mnt/... so pip can find requirements.txt
  const linuxServerDir = wslPathToLinux(serverDir)
  const reqFile = `${linuxServerDir}/requirements.txt`
  try {
    const { execFileSync } = require("node:child_process") as typeof import("node:child_process")
    execFileSync(
      WSL_EXE,
      ["-d", distro, "--", "bash", "-c",
        `[ -f "${reqFile}" ] && pip3 install -q --disable-pip-version-check -r "${reqFile}" 2>/dev/null || true`],
      { timeout: 60_000, stdio: "pipe" },
    )
  } catch {
    // Non-fatal — bridge will surface any actual import errors
  }
}

type BridgeRunner =
  | { mode: "binary"; binaryPath: string }
  | { mode: "python"; scriptPath: string; serverDir: string }

function resolveBridgeRunner(): BridgeRunner | undefined {
  const executableName = process.platform === "win32" ? "agentic-backend.exe" : "agentic-backend"
  const rp = (process as any).resourcesPath as string | undefined

  // 1. Packaged app: check for bundled Python scripts in resourcesPath/server/
  //    These are shipped alongside the binary so WSL workspaces can run them
  //    via `wsl python3` and get full access to PDK roots and EDA tools.
  if (rp) {
    const bundledScript = join(rp, "server", "opencode_bridge.py")
    if (existsSync(bundledScript)) {
      return { mode: "python", scriptPath: bundledScript, serverDir: join(rp, "server") }
    }
  }

  // 2. Packaged app: fall back to compiled binary (for non-WSL / native Windows workspaces)
  if (rp) {
    const platformKey = `${process.platform}-${process.arch}`
    const packedBin = join(rp, "backend", platformKey, executableName)
    if (existsSync(packedBin)) return { mode: "binary", binaryPath: packedBin }
    const flatBin = join(rp, "backend", executableName)
    if (existsSync(flatBin)) return { mode: "binary", binaryPath: flatBin }
  }

  // 3. Dev mode: walk up from cwd / __dirname looking for the repo's server/ folder
  const roots = [
    process.cwd(),
    dirname(fileURLToPath(import.meta.url)),
    resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../.."),
  ]
  for (const root of roots) {
    let current = resolve(root)
    for (let depth = 0; depth < 8; depth += 1) {
      const candidate = join(current, "server")
      if (existsSync(join(candidate, "opencode_bridge.py"))) {
        return { mode: "python", scriptPath: join(candidate, "opencode_bridge.py"), serverDir: candidate }
      }
      const parent = dirname(current)
      if (parent === current) break
      current = parent
    }
  }
  return undefined
}

function wslPathToLinux(pathStr: string): string {
  const normalized = pathStr.replace(/\\/g, "/")
  if (normalized.startsWith("//wsl.localhost/")) {
    const parts = normalized.split("/").filter(Boolean)
    if (parts.length >= 3) {
      return "/" + parts.slice(2).join("/")
    }
  }
  if (normalized.startsWith("//wsl$/")) {
    const parts = normalized.split("/").filter(Boolean)
    if (parts.length >= 3) {
      return "/" + parts.slice(2).join("/")
    }
  }
  if (normalized.length >= 2 && normalized[1] === ":") {
    const drive = normalized[0].toLowerCase()
    return `/mnt/${drive}${normalized.substring(2)}`
  }
  return pathStr
}

function getWslDistro(pathStr: string): string | undefined {
  const normalized = pathStr.replace(/\\/g, "/")
  if (normalized.startsWith("//wsl.localhost/")) {
    const parts = normalized.split("/").filter(Boolean)
    if (parts.length >= 2) {
      return parts[1]
    }
  }
  return undefined
}

function resolveLocalPath(directory: string, relativePath: string): string {
  let resolvedDir = directory
  if (process.platform !== "win32") {
    resolvedDir = wslPathToLinux(directory)
  }
  return join(resolvedDir, relativePath)
}

type DaemonInstance = {
  child: ChildProcess
  stdoutBuffer: string
  stderrBuffer: string
  pendingRequests: Array<{
    resolve: (data: any) => void
    reject: (err: any) => void
    timer: NodeJS.Timeout
  }>
}

const daemons = new Map<string, DaemonInstance>()

function getOrCreateDaemon(command: string, args: string[], cwd: string, distroKey: string): DaemonInstance {
  let inst = daemons.get(distroKey)
  if (inst && inst.child.exitCode === null) {
    return inst
  }

  console.log(`[Python Bridge Daemon] Starting keep-alive daemon for ${distroKey} (cwd: ${cwd})`)
  
  // Start the Python process in --daemon mode
  const child = spawn(command, [...args, "--daemon"], {
    cwd,
    env: process.env,
  })

  const newInst: DaemonInstance = {
    child,
    stdoutBuffer: "",
    stderrBuffer: "",
    pendingRequests: []
  }

  daemons.set(distroKey, newInst)

  // Parse stdout line-by-line (each line is a single JSON response)
  child.stdout.on("data", (chunk) => {
    newInst.stdoutBuffer += chunk.toString()
    let newlineIndex: number
    while ((newlineIndex = newInst.stdoutBuffer.indexOf("\n")) !== -1) {
      const line = newInst.stdoutBuffer.substring(0, newlineIndex).trim()
      newInst.stdoutBuffer = newInst.stdoutBuffer.substring(newlineIndex + 1)
      if (!line) continue

      const req = newInst.pendingRequests.shift()
      if (req) {
        clearTimeout(req.timer)
        try {
          req.resolve(JSON.parse(line))
        } catch (err) {
          req.reject(new Error(`Failed to parse bridge daemon stdout JSON: ${line.slice(0, 512)}. Error: ${err}`))
        }
      }
    }
  })

  child.stderr.on("data", (chunk) => {
    newInst.stderrBuffer += chunk.toString()
    console.error(`[Python Bridge Daemon ${distroKey} stderr]:`, chunk.toString())
  })

  child.on("close", (code) => {
    const pending = newInst.pendingRequests
    newInst.pendingRequests = []
    daemons.delete(distroKey)
    for (const req of pending) {
      clearTimeout(req.timer)
      req.reject(new Error(`Python bridge daemon exited unexpectedly with code ${code}. Stderr: ${newInst.stderrBuffer}`))
    }
  })

  child.on("error", (err) => {
    const pending = newInst.pendingRequests
    newInst.pendingRequests = []
    daemons.delete(distroKey)
    for (const req of pending) {
      clearTimeout(req.timer)
      req.reject(err)
    }
  })

  return newInst
}

// Clean up all daemon processes on process termination
process.on("exit", () => {
  for (const inst of daemons.values()) {
    try { inst.child.kill("SIGKILL") } catch { /* ignore */ }
  }
})

export function runPythonBridgePromise(payload: any): Promise<any> {
  return new Promise((resolvePromise, rejectPromise) => {
    const workspaceRoot = payload.workspace_root || ""
    const runner = resolveBridgeRunner()

    if (!runner) {
      rejectPromise(new Error("Could not locate the AgentIC backend. Expected either agentic-backend binary or opencode_bridge.py."))
      return
    }

    let command: string
    let args: string[]
    let spawnCwd: string
    let distroKey = "native"

    const isWslWorkspace =
      process.platform === "win32" &&
      (workspaceRoot.startsWith("//wsl.localhost/") ||
        workspaceRoot.startsWith("//wsl$/") ||
        workspaceRoot.startsWith("\\\\wsl.localhost\\") ||
        workspaceRoot.startsWith("\\\\wsl$\\"))

    if (runner.mode === "binary") {
      command = runner.binaryPath
      args = ["--bridge"]
      spawnCwd = dirname(runner.binaryPath)
      distroKey = "binary"
    } else {
      if (isWslWorkspace && isWslAvailable()) {
        const distro = getWslDistro(workspaceRoot) || "Ubuntu"
        const linuxScriptPath = wslPathToLinux(runner.scriptPath)
        ensureWslDeps(distro, runner.serverDir)
        command = WSL_EXE
        args = ["-d", distro, "--", "python3", linuxScriptPath]
        spawnCwd = dirname(runner.scriptPath)
        distroKey = `wsl-${distro}`
      } else if (isWslWorkspace && !isWslAvailable()) {
        rejectPromise(new Error(
          "WSL is required for this workspace but was not found. " +
          "Please install WSL (wsl --install) and restart the app."
        ))
        return
      } else {
        command = process.platform === "win32" ? "python" : "python3"
        args = [runner.scriptPath]
        spawnCwd = runner.serverDir
        distroKey = "native"
      }
    }

    let inst: DaemonInstance
    try {
      inst = getOrCreateDaemon(command, args, spawnCwd, distroKey)
    } catch (err) {
      rejectPromise(err)
      return
    }

    // Dynamic timeout mapping to avoid hanging
    const timer = setTimeout(() => {
      const idx = inst.pendingRequests.findIndex((r) => r.timer === timer)
      if (idx !== -1) {
        inst.pendingRequests.splice(idx, 1)
      }
      try { inst.child.kill("SIGKILL") } catch { /* ignore */ }
      daemons.delete(distroKey)
      rejectPromise(new Error(`Bridge daemon process request timed out after ${BRIDGE_TIMEOUT_MS / 1000}s`))
    }, BRIDGE_TIMEOUT_MS)

    inst.pendingRequests.push({
      resolve: resolvePromise,
      reject: rejectPromise,
      timer
    })

    // Write request to daemon's stdin
    if (inst.child.stdin) {
      inst.child.stdin.write(JSON.stringify(payload) + "\n")
    } else {
      rejectPromise(new Error("Python daemon process stdin is not available."))
    }
  })
}

function runPythonBridge(payload: any) {
  return Effect.promise(() => runPythonBridgePromise(payload))
}

export const opencodeBridgeRoute = HttpRouter.use((router) =>
  Effect.gen(function* () {
    // 1. POST /opencode/tool
    yield* router.add("POST", "/opencode/tool", (request) =>
      Effect.gen(function* () {
        const body = (yield* request.json.pipe(Effect.catch(() => Effect.succeed({})))) as any

        // Intercept schematic_json to run via WebAssembly if the native Yosys tool fails or is missing
        const toolArgs = body.args || body.arguments || {}
        if (body.name === "design_contract") {
          const nativeResult = yield* Effect.promise(() => runDesignContractTool(body)).pipe(
            Effect.catch((nativeErr) =>
              runPythonBridge({ action: "tool", ...body }).pipe(
                Effect.catch((bridgeErr) =>
                  Effect.succeed({
                    success: false,
                    error: `Native design contract failed: ${String(nativeErr)}; Python fallback failed: ${String(bridgeErr)}`,
                  }),
                ),
              ),
            ),
          )
          return HttpServerResponse.jsonUnsafe(nativeResult)
        }

        if (body.name === "workspace" && toolArgs.action === "schematic_json") {
          const workspaceRoot = body.workspace_root || ""
          const filePath = toolArgs.path || ""
          const moduleName = toolArgs.module || ""

          try {
            const bridgeRes = yield* runPythonBridge({ action: "tool", ...body })
            if (bridgeRes && bridgeRes.success) {
              const parsed = JSON.parse(bridgeRes.result)
              if (parsed && parsed.available) {
                return HttpServerResponse.jsonUnsafe(bridgeRes)
              }
            }
          } catch {
            // bridge failed/not found — fall through to WebAssembly
          }

          // Execute WebAssembly Yosys synthesis natively inside Electron/Hono.
          // Note: in dev mode (npm run dev), yosys.core.wasm is NOT bundled yet —
          // it only exists after `npm run build`. We catch ENOENT and return a
          // clean available:false instead of crashing the bridge.
          const wasmResult = yield* Effect.promise(async () => {
            try {
              return await runYosysWasm(workspaceRoot, filePath, moduleName)
            } catch (err: any) {
              const msg = String(err?.message || err)
               if (err?.code === "ENOENT" && msg.includes("yosys.core.wasm")) {
                 console.warn("[YoWASP] WASM file not found. If in dev mode, run `npm run build` once to compile and bundle assets.")
                 return {
                   available: false,
                   reason: "Schematic generation failed: Yosys compiler is not installed on this system, and the built-in WebAssembly fallback engine could not be loaded. Please install Yosys locally or contact support."
                 }
               }
               console.error("[YoWASP] Unexpected error:", msg)
               return { available: false, reason: `WebAssembly Yosys compiler error: ${msg}` }
            }
          }).pipe(
            Effect.map((result) => HttpServerResponse.jsonUnsafe({ success: true, result: JSON.stringify(result) })),
            Effect.catch((err) =>
              Effect.succeed(
                HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
              )
            )
          )
          return wasmResult
        }



        const res = yield* runPythonBridge({ action: "tool", ...body }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )


    // 2. POST /opencode/session/resolve
    yield* router.add("POST", "/opencode/session/resolve", (request) =>
      Effect.gen(function* () {
        const body = (yield* request.json.pipe(Effect.catch(() => Effect.succeed({})))) as any
        const res = yield* runPythonBridge({ action: "pipeline", ...body }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )

    // 3. GET /opencode/session/mode/*
    yield* router.add("GET", "/opencode/session/mode/*", (request) =>
      Effect.gen(function* () {
        const session_id = request.url.split("/").pop() || ""
        const res = yield* runPythonBridge({ action: "get_mode", session_id }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )

    // 4. POST /opencode/session/mode
    yield* router.add("POST", "/opencode/session/mode", (request) =>
      Effect.gen(function* () {
        const body = (yield* request.json.pipe(Effect.catch(() => Effect.succeed({})))) as any
        const res = yield* runPythonBridge({ action: "set_mode", ...body }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )

    // 5. POST /opencode/git/clone
    yield* router.add("POST", "/opencode/git/clone", (request) =>
      Effect.gen(function* () {
        const body = (yield* request.json.pipe(Effect.catch(() => Effect.succeed({})))) as any
        const res = yield* runPythonBridge({ action: "git_clone", ...body }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )

    // 6. POST /auth/desktop-session
    yield* router.add("POST", "/auth/desktop-session", () =>
      Effect.gen(function* () {
        return HttpServerResponse.jsonUnsafe({ success: true })
      }),
    )

    // 7. GET /license/status
    yield* router.add("GET", "/license/status", (request) =>
      Effect.gen(function* () {
        const authHeader = request.headers["authorization"]
        const headers = authHeader ? { authorization: authHeader } : {}
        const res = yield* runPythonBridge({ action: "license_status", headers }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )

    // 8. GET /build/signoff/session/*
    yield* router.add("GET", "/build/signoff/session/*", (request) =>
      Effect.gen(function* () {
        const session_id = request.url.split("/").pop() || ""
        const res = yield* runPythonBridge({ action: "signoff_report", session_id }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )

    // 9. GET /build/sta/session/*
    yield* router.add("GET", "/build/sta/session/*", (request) =>
      Effect.gen(function* () {
        const session_id = request.url.split("/").pop() || ""
        const res = yield* runPythonBridge({ action: "sta_report", session_id }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )

    // 10. GET /simulation/waveforms/*
    yield* router.add("GET", "/simulation/waveforms/*", (request) =>
      Effect.gen(function* () {
        const design_name = request.url.split("/").pop() || ""
        const res = yield* runPythonBridge({ action: "waveforms", design_name }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )

    // 11. GET /opencode/binary-file
    yield* router.add("GET", "/opencode/binary-file", (request) =>
      Effect.gen(function* () {
        const url = new URL(request.url, "http://localhost")
        const directory = url.searchParams.get("directory") || ""
        const fileCwd = url.searchParams.get("path") || ""
        const resolvedPath = resolveLocalPath(directory, fileCwd)

        if (!existsSync(resolvedPath)) {
          return HttpServerResponse.jsonUnsafe({ success: false, error: "File not found" }, { status: 404 })
        }

        const fileContent = yield* Effect.tryPromise(() => import("node:fs/promises").then((fs) => fs.readFile(resolvedPath)))
        return HttpServerResponse.raw(fileContent, {
          headers: {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": `attachment; filename="${resolvedPath.split(/[/\\]/).pop()}"`,
          }
        })
      }),
    )

    // 12. GET /auth/profile
    yield* router.add("GET", "/auth/profile", (request) =>
      Effect.gen(function* () {
        const authHeader = request.headers["authorization"]
        const headers = authHeader ? { authorization: authHeader } : {}
        const res = yield* runPythonBridge({ action: "auth_profile", headers }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )

    // 13. POST /auth/logout
    yield* router.add("POST", "/auth/logout", () =>
      Effect.gen(function* () {
        const res = yield* runPythonBridge({ action: "auth_logout" }).pipe(
          Effect.map((data) => HttpServerResponse.jsonUnsafe(data)),
          Effect.catch((err) =>
            Effect.succeed(
              HttpServerResponse.jsonUnsafe({ success: false, error: String(err) }, { status: 500 })
            )
          )
        )
        return res
      }),
    )
  }),
)
