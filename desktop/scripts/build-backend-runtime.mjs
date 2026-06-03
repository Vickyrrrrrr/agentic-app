import { mkdirSync, copyFileSync, existsSync, rmSync } from 'fs'
import { dirname, join, resolve } from 'path'
import { fileURLToPath } from 'url'
import { spawnSync } from 'child_process'

const __dirname = dirname(fileURLToPath(import.meta.url))
const desktopDir = resolve(__dirname, '..')
const repoRoot = resolve(desktopDir, '..')
const serverDir = join(repoRoot, 'server')

const targetPlatform = process.argv[2] || process.platform
if (targetPlatform !== process.platform) {
  console.error(
    `AgentIC backend runtime must be built on its target OS. ` +
    `Requested ${targetPlatform}, current builder is ${process.platform}.`
  )
  console.error('Use a Windows builder for win32, macOS builder for darwin, and Linux builder for linux.')
  process.exit(1)
}

const platform = targetPlatform
const arch = process.arch
const platformArch = `${platform}-${arch}`
const executableName = platform === 'win32' ? 'agentic-backend.exe' : 'agentic-backend'
const pyinstallerOutput = join(serverDir, 'dist', executableName)
const targetDir = join(desktopDir, 'resources', 'backend', platformArch)
const targetPath = join(targetDir, executableName)

const pythonCandidates = platform === 'win32'
  ? [
      ['python3', []],
      ['py', ['-3']],
      ['python', []],
    ]
  : [
      ['python3', []],
      ['python', []],
    ]

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    stdio: 'inherit',
    shell: platform === 'win32',
    ...options,
  })
  if (result.status !== 0) {
    process.exit(result.status ?? 1)
  }
}

function pickPython() {
  for (const [command, args] of pythonCandidates) {
    const result = spawnSync(command, [...args, '--version'], {
      stdio: 'ignore',
      shell: platform === 'win32',
    })
    if (result.status === 0) return [command, args]
  }
  console.error('No Python runtime found for building AgentIC backend.')
  process.exit(1)
}

const [python, prefixArgs] = pickPython()

run(python, [...prefixArgs, '-m', 'pip', 'install', '-r', join(serverDir, 'requirements-build.txt')])
run(python, [...prefixArgs, '-m', 'PyInstaller', '--clean', '--noconfirm', 'agentic_backend.spec'], { cwd: serverDir })

if (!existsSync(pyinstallerOutput)) {
  console.error(`Expected backend executable was not created: ${pyinstallerOutput}`)
  process.exit(1)
}

rmSync(targetDir, { recursive: true, force: true })
mkdirSync(targetDir, { recursive: true })
copyFileSync(pyinstallerOutput, targetPath)

console.log(`AgentIC backend runtime ready: ${targetPath}`)
