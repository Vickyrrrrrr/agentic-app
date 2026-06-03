import { app, shell, BrowserWindow, ipcMain, dialog, protocol, session } from 'electron'
import { join, resolve as nodeResolve } from 'path'
import { appendFileSync, existsSync, readFileSync } from 'fs'
import { writeFile } from 'fs/promises'
import { execFile, spawn } from 'child_process'
import { get as httpGet } from 'http'
import { autoUpdater } from 'electron-updater'

let mainWindow: BrowserWindow | null = null
let backendProcess: import('child_process').ChildProcess | null = null
let rendererReady = false
const pendingDeepLinks: string[] = []
const isDev = !app.isPackaged

async function createWindow(): Promise<void> {
  if (isDev) {
    await session.defaultSession.clearCache()
  }

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 768,
    show: false,
    backgroundColor: '#000000',
    titleBarStyle: 'hidden',
    ...(process.platform === 'win32'
      ? {
          titleBarOverlay: {
            color: '#000000',
            symbolColor: '#ffffff',
            height: 36
          }
        }
      : {}),
    ...(process.platform === 'linux' ? { frame: false } : {}),
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false
    }
  })

  mainWindow.on('ready-to-show', () => {
    mainWindow?.show()
  })

  mainWindow.webContents.on('did-finish-load', () => {
    rendererReady = true
    flushPendingDeepLinks()
    mainWindow?.show()
  })

  setTimeout(() => {
    mainWindow?.show()
  }, 2500)

  mainWindow.webContents.setWindowOpenHandler((details) => {
    shell.openExternal(details.url)
    return { action: 'deny' }
  })

  if (isDev && process.env['ELECTRON_RENDERER_URL']) {
    const url = new URL(process.env['ELECTRON_RENDERER_URL'])
    url.searchParams.set('desktop_build', String(Date.now()))
    mainWindow.loadURL(url.toString())
  } else {
    const rendererIndex = isDev
      ? join(__dirname, '../renderer/index.html')
      : join(process.resourcesPath, 'renderer/index.html')
    mainWindow.loadFile(rendererIndex)
  }
}

const gotTheLock = app.requestSingleInstanceLock()

if (!gotTheLock) {
  app.quit()
} else {
  app.on('second-instance', (_event, commandLine) => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.focus()
    }
    handleDeepLink(commandLine)
  })

  app.whenReady().then(() => {
    if (process.platform === 'win32') {
      app.setAppUserModelId(isDev ? process.execPath : 'live.buildstack.agentic')
    }

    app.on('browser-window-created', (_, window) => {
      watchWindowShortcuts(window)
    })

    registerIpcHandlers()
    registerProtocol()
    startBackend()
    setupAutoUpdates()
    void createWindow()

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) void createWindow()
    })
  })

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
      app.quit()
    }
  })

  app.on('before-quit', () => {
    stopBackend()
  })

  app.on('open-url', (_event, url) => {
    handleDeepLink([url])
  })
}

function watchWindowShortcuts(window: BrowserWindow): void {
  window.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return
    if (!isDev && input.code === 'KeyR' && (input.control || input.meta)) {
      event.preventDefault()
      return
    }
    if (isDev && input.code === 'F12') {
      if (window.webContents.isDevToolsOpened()) {
        window.webContents.closeDevTools()
      } else {
        window.webContents.openDevTools({ mode: 'undocked' })
      }
      return
    }
    if ((input.code === 'Minus' || (input.code === 'Equal' && input.shift)) && (input.control || input.meta)) {
      event.preventDefault()
    }
  })
}

function startBackend(): void {
  const serverDir = app.isPackaged
    ? join(process.resourcesPath, 'server')
    : join(__dirname, '..', '..', '..', 'server')
  const serverScript = join(serverDir, 'main.py')
  const runScript = join(serverDir, 'run.sh')
  const workspace = join(app.getPath('home'), 'AgentIC-workspace')
  const env = backendEnvironment(workspace)
  env['PYTHONUNBUFFERED'] = '1'
  const bundledBackend = app.isPackaged ? packagedBackendExecutablePath() : null

  const launchBackend = (): void => {
    if (bundledBackend && existsSync(bundledBackend)) {
      logBackendLine('backend', `Starting bundled backend runtime: ${bundledBackend}`)
      backendProcess = spawn(bundledBackend, [], {
        cwd: serverDir,
        env,
        stdio: 'pipe',
        windowsHide: true,
      })
    } else if (process.platform === 'win32') {
      logBackendLine('backend', 'Bundled backend runtime is unavailable. Falling back to Windows Python.')
      backendProcess = spawn('python', [serverScript], {
        cwd: serverDir,
        env,
        stdio: 'pipe',
        windowsHide: true
      })
    } else {
      backendProcess = spawn('bash', [runScript], {
        cwd: serverDir,
        env,
        stdio: 'pipe',
      })
    }

    attachBackendLogging()
    waitForBackendReady()
  }

  if (process.platform === 'win32' && app.isPackaged) {
    if (bundledBackend && existsSync(bundledBackend)) {
      launchBackend()
    } else {
      ensureWindowsBackendDependencies(serverDir, env, launchBackend)
    }
    return
  }

  launchBackend()
}

function packagedBackendExecutablePath(): string {
  const platformKey = `${process.platform}-${process.arch}`
  const executable = process.platform === 'win32' ? 'agentic-backend.exe' : 'agentic-backend'
  return join(process.resourcesPath, 'backend', platformKey, executable)
}

function ensureWindowsBackendDependencies(
  serverDir: string,
  env: NodeJS.ProcessEnv,
  onReady: () => void
): void {
  const probe = spawn('python', ['-c', 'import fastapi, uvicorn, sse_starlette, openai, jwt'], {
    cwd: serverDir,
    env,
    stdio: 'pipe',
    windowsHide: true
  })

  attachProcessLogging(probe, 'backend-deps-check')

  probe.on('exit', (code: number | null) => {
    if (code === 0) {
      onReady()
      return
    }

    logBackendLine('backend-deps', 'Installing local backend dependencies for packaged Windows app.')
    const installer = spawn('python', ['-m', 'pip', 'install', '--user', '-r', join(serverDir, 'requirements.txt')], {
      cwd: serverDir,
      env,
      stdio: 'pipe',
      windowsHide: true
    })

    attachProcessLogging(installer, 'backend-deps')
    installer.on('exit', (installCode: number | null) => {
      logBackendLine('backend-deps', `Dependency install exited with code ${installCode}`)
      onReady()
    })
  })
}

function attachBackendLogging(): void {
  if (!backendProcess) return

  if (process.platform === 'win32') {
    logBackendLine('backend', 'Starting packaged Windows backend.')
  }

  attachProcessLogging(backendProcess, 'backend')

  backendProcess.on('exit', (code: number | null) => {
    logBackendLine('backend', `exited with code ${code}`)
    backendProcess = null
  })
}

function attachProcessLogging(processRef: import('child_process').ChildProcess, label: string): void {
  processRef.stdout?.on('data', (data: Buffer) => {
    const text = data.toString()
    logBackendLine(label, text)
    if (text.includes('Uvicorn running on') || text.includes('localhost:7860')) {
      mainWindow?.webContents.send('backend-ready')
    }
  })

  processRef.stderr?.on('data', (data: Buffer) => {
    logBackendLine(`${label}:err`, data.toString())
  })
}

function waitForBackendReady(attempt = 0): void {
  if (attempt > 80 || !backendProcess) return

  const request = httpGet('http://127.0.0.1:7860/health', (response) => {
    response.resume()
    if (response.statusCode && response.statusCode >= 200 && response.statusCode < 500) {
      mainWindow?.webContents.send('backend-ready')
      logBackendLine('backend', 'Local backend is ready.')
      return
    }
    setTimeout(() => waitForBackendReady(attempt + 1), 500)
  })

  request.on('error', () => {
    setTimeout(() => waitForBackendReady(attempt + 1), 500)
  })
  request.setTimeout(1000, () => {
    request.destroy()
    setTimeout(() => waitForBackendReady(attempt + 1), 500)
  })
}

function logBackendLine(label: string, text: string): void {
  if (isDev) {
    process.stdout.write(`[${label}] ${text}`)
    return
  }

  try {
    appendFileSync(join(app.getPath('userData'), 'backend.log'), `[${new Date().toISOString()}] [${label}] ${text}`)
  } catch {
    // Logging should never prevent the desktop app from opening.
  }
}

function backendEnvironment(workspace: string): NodeJS.ProcessEnv {
  const licenseConfig = readLicenseConfig()
  const licenseServerUrl = (
    process.env['AGENTIC_LICENSE_SERVER_URL'] ||
    process.env['VITE_AGENTIC_LICENSE_SERVER_URL'] ||
    licenseConfig.license_server_url ||
    'https://api.buildstack.live'
  ).replace(/\/$/, '')

  const env: NodeJS.ProcessEnv = {
    ...process.env,
    AGENTIC_WORKSPACE: workspace,
    AGENTIC_LICENSE_SERVER_URL: licenseServerUrl,
    AGENTIC_LICENSE_STATUS_URL: process.env['AGENTIC_LICENSE_STATUS_URL'] || `${licenseServerUrl}/license/status`,
    AGENTIC_CHECKOUT_URL: process.env['AGENTIC_CHECKOUT_URL'] || `${licenseServerUrl}/checkout/create`,
    AGENTIC_USAGE_URL: process.env['AGENTIC_USAGE_URL'] || `${licenseServerUrl}/usage/build`,
    AGENTIC_ENTITLEMENT_PUBLIC_KEY:
      process.env['AGENTIC_ENTITLEMENT_PUBLIC_KEY'] ||
      licenseConfig.entitlement_public_key ||
      '',
    AGENTIC_REQUIRE_SIGNED_ENTITLEMENT: process.env['AGENTIC_REQUIRE_SIGNED_ENTITLEMENT'] || 'true'
  }

  stripCloudOnlySecrets(env)

  if (app.isPackaged) {
    delete env['AGENTIC_LICENSE_BYPASS']
    delete env['AGENTIC_ALLOW_HS256_ENTITLEMENTS']
    delete env['AGENTIC_ENTITLEMENT_SECRET']
  }

  return env
}

function stripCloudOnlySecrets(env: NodeJS.ProcessEnv): void {
  for (const key of Object.keys(env)) {
    const upper = key.toUpperCase()
    if (
      upper.startsWith('LEMON_SQUEEZY_') ||
      upper === 'SUPABASE_SERVICE_ROLE_KEY' ||
      upper === 'SUPABASE_JWT_SECRET' ||
      upper === 'DATABASE_URL' ||
      upper === 'POSTGRES_URL' ||
      upper === 'POSTGRES_PRISMA_URL' ||
      upper === 'POSTGRES_URL_NON_POOLING' ||
      upper === 'ENTITLEMENT_JWT_PRIVATE_KEY' ||
      upper === 'ENTITLEMENT_JWT_PRIVATE_KEY_FILE' ||
      upper === 'ENTITLEMENT_JWT_SECRET'
    ) {
      delete env[key]
    }
  }
}

function readLicenseConfig(): { license_server_url?: string; entitlement_public_key?: string } {
  const candidates = isDev
    ? [join(__dirname, '..', '..', 'resources', 'license.json')]
    : [join(process.resourcesPath, 'license.json')]

  for (const filePath of candidates) {
    try {
      const parsed = JSON.parse(readFileSync(filePath, 'utf-8'))
      return {
        license_server_url: typeof parsed.license_server_url === 'string' ? parsed.license_server_url : undefined,
        entitlement_public_key: typeof parsed.entitlement_public_key === 'string' ? parsed.entitlement_public_key : undefined
      }
    } catch {
      // The app can still run with environment-provided config in development.
    }
  }
  return {}
}

function stopBackend(): void {
  if (backendProcess) {
    backendProcess.kill('SIGTERM')
    setTimeout(() => {
      if (backendProcess) backendProcess.kill('SIGKILL')
    }, 3000)
  }
}

function setupAutoUpdates(): void {
  if (isDev) return

  autoUpdater.autoDownload = true
  autoUpdater.autoInstallOnAppQuit = true

  autoUpdater.on('checking-for-update', () => logBackendLine('updates', 'Checking for updates.'))
  autoUpdater.on('update-available', (info) => logBackendLine('updates', `Update available: ${info.version}`))
  autoUpdater.on('update-not-available', () => logBackendLine('updates', 'No update available.'))
  autoUpdater.on('update-downloaded', (info) => {
    logBackendLine('updates', `Update downloaded: ${info.version}. It will install when AgentIC exits.`)
  })
  autoUpdater.on('error', (error) => {
    logBackendLine('updates', `Update check failed: ${error.message}`)
  })

  setTimeout(() => {
    autoUpdater.checkForUpdatesAndNotify().catch((error) => {
      logBackendLine('updates', `Update check failed: ${error.message}`)
    })
  }, 10000)
}

function registerIpcHandlers(): void {
  ipcMain.handle('save-file', async (_event, fileName: string, content: string) => {
    const { canceled, filePath } = await dialog.showSaveDialog({
      defaultPath: fileName,
      filters: [
        { name: 'All Files', extensions: ['*'] },
        { name: 'Verilog', extensions: ['v', 'sv'] },
        { name: 'Text', extensions: ['txt', 'json', 'md'] }
      ]
    })

    if (canceled || !filePath) {
      return { success: false, filePath: null }
    }

    await writeFile(filePath, content, 'utf-8')
    return { success: true, filePath }
  })

  ipcMain.handle('get-app-version', () => {
    return app.getVersion()
  })

  ipcMain.handle('get-platform', () => {
    return process.platform
  })

  ipcMain.handle('open-external', async (_event, url: string) => {
    try {
      const parsed = new URL(url)
      if (!['https:', 'http:'].includes(parsed.protocol)) {
        return { success: false }
      }
      return { success: await openExternalUrl(parsed.toString()) }
    } catch {
      return { success: false }
    }
  })

  // EDA execution intentionally stays behind the local FastAPI backend.
  // The renderer should not receive a generic arbitrary-command IPC.
}

async function openExternalUrl(url: string): Promise<boolean> {
  if (isWslRuntime()) {
    const escapedPowerShellUrl = url.replace(/'/g, "''")
    try {
      await execFileAsync('powershell.exe', ['-NoProfile', '-Command', `Start-Process '${escapedPowerShellUrl}'`])
      return true
    } catch {
      // Try the Windows shell next.
    }

    try {
      await execFileAsync('cmd.exe', ['/c', 'start', '', url])
      return true
    } catch {
      // Try common WSL desktop helpers.
    }
  }

  try {
    await execFileAsync('wslview', [url])
    return true
  } catch {
    // Continue to Linux desktop helpers.
  }

  if (process.platform === 'linux') {
    try {
      await execFileAsync('xdg-open', [url])
      return true
    } catch {
      // Fall through to Electron's normal opener.
    }
  }

  try {
    await shell.openExternal(url)
    return true
  } catch {
    return false
  }
}

function isWslRuntime(): boolean {
  if (process.platform !== 'linux') return false
  if (process.env['WSL_DISTRO_NAME'] || process.env['WSL_INTEROP']) return true
  try {
    return readFileSync('/proc/version', 'utf-8').toLowerCase().includes('microsoft')
  } catch {
    return false
  }
}

function execFileAsync(command: string, args: string[]): Promise<void> {
  return new Promise((resolve, reject) => {
    execFile(command, args, { windowsHide: true }, (error) => {
      if (error) reject(error)
      else resolve()
    })
  })
}

function registerProtocol(): void {
  if (process.defaultApp) {
    if (process.env['AGENTIC_REGISTER_PROTOCOL_IN_DEV'] !== 'true') {
      return
    }
    if (process.argv.length >= 2) {
      app.setAsDefaultProtocolClient('agentic', process.execPath, [
        resolve(process.argv[1])
      ])
    }
  } else {
    app.setAsDefaultProtocolClient('agentic')
  }

  protocol.handle('agentic', (request) => {
    deliverDeepLink(toDeepLinkPath(request.url))
    return new Response('', { status: 200 })
  })
}

function handleDeepLink(args: string[]): void {
  const deepLinkUrl = args.find((arg) => arg.startsWith('agentic://'))
  if (!deepLinkUrl) return
  deliverDeepLink(toDeepLinkPath(deepLinkUrl))
}

function deliverDeepLink(path: string): void {
  if (mainWindow && rendererReady) {
    mainWindow.webContents.send('deep-link', path)
    return
  }
  pendingDeepLinks.push(path)
}

function flushPendingDeepLinks(): void {
  if (!mainWindow || !rendererReady) return
  while (pendingDeepLinks.length > 0) {
    const path = pendingDeepLinks.shift()
    if (path) mainWindow.webContents.send('deep-link', path)
  }
}

function toDeepLinkPath(rawUrl: string): string {
  const url = new URL(rawUrl)
  const route = [url.hostname, url.pathname.replace(/^\/+/, '')]
    .filter(Boolean)
    .join('/')
  return `/${route}${url.search}${url.hash}`
}

function resolve(path: string): string {
  return nodeResolve(path)
}
