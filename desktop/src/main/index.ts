import { app, shell, BrowserWindow, ipcMain, dialog, protocol, session } from 'electron'
import { join, resolve as nodeResolve } from 'path'
import { readFileSync } from 'fs'
import { writeFile } from 'fs/promises'
import { execFile, spawn } from 'child_process'

let mainWindow: BrowserWindow | null = null
let backendProcess: import('child_process').ChildProcess | null = null
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
  const serverDir = join(__dirname, '..', '..', '..', 'server')
  const serverScript = join(serverDir, 'main.py')
  const runScript = join(serverDir, 'run.sh')
  const workspace = join(app.getPath('home'), 'AgentIC-workspace')
  const env = backendEnvironment(workspace)

  if (process.platform === 'win32') {
    // Windows: use the Python launcher
    backendProcess = spawn('python', [serverScript], {
      cwd: serverDir,
      env,
      stdio: 'pipe',
    })
  } else {
    // Linux/macOS: use run.sh
    backendProcess = spawn('bash', [runScript], {
      cwd: serverDir,
      env,
      stdio: 'pipe',
    })
  }

  if (backendProcess.stdout) {
    backendProcess.stdout.on('data', (data: Buffer) => {
      const text = data.toString()
      // Log server output in dev mode
      if (isDev) process.stdout.write(`[backend] ${text}`)
      // Notify renderer when server is ready
      if (text.includes('Uvicorn running on') || text.includes('localhost:7860')) {
        mainWindow?.webContents.send('backend-ready')
      }
    })
  }

  if (backendProcess.stderr) {
    backendProcess.stderr.on('data', (data: Buffer) => {
      if (isDev) process.stderr.write(`[backend:err] ${data.toString()}`)
    })
  }

  backendProcess.on('exit', (code: number | null) => {
    if (isDev) console.log(`[backend] exited with code ${code}`)
    backendProcess = null
  })
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

  ipcMain.handle('execute-local-eda', async (_event, command: string, cwd?: string) => {
    try {
      const { exec } = await import('child_process')
      const { promisify } = await import('util')
      const execAsync = promisify(exec)

      let fullCommand: string
      if (process.platform === 'win32') {
        const wslCwd = cwd ? `cd ${cwd} && ` : ''
        fullCommand = `wsl -d Ubuntu-22.04 bash -c "${wslCwd}${command.replace(/"/g, '\\"')}"`
      } else {
        fullCommand = cwd ? `cd "${cwd}" && ${command}` : command
      }

      const { stdout, stderr } = await execAsync(fullCommand)
      return { success: true, stdout, stderr, code: 0 }
    } catch (error: any) {
      return {
        success: false,
        stdout: error.stdout || '',
        stderr: error.stderr || error.message,
        code: error.code || 1
      }
    }
  })
}

async function openExternalUrl(url: string): Promise<boolean> {
  if (isWslRuntime()) {
    try {
      const escaped = url.replace(/'/g, "''")
      await execFileAsync('powershell.exe', ['-NoProfile', '-Command', `Start-Process '${escaped}'`])
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
    execFile(command, args, (error) => {
      if (error) reject(error)
      else resolve()
    })
  })
}

function registerProtocol(): void {
  if (process.defaultApp) {
    if (process.argv.length >= 2) {
      app.setAsDefaultProtocolClient('agentic', process.execPath, [
        resolve(process.argv[1])
      ])
    }
  } else {
    app.setAsDefaultProtocolClient('agentic')
  }

  protocol.handle('agentic', (request) => {
    mainWindow?.webContents.send('deep-link', toDeepLinkPath(request.url))
    return new Response('', { status: 200 })
  })
}

function handleDeepLink(args: string[]): void {
  const deepLinkUrl = args.find((arg) => arg.startsWith('agentic://'))
  if (deepLinkUrl && mainWindow) {
    mainWindow.webContents.send('deep-link', toDeepLinkPath(deepLinkUrl))
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
