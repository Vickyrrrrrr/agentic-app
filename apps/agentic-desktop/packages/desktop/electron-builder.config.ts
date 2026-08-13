import { existsSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

import type { Configuration } from "electron-builder"

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..")
const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)))
const slangRuntimeDir = path.join(desktopDir, "resources", "tools", "slang")

// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function verifyAgenticBackendRuntime(context: any) {
  const platform = context.electronPlatformName || process.platform
  const arch = process.arch

  const slangPath = path.join(slangRuntimeDir, `${platform}-${arch}`, "slang-server")
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
      // Bundle the Python backend scripts so the packaged app can invoke them
      // directly on Linux/macOS where EDA tools and PDKs live on PATH.
      from: "../../../../server/",
      to: "server/",
      filter: ["**/*.py", "**/*.sh", "requirements*.txt"],
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
  linux: {
    icon: `resources/icons`,
    category: "Development",
    // AppImage runs on every Linux distro (Ubuntu, RHEL, Fedora, Rocky, Arch…)
    // One file, no installation required — launch like: ./AgentIC.AppImage &
    target: ["AppImage"],
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
      }
    }
  }
}

export default getConfig()
