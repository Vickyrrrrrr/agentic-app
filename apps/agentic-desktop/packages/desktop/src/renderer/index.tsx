// @refresh reload

import {
  ACCEPTED_FILE_EXTENSIONS,
  AppBaseProviders,
  AppInterface,
  handleNotificationClick,
  loadLocaleDict,
  normalizeLocale,
  type Locale,
  type Platform,
  PlatformProvider,
  ServerConnection,
  useCommand,
  useWslServers,
} from "@opencode-ai/app"
import type { UpdaterState } from "@opencode-ai/app/updater"
import * as Sentry from "@sentry/solid"
import type { AsyncStorage } from "@solid-primitives/storage"
import { MemoryRouter } from "@solidjs/router"
import { createEffect, createMemo, createResource, createSignal, type JSX, onCleanup, onMount, Show } from "solid-js"
import { render } from "solid-js/web"
import pkg from "../../package.json"
import { initI18n, t } from "./i18n"
import { initializationData, initializationReady } from "./initialization"
import { resetZoom, setPinchZoomEnabled, webviewZoom, zoomIn, zoomOut } from "./webview-zoom"
import { availableStartupServer, readyWslConnections } from "./wsl/connections"
import "./styles.css"
import { Splash } from "@opencode-ai/ui/logo"
import { useTheme } from "@opencode-ai/ui/theme/context"

const root = document.getElementById("root")
if (import.meta.env.DEV && !(root instanceof HTMLElement)) {
  throw new Error(t("error.dev.rootNotFound"))
}

if (import.meta.env.VITE_SENTRY_DSN) {
  Sentry.init({
    dsn: import.meta.env.VITE_SENTRY_DSN,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT ?? import.meta.env.MODE,
    release: import.meta.env.VITE_SENTRY_RELEASE ?? `desktop@${pkg.version}`,
    initialScope: {
      tags: {
        platform: "desktop",
      },
    },
    integrations: (integrations) => {
      return integrations.filter(
        (i) =>
          i.name !== "Breadcrumbs" &&
          !(
            import.meta.env.OPENCODE_CHANNEL === "prod" &&
            (i.name === "GlobalHandlers" || i.name === "BrowserApiErrors")
          ),
      )
    },
  })
}

void initI18n()

const [updaterState, setUpdaterState] = createSignal<UpdaterState>({ status: "disabled" })
void window.api.updater.subscribe(setUpdaterState)

const deepLinkEvent = "opencode:deep-link"
const AGENTIC_AUTH_STORAGE_KEY = "agentic_auth_session"
const AGENTIC_API_BASE_STORAGE_KEY = "agentic_local_api_base"
const AGENTIC_PRICING_URL = "https://www.buildstack.live/agentic/pricing"
const AGENTIC_AUTH_START_URL = "https://api.buildstack.live/auth/google/start"

type AgenticAuthUser = {
  id?: string
  email?: string | null
}

type AgenticAuthSession = {
  access_token?: string | null
  refresh_token?: string | null
  expires_at?: number | null
  expires_in?: number | null
  token_type?: string
  user?: AgenticAuthUser | null
}

type AgenticLicenseStatus = {
  active?: boolean
  plan?: string
  source?: string
  reason?: string
}

const emitDeepLinks = (urls: string[]) => {
  if (urls.length === 0) return
  window.__OPENCODE__ ??= {}
  const pending = window.__OPENCODE__.deepLinks ?? []
  window.__OPENCODE__.deepLinks = [...pending, ...urls]
  window.dispatchEvent(new CustomEvent(deepLinkEvent, { detail: { urls } }))
}

const userFromAccessToken = (token: string): AgenticAuthUser | null => {
  try {
    const payload = token.split(".")[1]
    if (!payload) return null
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/")
    const parsed = JSON.parse(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=")))
    return { id: parsed.sub, email: parsed.email }
  } catch {
    return null
  }
}

const readStoredAgenticSession = (): AgenticAuthSession | null => {
  try {
    const raw = localStorage.getItem(AGENTIC_AUTH_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as AgenticAuthSession
    return parsed?.access_token ? parsed : null
  } catch {
    return null
  }
}

const storeAgenticSession = (session: AgenticAuthSession | null) => {
  if (!session?.access_token) {
    localStorage.removeItem(AGENTIC_AUTH_STORAGE_KEY)
    return
  }
  localStorage.setItem(AGENTIC_AUTH_STORAGE_KEY, JSON.stringify(session))
}

const persistAgenticSession = async (apiBase: string, session: AgenticAuthSession) => {
  storeAgenticSession(session)
  await fetch(`${apiBase.replace(/\/+$/, "")}/auth/desktop-session`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(session),
  }).catch(() => undefined)
}

const clearAgenticSession = async (apiBase: string) => {
  storeAgenticSession(null)
  await fetch(`${apiBase.replace(/\/+$/, "")}/auth/logout`, {
    method: "POST",
    headers: { "content-type": "application/json" },
  }).catch(() => undefined)
}

const parseAgenticAuthDeepLink = (input: string): AgenticAuthSession | null => {
  if (!input.startsWith("agentic://")) return null
  let url: URL
  try {
    url = new URL(input)
  } catch {
    return null
  }
  if (url.hostname !== "auth-callback") return null
  const params = new URLSearchParams(url.hash.replace(/^#/, ""))
  const accessToken = params.get("access_token")
  if (!accessToken) return null
  const expiresAtRaw = params.get("expires_at")
  const expiresInRaw = params.get("expires_in")
  const expiresAt = expiresAtRaw ? Number(expiresAtRaw) : undefined
  const expiresIn = expiresInRaw ? Number(expiresInRaw) : undefined
  return {
    access_token: accessToken,
    refresh_token: params.get("refresh_token"),
    expires_at: Number.isFinite(expiresAt) ? expiresAt : undefined,
    expires_in: Number.isFinite(expiresIn) ? expiresIn : undefined,
    token_type: params.get("token_type") || "bearer",
    user: userFromAccessToken(accessToken),
  }
}

const responseError = async (response: Response, fallback: string) => {
  try {
    const body = await response.json()
    return body?.detail || body?.message || body?.error || fallback
  } catch {
    return fallback
  }
}

const agenticAuthHeaders = (session: AgenticAuthSession | null) => {
  const headers: Record<string, string> = { "content-type": "application/json" }
  if (session?.access_token) headers.authorization = `Bearer ${session.access_token}`
  return headers
}

const openAgenticWebFlow = (apiBase: string, path: string) => {
  window.api.openLink(`${apiBase.replace(/\/+$/, "")}${path}`)
}

const verifyAgenticLicense = async (
  apiBase: string,
  session: AgenticAuthSession | null,
): Promise<AgenticLicenseStatus> => {
  const response = await fetch(`${apiBase.replace(/\/+$/, "")}/license/status`, {
    headers: agenticAuthHeaders(session),
  })
  if (!response.ok) throw new Error(await responseError(response, "Unable to verify license. Please try again."))
  return (await response.json()) as AgenticLicenseStatus
}

const listenForDeepLinks = () => {
  void window.api.consumeInitialDeepLinks().then((urls) => emitDeepLinks(urls))
  return window.api.onDeepLink((urls) => emitDeepLinks(urls))
}

const createPlatform = (): Platform => {
  const os = (() => {
    const ua = navigator.userAgent
    if (ua.includes("Mac")) return "macos"
    if (ua.includes("Windows")) return "windows"
    if (ua.includes("Linux")) return "linux"
    return undefined
  })()

  const runDesktopMenuAction: Platform["runDesktopMenuAction"] = (action) => {
    switch (action) {
      case "view.resetZoom":
        resetZoom()
        return
      case "view.zoomIn":
        zoomIn()
        return
      case "view.zoomOut":
        zoomOut()
        return
    }

    return window.api.runDesktopMenuAction(action)
  }

  const storage = (() => {
    const cache = new Map<string, AsyncStorage>()

    const createStorage = (name: string) => {
      const api: AsyncStorage = {
        getItem: (key: string) => window.api.storeGet(name, key),
        setItem: (key: string, value: string) => window.api.storeSet(name, key, value),
        removeItem: (key: string) => window.api.storeDelete(name, key),
        clear: () => window.api.storeClear(name),
        key: async (index: number) => (await window.api.storeKeys(name))[index],
        getLength: () => window.api.storeLength(name),
        get length() {
          return api.getLength()
        },
      }
      return api
    }

    return (name = "default.dat") => {
      const cached = cache.get(name)
      if (cached) return cached
      const api = createStorage(name)
      cache.set(name, api)
      return api
    }
  })()

  const wslServersApi = os === "windows" ? window.api.wslServers : undefined

  return {
    platform: "desktop",
    os,
    version: pkg.version,

    async openDirectoryPickerDialog(opts) {
      return window.api.openDirectoryPicker({
        multiple: opts?.multiple ?? false,
        title: opts?.title ?? t("desktop.dialog.chooseFolder"),
      })
    },

    async openAttachmentPickerDialog(opts, onFile) {
      const result = await window.api.openFilePicker({
        multiple: opts?.multiple ?? false,
        title: opts?.title ?? t("desktop.dialog.chooseFile"),
        defaultPath: opts?.defaultPath,
        extensions: opts?.extensions ?? ACCEPTED_FILE_EXTENSIONS,
      })
      if (!result) return
      try {
        for (const file of result.files) {
          await onFile(new File([await window.api.readPickedFile(result.token, file.path)], file.name))
        }
      } finally {
        await window.api.releasePickedFiles(result.token)
      }
    },

    async saveFilePickerDialog(opts) {
      return window.api.saveFilePicker({
        title: opts?.title ?? t("desktop.dialog.saveFile"),
        defaultPath: opts?.defaultPath,
      })
    },

    openLink(url: string) {
      window.api.openLink(url)
    },
    async openPath(path: string, app?: string) {
      if (os === "windows") {
        const resolvedApp = app ? await window.api.resolveAppPath(app).catch(() => null) : null
        return window.api.openPath(path, resolvedApp ?? undefined)
      }
      return window.api.openPath(path, app)
    },

    back() {
      window.history.back()
    },

    forward() {
      window.history.forward()
    },

    storage,

    updater: {
      state: updaterState,
      check: () => window.api.updater.check(),
      install: () => window.api.updater.install(),
    },

    exportDebugLogs: () => window.api.exportDebugLogs(),

    recordFatalRendererError: (error) => window.api.recordFatalRendererError(error),

    restart: async () => {
      await window.api.killSidecar().catch(() => undefined)
      window.api.relaunch()
    },

    notify: async (title, description, href) => {
      const focused = await window.api.getWindowFocused().catch(() => document.hasFocus())
      if (focused) return

      const notification = new Notification(title, {
        body: description ?? "",
        icon: "https://opencode.ai/favicon-96x96-v3.png",
      })
      notification.onclick = () => {
        void window.api.showWindow()
        void window.api.setWindowFocus()
        handleNotificationClick(href)
        notification.close()
      }
    },

    fetch: (input, init) => {
      if (input instanceof Request) return fetch(input)
      return fetch(input, init)
    },

    getDefaultServer: async () => {
      const url = await window.api.getDefaultServerUrl().catch(() => null)
      if (!url) return null
      return ServerConnection.Key.make(url)
    },

    setDefaultServer: async (url: string | null) => {
      await window.api.setDefaultServerUrl(url)
    },

    wslServers: wslServersApi,

    getDisplayBackend: async () => {
      return window.api.getDisplayBackend().catch(() => null)
    },

    setDisplayBackend: async (backend) => {
      await window.api.setDisplayBackend(backend)
    },

    parseMarkdown: (markdown: string) => window.api.parseMarkdownCommand(markdown),

    webviewZoom,

    getPinchZoomEnabled: () => window.api.getPinchZoomEnabled(),

    setPinchZoomEnabled,

    runDesktopMenuAction,

    checkAppExists: async (appName: string) => {
      return window.api.checkAppExists(appName)
    },

    async readClipboardImage() {
      const image = await window.api.readClipboardImage().catch(() => null)
      if (!image) return null
      const blob = new Blob([image.buffer], { type: "image/png" })
      return new File([blob], `pasted-image-${Date.now()}.png`, {
        type: "image/png",
      })
    },
  }
}

let menuTrigger = null as null | ((id: string) => void)
window.api.onMenuCommand((id) => {
  menuTrigger?.(id)
})
listenForDeepLinks()

function AgenticAuthPage(props: {
  apiBase: string
  onAuth: (session: AgenticAuthSession) => Promise<void>
}) {
  const [busy, setBusy] = createSignal(false)
  const [success, setSuccess] = createSignal("")

  const openGoogle = () => {
    setBusy(true)
    setSuccess("Google sign-in opened in your browser. AgentIC will unlock automatically when the secure web flow returns.")
    window.api.openLink(AGENTIC_AUTH_START_URL)
    window.setTimeout(() => setBusy(false), 1200)
  }

  const openCheckout = () => {
    setBusy(true)
    setSuccess("Opening AgentIC pricing in your browser. Choose the plan that fits, then checkout will handle Google sign-in.")
    window.api.openLink(AGENTIC_PRICING_URL)
    window.setTimeout(() => setBusy(false), 1200)
  }

  return (
    <main class="min-h-dvh w-screen bg-background-base text-text-base flex">
      <section class="hidden lg:flex flex-1 flex-col justify-between px-14 py-12 border-r border-border-subtle bg-surface-base">
        <div class="flex items-center gap-3 text-text-strong">
          <Splash class="w-9 h-11" />
          <span class="text-18-bold">AgentIC</span>
        </div>
        <div class="max-w-xl">
          <p class="text-13-medium uppercase tracking-[0.18em] text-text-muted mb-5">Local-first silicon workspace</p>
          <h1 class="text-[48px] leading-[1.02] font-semibold text-text-strong mb-6">
            Sign in before the agent opens your chip workspace.
          </h1>
          <p class="text-16-regular text-text-base leading-7">
            AgentIC verifies your license before enabling VLSI context, EDA execution, and local build tools. Your
            source files, prompts, logs, PDK files, and artifacts stay on this machine.
          </p>
        </div>
        <div class="grid grid-cols-3 gap-3 text-12-regular text-text-weak">
          <div class="rounded-lg border border-border-subtle p-4 bg-background-base">Account and license check only</div>
          <div class="rounded-lg border border-border-subtle p-4 bg-background-base">Local AgentIC workspace</div>
          <div class="rounded-lg border border-border-subtle p-4 bg-background-base">BYOK model access</div>
        </div>
      </section>

      <section class="flex min-h-dvh flex-1 items-center justify-center px-6 py-10">
        <div class="w-full max-w-[420px]">
          <div class="mb-8 lg:hidden flex items-center gap-3 text-text-strong">
            <Splash class="w-9 h-11" />
            <span class="text-18-bold">AgentIC</span>
          </div>
          <h2 class="text-28-bold text-text-strong mb-2">Sign in with AgentIC</h2>
          <p class="text-14-regular text-text-weak mb-7">
            Account creation, Google sign-in, and subscription checkout happen on the secure Buildstack account site.
            The desktop app only receives the returned session and runs your silicon workspace locally.
          </p>

          <button
            type="button"
            disabled={busy()}
            class="h-11 w-full rounded-md bg-text-strong text-background-base text-14-medium disabled:opacity-50"
            onClick={openGoogle}
          >
            Continue with Google
          </button>

          <button
            type="button"
            disabled={busy()}
            class="mt-3 h-11 w-full rounded-md border border-border-subtle bg-surface-base text-14-medium text-text-strong disabled:opacity-50"
            onClick={openCheckout}
          >
            Buy AgentIC
          </button>
          <Show when={success()}>
            <div class="mt-5 rounded-md border border-border-subtle bg-surface-raised px-3 py-2 text-13-regular text-text-base">
              {success()}
            </div>
          </Show>
        </div>
      </section>
    </main>
  )
}

function AgenticLicensePage(props: {
  apiBase: string
  session: AgenticAuthSession | null
  status: AgenticLicenseStatus | null
  checking: boolean
  onRecheck: () => Promise<void>
  onSignOut: () => Promise<void>
}) {
  const [busy, setBusy] = createSignal(false)
  const [message, setMessage] = createSignal("")
  const reason = createMemo(() => {
    if (props.status?.source === "cloud_unauthorized") return "Please sign in again to continue."
    return props.status?.reason || "No active paid license was found for this account."
  })

  const purchase = async () => {
    setBusy(true)
    setMessage("")
    try {
      window.api.openLink(AGENTIC_PRICING_URL)
      setMessage("Pricing opened in your browser. Pick a plan there; after checkout, AgentIC will return here through the secure desktop callback.")
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Unable to open purchase. Please try again.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <main class="min-h-dvh w-screen bg-background-base text-text-base flex items-center justify-center px-6 py-10">
      <section class="w-full max-w-[520px] rounded-lg border border-border-subtle bg-surface-base p-8 text-center">
        <Splash class="mx-auto mb-5 w-12 h-15" />
        <p class="text-13-medium uppercase tracking-[0.18em] text-text-muted mb-3">License required</p>
        <h1 class="text-32-bold text-text-strong mb-3">
          {props.status?.source === "cloud_unauthorized" ? "Sign in again" : "Finish your AgentIC purchase"}
        </h1>
        <p class="text-14-regular text-text-base leading-6 mb-5">
          AgentIC Desktop runs the chip workspace locally, but the VLSI agent and bridge tools unlock only after the
          server verifies an active account.
        </p>
        <div class="rounded-md border border-border-subtle bg-background-base px-4 py-3 text-13-regular text-text-base mb-5">
          {reason()}
        </div>
        <Show when={message()}>
          <div class="rounded-md border border-border-subtle bg-surface-raised px-4 py-3 text-13-regular text-text-base mb-5">
            {message()}
          </div>
        </Show>
        <div class="flex flex-col sm:flex-row justify-center gap-3">
          <button
            type="button"
            disabled={busy()}
            class="h-10 rounded-md bg-text-strong text-background-base px-4 text-14-medium disabled:opacity-50"
            onClick={() => void purchase()}
          >
            {busy() ? "Opening pricing..." : "View Pricing"}
          </button>
          <button
            type="button"
            disabled={props.checking}
            class="h-10 rounded-md border border-border-subtle bg-background-base px-4 text-14-medium text-text-strong disabled:opacity-50"
            onClick={() => void props.onRecheck()}
          >
            {props.checking ? "Checking..." : "Recheck License"}
          </button>
          <button
            type="button"
            class="h-10 rounded-md border border-border-subtle bg-background-base px-4 text-14-medium text-text-base"
            onClick={() => void props.onSignOut()}
          >
            Sign Out
          </button>
        </div>
      </section>
    </main>
  )
}

function AgenticAuthGate(props: { apiBase: string; children: JSX.Element }) {
  const [session, setSession] = createSignal<AgenticAuthSession | null>(readStoredAgenticSession())
  const [licenseStatus, setLicenseStatus] = createSignal<AgenticLicenseStatus | null>(null)
  const [licenseChecking, setLicenseChecking] = createSignal(false)

  const acceptSession = async (next: AgenticAuthSession) => {
    await persistAgenticSession(props.apiBase, next)
    setSession(next)
  }

  const recheckLicense = async () => {
    if (!session()?.access_token) return
    setLicenseChecking(true)
    try {
      setLicenseStatus(await verifyAgenticLicense(props.apiBase, session()))
    } catch (err) {
      setLicenseStatus({
        active: false,
        source: "unavailable",
        reason: err instanceof Error ? err.message : "Unable to verify license. Please try again.",
      })
    } finally {
      setLicenseChecking(false)
    }
  }

  const signOut = async () => {
    await clearAgenticSession(props.apiBase)
    setLicenseStatus(null)
    setSession(null)
  }

  onMount(() => {
    const processUrls = (urls: string[]) => {
      for (const url of urls) {
        const next = parseAgenticAuthDeepLink(url)
        if (next?.access_token) void acceptSession(next)
      }
    }

    processUrls(window.__OPENCODE__?.deepLinks ?? [])
    const handler = (event: Event) => {
      const urls = (event as CustomEvent<{ urls?: string[] }>).detail?.urls ?? []
      processUrls(urls)
    }
    window.addEventListener(deepLinkEvent, handler)

    const existing = session()
    if (existing?.access_token) void persistAgenticSession(props.apiBase, existing)

    onCleanup(() => window.removeEventListener(deepLinkEvent, handler))
  })

  createEffect(() => {
    localStorage.setItem(AGENTIC_API_BASE_STORAGE_KEY, props.apiBase)
    if (!session()?.access_token) {
      setLicenseStatus(null)
      return
    }
    void recheckLicense()
  })

  return (
    <Show when={session()?.access_token} fallback={<AgenticAuthPage apiBase={props.apiBase} onAuth={acceptSession} />}>
      <Show
        when={!licenseChecking() || licenseStatus()}
        fallback={
          <div class="h-dvh w-screen flex flex-col items-center justify-center bg-background-base">
            <Splash class="w-16 h-20 opacity-50 animate-pulse" />
            <span class="mt-4 text-13-regular text-text-weak">Verifying AgentIC license...</span>
          </div>
        }
      >
        <Show
          when={licenseStatus()?.active}
          fallback={
            <AgenticLicensePage
              apiBase={props.apiBase}
              session={session()}
              status={licenseStatus()}
              checking={licenseChecking()}
              onRecheck={recheckLicense}
              onSignOut={signOut}
            />
          }
        >
          {props.children}
        </Show>
      </Show>
    </Show>
  )
}

render(() => {
  const platform = createPlatform()
  const loadLocale = async () => {
    const current = await platform.storage?.("opencode.global.dat").getItem("language")
    const legacy = current ? undefined : await platform.storage?.().getItem("language.v1")
    const raw = current ?? legacy
    if (!raw) return
    const locale = raw.match(/"locale"\s*:\s*"([^"]+)"/)?.[1]
    if (!locale) return
    const next = normalizeLocale(locale)
    if (next !== "en") await loadLocaleDict(next)
    return next satisfies Locale
  }

  const [windowCount] = createResource(() => window.api.getWindowCount())

  // Fetch sidecar credentials (available immediately, before health check)
  const [sidecar] = createResource(() => window.api.awaitInitialization())

  const [defaultServer] = createResource(() => platform.getDefaultServer?.())
  const [locale] = createResource(loadLocale)

  function handleClick(e: MouseEvent) {
    const link = (e.target as HTMLElement).closest("a.external-link") as HTMLAnchorElement | null
    if (link?.href) {
      e.preventDefault()
      platform.openLink(link.href)
    }
  }

  function Inner() {
    const cmd = useCommand()
    menuTrigger = (id) => cmd.trigger(id)

    const theme = useTheme()

    createEffect(() => {
      theme.themeId()
      theme.mode()
      const bg = getComputedStyle(document.documentElement).getPropertyValue("--background-base").trim()
      if (bg) {
        void window.api.setBackgroundColor(bg)
      }
    })

    return null
  }

  function App() {
    const wslServers = useWslServers()
    const splash = (
      <div class="h-dvh w-screen flex flex-col items-center justify-center bg-background-base">
        <Splash class="w-16 h-20 opacity-50 animate-pulse" />
      </div>
    )

    const ready = createMemo(
      () => !defaultServer.loading && !sidecar.loading && !windowCount.loading && !locale.loading,
    )
    const servers = createMemo(() => {
      const data = initializationData(sidecar)
      const list: ServerConnection.Any[] = []
      if (data) {
        list.push({
          displayName: "Local Server",
          type: "sidecar",
          variant: "base",
          http: {
            url: data.url,
            username: data.username ?? undefined,
            password: data.password ?? undefined,
          },
        })
      }
      list.push(...readyWslConnections(wslServers.data))
      return list
    })
    const effectiveDefaultServer = createMemo(() =>
      ServerConnection.Key.make(availableStartupServer(defaultServer.latest, wslServers.data)),
    )

    return (
      <Show when={ready()} fallback={splash}>
        <Show when={initializationData(sidecar)} keyed>
          {(data) => (
            <AgenticAuthGate apiBase={data.agenticUrl ?? data.url}>
              <Show when={effectiveDefaultServer()} keyed>
                {(key) => (
                  <AppInterface defaultServer={key} servers={servers()} router={MemoryRouter}>
                    <Inner />
                  </AppInterface>
                )}
              </Show>
            </AgenticAuthGate>
          )}
        </Show>
      </Show>
    )
  }

  onMount(() => {
    document.addEventListener("click", handleClick)
    onCleanup(() => {
      document.removeEventListener("click", handleClick)
    })
  })

  return (
    <PlatformProvider value={platform}>
      <AppBaseProviders locale={locale.latest}>
        <Show when={true}>{(_) => <App />}</Show>
      </AppBaseProviders>
    </PlatformProvider>
  )
}, root!)
