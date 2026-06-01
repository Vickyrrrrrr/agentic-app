import { app, shell, BrowserWindow, ipcMain, dialog, protocol, session } from 'electron'
import { join, resolve as nodeResolve, dirname } from 'path'
import { writeFile } from 'fs/promises'
import { spawn, execSync } from 'child_process'

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

  if (process.platform === 'win32') {
    // Windows: use the Python launcher
    backendProcess = spawn('python', [serverScript], {
      cwd: serverDir,
      env: { ...process.env, AGENTIC_WORKSPACE: workspace },
      stdio: 'pipe',
    })
  } else {
    // Linux/macOS: use run.sh
    backendProcess = spawn('bash', [runScript], {
      cwd: serverDir,
      env: { ...process.env, AGENTIC_WORKSPACE: workspace },
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
    const url = new URL(request.url)
    mainWindow?.webContents.send('deep-link', url.pathname + url.search)
    return new Response('', { status: 200 })
  })
}

function handleDeepLink(args: string[]): void {
  const deepLinkUrl = args.find((arg) => arg.startsWith('agentic://'))
  if (deepLinkUrl && mainWindow) {
    const url = new URL(deepLinkUrl)
    mainWindow.webContents.send('deep-link', url.pathname + url.search)
  }
}

function resolve(path: string): string {
  return nodeResolve(path)
}
