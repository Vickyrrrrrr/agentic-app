import { execFile } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { promisify } from "node:util"

import type { BeforePackContext, Configuration } from "electron-builder"

const execFileAsync = promisify(execFile)
const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..")
const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)))
const signScript = path.join(rootDir, "script", "sign-windows.ps1")
const slangRuntimeDir = path.join(desktopDir, "resources", "tools", "slang")

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

async function verifyAgenticBackendRuntime(context: BeforePackContext) {
  const platform = context.electronPlatformName || process.platform
  const arch = process.arch
  const executable = platform === "win32" ? "agentic-backend.exe" : "agentic-backend"
  const backendPath = path.join(desktopDir, "resources", "backend", `${platform}-${arch}`, executable)

  if (!existsSync(backendPath)) {
    throw new Error(
      [
        `Missing AgentIC backend runtime: ${backendPath}`,
        `Run "bun run build:agentic-backend" on ${platform} before packaging.`,
        "The backend runtime is OS-specific and cannot be reused from another platform.",
      ].join("\n"),
    )
  }
  const slangExecutable = platform === "win32" ? "slang-server.exe" : "slang-server"
  const slangPath = path.join(slangRuntimeDir, `${platform}-${arch}`, slangExecutable)
  if (channel !== "dev" && !existsSync(slangPath)) {
    throw new Error(
      `Missing bundled Slang runtime: ${slangPath}. Build the exact platform/arch binary before beta/production packaging.`,
    )
  }
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
  beforePack: verifyAgenticBackendRuntime,
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
    ...(existsSync(slangRuntimeDir)
      ? [{
          // Platform-specific Slang binaries remain outside asar so the Hono
          // sidecar can execute them directly.
          from: "resources/tools/slang/",
          to: "tools/slang/",
          filter: ["**/*"],
        }]
      : []),
    {
      from: "resources/license.json",
      to: "license.json",
    },
    {
      // Bundle the Python bridge scripts so the packaged app can invoke them
      // via WSL python3 on Windows (where EDA tools and PDKs live in WSL).
      from: "../../../../server/",
      to: "server/",
      filter: ["**/*.py", "requirements*.txt"],
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
