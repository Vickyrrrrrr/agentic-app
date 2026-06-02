import { contextBridge, ipcRenderer } from 'electron'

export interface ElectronAPI {
  saveFile: (
    fileName: string,
    content: string
  ) => Promise<{ success: boolean; filePath: string | null }>
  getVersion: () => Promise<string>
  getPlatform: () => Promise<string>
  openExternal: (url: string) => Promise<{ success: boolean }>
  onDeepLink: (callback: (path: string) => void) => () => void
  onBackendReady: (callback: () => void) => () => void
  executeLocalEDA: (
    command: string,
    cwd?: string
  ) => Promise<{ success: boolean; stdout: string; stderr: string; code: number }>
}

const electronAPI: ElectronAPI = {
  saveFile: (fileName: string, content: string) =>
    ipcRenderer.invoke('save-file', fileName, content),

  getVersion: () => ipcRenderer.invoke('get-app-version'),

  getPlatform: () => ipcRenderer.invoke('get-platform'),

  openExternal: (url: string) => ipcRenderer.invoke('open-external', url),

  onDeepLink: (callback: (path: string) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, path: string): void => {
      callback(path)
    }
    ipcRenderer.on('deep-link', handler)
    return () => {
      ipcRenderer.removeListener('deep-link', handler)
    }
  },

  onBackendReady: (callback: () => void) => {
    const handler = (): void => callback()
    ipcRenderer.on('backend-ready', handler)
    return () => {
      ipcRenderer.removeListener('backend-ready', handler)
    }
  },

  executeLocalEDA: (command: string, cwd?: string) =>
    ipcRenderer.invoke('execute-local-eda', command, cwd)
}

contextBridge.exposeInMainWorld('electronAPI', electronAPI)
