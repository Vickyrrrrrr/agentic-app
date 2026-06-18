import { execFile } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { promisify } from "node:util"

import type { Configuration } from "electron-builder"

const execFileAsync = promisify(execFile)
const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..")
const signScript = path.join(rootDir, "script", "sign-windows.ps1")

async function signWindows(configuration: { path: string }) {
  if (process.platform !== "win32") return
  if (process.env.GITHUB_ACTIONS !== "true") return
  if (!existsSync(signScript)) return

  await execFileAsync(
    "pwsh",
    ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", signScript, configuration.path],
    { cwd: rootDir },
  )
}

const channel = (() => {
  const raw = process.env.OPENCODE_CHANNEL
  if (raw === "dev" || raw === "beta" || raw === "prod") return raw
  return "dev"
})()

const getBase = (): Configuration => ({
  artifactName: "agentic-desktop-${os}-${arch}.${ext}",
  directories: {
    output: "dist",
    buildResources: "resources",
  },
  files: ["out/**/*", "resources/**/*"],
  extraResources: [
    {
      from: "native/",
      to: "native/",
      filter: ["index.js", "index.d.ts", "build/Release/mac_window.node", "swift-build/**"],
    },
    {
      from: "resources/backend/",
      to: "backend/",
      filter: ["**/*"],
    },
    {
      from: "resources/license.json",
      to: "license.json",
    },
  ],
  mac: {
    category: "public.app-category.developer-tools",
    icon: `resources/icons/icon.icns`,
    hardenedRuntime: true,
    gatekeeperAssess: false,
    entitlements: "resources/entitlements.plist",
    entitlementsInherit: "resources/entitlements.plist",
    notarize: channel === "prod" && process.env.AGENTIC_NOTARIZE === "1",
    target: ["dmg", "zip"],
  },
  dmg: {
    sign: channel === "prod" && process.env.AGENTIC_SIGN_MAC === "1",
  },
  protocols: {
    name: "AgentIC",
    schemes: ["agentic"],
  },
  win: {
    icon: `resources/icons/icon.ico`,
    signtoolOptions: {
      sign: signWindows,
    },
    target: ["nsis"],
    verifyUpdateCodeSignature: false,
  },
  nsis: {
    oneClick: true,
    perMachine: false,
    installerIcon: `resources/icons/icon.ico`,
    installerHeaderIcon: `resources/icons/icon.ico`,
  },
  linux: {
    icon: `resources/icons`,
    category: "Development",
    target: ["AppImage", "deb", "rpm"],
  },
})

function getConfig() {
  const base = getBase()

  switch (channel) {
    case "dev": {
      return {
        ...base,
        appId: "live.buildstack.agentic.dev",
        productName: "AgentIC Dev",
        rpm: { packageName: "agentic-dev" },
      }
    }
    case "beta": {
      return {
        ...base,
        appId: "live.buildstack.agentic.beta",
        productName: "AgentIC Beta",
        protocols: { name: "AgentIC Beta", schemes: ["agentic"] },
        publish: {
          provider: "github",
          owner: "Vickyrrrrrr",
          repo: "buildstack",
          channel: "beta",
          releaseType: "prerelease",
        },
        rpm: { packageName: "agentic-beta" },
      }
    }
    case "prod": {
      return {
        ...base,
        appId: "live.buildstack.agentic",
        productName: "AgentIC",
        protocols: { name: "AgentIC", schemes: ["agentic"] },
        publish: {
          provider: "github",
          owner: "Vickyrrrrrr",
          repo: "buildstack",
          channel: "latest",
          releaseType: "release",
        },
        rpm: { packageName: "agentic" },
      }
    }
  }
}

export default getConfig()
