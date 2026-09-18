import type { Configuration } from "electron-builder"

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
