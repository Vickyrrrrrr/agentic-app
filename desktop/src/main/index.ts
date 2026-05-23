import { app, shell, BrowserWindow, ipcMain, dialog, protocol, session } from 'electron'
import { join, resolve as nodeResolve } from 'path'
import { writeFile } from 'fs/promises'

let mainWindow: BrowserWindow | null = null
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
