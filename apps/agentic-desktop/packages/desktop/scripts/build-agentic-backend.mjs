import { spawnSync } from "node:child_process"
import { copyFileSync, existsSync, mkdirSync, rmSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const __dirname = dirname(fileURLToPath(import.meta.url))
const desktopDir = resolve(__dirname, "..")
const repoRoot = resolve(desktopDir, "../../../..")
const serverDir = join(repoRoot, "server")

const targetPlatform = process.argv[2] || process.platform
if (targetPlatform !== process.platform) {
  console.error(
    `AgentIC backend runtime must be built on its target OS. Requested ${targetPlatform}, current builder is ${process.platform}.`,
  )
  console.error("Use a Windows builder for win32, macOS builder for darwin, and Linux builder for linux.")
  process.exit(1)
}

const platform = targetPlatform
const arch = process.arch
const platformArch = `${platform}-${arch}`
const executableName = platform === "win32" ? "agentic-backend.exe" : "agentic-backend"
const pyinstallerOutput = join(serverDir, "dist", executableName)
const targetDir = join(desktopDir, "resources", "backend", platformArch)
const targetPath = join(targetDir, executableName)
const venvDir = join(desktopDir, ".agentic-backend-build-venv", platformArch)
const venvPython = platform === "win32" ? join(venvDir, "Scripts", "python.exe") : join(venvDir, "bin", "python")

const pythonCandidates =
  platform === "win32"
    ? [
        ["python3", []],
        ["py", ["-3"]],
        ["python", []],
      ]
    : [
        ["/usr/bin/python3", []],
        ["python3", []],
        ["python", []],
      ]

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    stdio: "inherit",
    shell: platform === "win32",
    env: cleanBuildEnv(),
    ...options,
  })
  if (result.status !== 0) process.exit(result.status ?? 1)
}

function pickPython() {
  for (const [command, args] of pythonCandidates) {
    const result = spawnSync(command, [...args, "--version"], {
      stdio: "ignore",
      shell: platform === "win32",
    })
    if (result.status === 0) return [command, args]
  }
  console.error("No Python runtime found for building AgentIC backend.")
  process.exit(1)
}

const [python, prefixArgs] = pickPython()

rmSync(venvDir, { recursive: true, force: true })
run(python, [...prefixArgs, "-m", "venv", venvDir])
run(venvPython, ["-m", "ensurepip", "--upgrade"])
run(venvPython, ["-m", "pip", "install", "--upgrade", "pip"])
run(venvPython, ["-m", "pip", "install", "-r", join(serverDir, "requirements-build.txt")])
run(venvPython, ["-m", "PyInstaller", "--clean", "--noconfirm", "agentic_backend.spec"], {
  cwd: serverDir,
})

if (!existsSync(pyinstallerOutput)) {
  console.error(`Expected backend executable was not created: ${pyinstallerOutput}`)
  process.exit(1)
}

rmSync(targetDir, { recursive: true, force: true })
mkdirSync(targetDir, { recursive: true })
copyFileSync(pyinstallerOutput, targetPath)

console.log(`AgentIC backend runtime ready: ${targetPath}`)

function cleanBuildEnv() {
  const env = { ...process.env }
  delete env.PYTHONPATH
  delete env.PYTHONHOME
  delete env.VIRTUAL_ENV
  return env
}
