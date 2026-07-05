import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { Effect } from "effect"
import { HttpRouter, HttpServerResponse } from "effect/unstable/http"

type BridgeRunner =
  | { mode: "binary"; binaryPath: string }
  | { mode: "python"; scriptPath: string; serverDir: string }

function resolveBridgeRunner(): BridgeRunner | undefined {
  const executableName = process.platform === "win32" ? "agentic-backend.exe" : "agentic-backend"

  // 1. Packaged app: look for the compiled binary in process.resourcesPath/backend/<platform>-<arch>/
  const rp = (process as any).resourcesPath as string | undefined
  if (rp) {
    const platformKey = `${process.platform}-${process.arch}`
    const packedBin = join(rp, "backend", platformKey, executableName)
    if (existsSync(packedBin)) return { mode: "binary", binaryPath: packedBin }
    // Also try the flat backend directory (some builds skip the platform subfolder)
    const flatBin = join(rp, "backend", executableName)
    if (existsSync(flatBin)) return { mode: "binary", binaryPath: flatBin }
  }

  // 2. Dev mode: walk up from cwd / __dirname looking for the repo's server/ folder
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

function runPythonBridgePromise(payload: any): Promise<any> {
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

    if (runner.mode === "binary") {
      // Packaged app: run the compiled PyInstaller binary with --bridge flag
      command = runner.binaryPath
      args = ["--bridge"]
      spawnCwd = dirname(runner.binaryPath)
    } else {
      // Dev mode: run python3 opencode_bridge.py
      const isWslWorkspace =
        process.platform === "win32" &&
        (workspaceRoot.startsWith("//wsl.localhost/") ||
          workspaceRoot.startsWith("//wsl$/") ||
          workspaceRoot.startsWith("\\\\wsl.localhost\\") ||
          workspaceRoot.startsWith("\\\\wsl$\\"))

      if (isWslWorkspace) {
        const distro = getWslDistro(workspaceRoot) || "Ubuntu"
        const linuxScriptPath = wslPathToLinux(runner.scriptPath)
        command = "C:\\Windows\\System32\\wsl.exe"
        args = ["-d", distro, "--", "python3", linuxScriptPath]
        spawnCwd = runner.serverDir
      } else {
        command = process.platform === "win32" ? "python" : "python3"
        args = [runner.scriptPath]
        spawnCwd = runner.serverDir
      }
    }

    const child = spawn(command, args, {
      cwd: spawnCwd,
      env: process.env,
    })

    child.stdin.write(JSON.stringify(payload))
    child.stdin.end()

    let stdout = ""
    let stderr = ""

    child.stdout.on("data", (data) => {
      stdout += data.toString()
    })

    child.stderr.on("data", (data) => {
      stderr += data.toString()
    })

    child.on("close", (code) => {
      if (code !== 0) {
        rejectPromise(new Error(`Bridge process exited with code ${code}. Stderr: ${stderr}`))
        return
      }
      try {
        const res = JSON.parse(stdout)
        resolvePromise(res)
      } catch (err) {
        rejectPromise(new Error(`Failed to parse bridge stdout JSON: ${stdout}. Error: ${err}`))
      }
    })

    child.on("error", (err) => {
      rejectPromise(err)
    })
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
