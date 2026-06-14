import { Component, Show, createMemo, createResource } from "solid-js"
import { ButtonV2 } from "@opencode-ai/ui/v2/button-v2"
import { Tag } from "@opencode-ai/ui/v2/badge-v2"
import { usePlatform } from "@/context/platform"
import { SettingsListV2 } from "./parts/list"
import { SettingsRowV2 } from "./parts/row"
import "./settings-v2.css"

const AGENTIC_AUTH_STORAGE_KEY = "agentic_auth_session"
const AGENTIC_API_BASE_STORAGE_KEY = "agentic_local_api_base"
const AGENTIC_PRICING_URL = "https://www.buildstack.live/agentic/pricing"

type AgenticProfile = {
  authenticated?: boolean
  expires_at?: number | string | null
  user?: {
    id?: string | null
    email?: string | null
  } | null
  license?: {
    active?: boolean
    plan?: string | null
    source?: string | null
    reason?: string | null
    expires_at?: number | string | null
  } | null
}

type AgenticSession = {
  access_token?: string | null
}

const readJson = <T,>(key: string): T | null => {
  try {
    const raw = localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : null
  } catch {
    return null
  }
}

const apiBase = () => localStorage.getItem(AGENTIC_API_BASE_STORAGE_KEY)?.replace(/\/+$/, "") || ""

const authHeaders = () => {
  const session = readJson<AgenticSession>(AGENTIC_AUTH_STORAGE_KEY)
  const headers: Record<string, string> = { "content-type": "application/json" }
  if (session?.access_token) headers.authorization = `Bearer ${session.access_token}`
  return headers
}

const loadProfile = async (): Promise<AgenticProfile> => {
  const base = apiBase()
  if (!base) return { authenticated: false, license: { active: false, reason: "AgentIC local backend is not ready." } }
  const response = await fetch(`${base}/auth/profile`, { headers: authHeaders() })
  if (!response.ok) {
    return {
      authenticated: false,
      license: {
        active: false,
        reason: response.status === 401 ? "Sign in again to refresh your account session." : "Unable to load profile.",
      },
    }
  }
  return (await response.json()) as AgenticProfile
}

export const SettingsProfileV2: Component = () => {
  const platform = usePlatform()
  const [profile, { refetch }] = createResource(loadProfile)

  const signedIn = createMemo(() => profile.latest?.authenticated === true)
  const license = createMemo(() => profile.latest?.license)
  const active = createMemo(() => license()?.active === true)
  const email = createMemo(() => profile.latest?.user?.email || "Not signed in")
  const plan = createMemo(() => license()?.plan || (active() ? "active" : "none"))
  const statusCopy = createMemo(() => {
    if (profile.loading) return "Checking"
    if (active()) return "Active"
    if (signedIn()) return "Signed in"
    return "Not connected"
  })

  const signOut = async () => {
    const base = apiBase()
    if (base) {
      await fetch(`${base}/auth/logout`, { method: "POST", headers: authHeaders() }).catch(() => undefined)
    }
    localStorage.removeItem(AGENTIC_AUTH_STORAGE_KEY)
    window.location.reload()
  }

  return (
    <>
      <div class="settings-v2-tab-header">
        <h2 class="settings-v2-tab-title">Profile</h2>
      </div>

      <div class="settings-v2-tab-body">
        <div class="settings-v2-section">
          <h3 class="settings-v2-section-title">AgentIC account</h3>
          <SettingsListV2>
            <SettingsRowV2 title="Account" description={email()}>
              <div class="flex items-center gap-2">
                <Tag>{statusCopy()}</Tag>
              </div>
            </SettingsRowV2>

            <SettingsRowV2
              title="License"
              description={active() ? `Plan: ${plan()}` : license()?.reason || "No active subscription found."}
            >
              <div class="flex items-center gap-2">
                <Tag>{active() ? "Enabled" : "Locked"}</Tag>
              </div>
            </SettingsRowV2>

            <SettingsRowV2
              title="Subscription"
              description="Plan selection and payment are handled securely on Buildstack."
            >
              <ButtonV2 size="normal" variant="neutral" onClick={() => platform.openLink(AGENTIC_PRICING_URL)}>
                View Pricing
              </ButtonV2>
            </SettingsRowV2>

            <SettingsRowV2 title="Refresh" description="Reload your account and entitlement from the local bridge.">
              <ButtonV2 size="normal" variant="ghost-muted" disabled={profile.loading} onClick={() => void refetch()}>
                {profile.loading ? "Checking" : "Recheck"}
              </ButtonV2>
            </SettingsRowV2>

            <Show when={signedIn()}>
              <SettingsRowV2 title="Session" description="Remove the local AgentIC account token from this machine.">
                <ButtonV2 size="normal" variant="ghost-muted" onClick={() => void signOut()}>
                  Sign Out
                </ButtonV2>
              </SettingsRowV2>
            </Show>
          </SettingsListV2>
        </div>
      </div>
    </>
  )
}
