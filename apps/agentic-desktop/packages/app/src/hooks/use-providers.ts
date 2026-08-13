import { useServerSync } from "@/context/server-sync"
import { decode64 } from "@/utils/base64"
import { useParams } from "@solidjs/router"
import { Iterable, pipe } from "effect"
import { createMemo } from "solid-js"

export const popularProviders = [
  "opencode",
  "opencode-go",
  "anthropic",
  "github-copilot",
  "openai",
  "google",
  "openrouter",
  "vercel",
]
const popularProviderSet = new Set(popularProviders)

let lastValidProvider: any | undefined

export function useProviders() {
  const serverSync = useServerSync()
  const params = useParams()
  const dir = createMemo(() => decode64(params.dir) ?? "")
  const providers = () => {
    let current: any | undefined
    if (dir()) {
      const [projectStore] = serverSync.child(dir())
      if (projectStore?.provider?.all?.size) current = projectStore.provider
      else if (projectStore?.provider_ready) current = projectStore.provider
    }
    if (!current?.all?.size) {
      if (serverSync.data.provider?.all?.size) current = serverSync.data.provider
    }
    if (current?.all?.size) {
      lastValidProvider = current
      return current
    }
    return lastValidProvider ?? current ?? serverSync.data.provider
  }
  return {
    all: () => providers().all,
    default: () => providers().default,
    popular: () => {
      const all = Array.from(providers().all?.values() ?? [])
      return all.filter((p: any) => popularProviderSet.has(p.id))
    },
    connected: () => {
      const connectedSet = new Set(["opencode", "opencode-go", ...(providers().connected ?? [])])
      const all = Array.from(providers().all?.values() ?? [])
      return all.filter((p: any) => connectedSet.has(p.id))
    },
    paid: () => {
      const connectedSet = new Set(providers().connected ?? [])
      const all = (Array.from(providers().all?.entries() ?? []) as [string, any][])
      return all.filter(
        ([id, provider]) =>
          connectedSet.has(id) &&
          (id !== "opencode" || Object.values(provider?.models ?? {}).some((m: any) => m.cost?.input)),
      )
    },
  }
}
