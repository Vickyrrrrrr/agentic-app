import { createResource, createSignal, For, Show } from "solid-js"
import { Icon } from "@opencode-ai/ui/icon"
import { useSessionLayout } from "@/pages/session/session-layout"
import { getAgenticBase } from "@/utils/agentic"

interface STAPath {
  startpoint: string
  endpoint: string
  slack: number
  delay?: number | null
  path_group?: string | null
  severity?: string
  message?: string | null
}

interface STAResponse {
  status?: "ready" | "violating" | "missing"
  wns: number | null
  tns: number | null
  paths: STAPath[]
  source?: { type?: string; path?: string; tool?: string; stage?: string } | null
  message?: string
}

function normalizeSTAResponse(raw: unknown): STAResponse {
  const parsed = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {}
  const paths = Array.isArray(parsed.paths) ? parsed.paths : []
  return {
    status: parsed.status === "violating" || parsed.status === "missing" || parsed.status === "ready" ? parsed.status : undefined,
    wns: typeof parsed.wns === "number" ? parsed.wns : null,
    tns: typeof parsed.tns === "number" ? parsed.tns : null,
    paths: paths.flatMap((item) => {
      if (!item || typeof item !== "object") return []
      const path = item as Record<string, unknown>
      return [{
        startpoint: typeof path.startpoint === "string" ? path.startpoint : "unknown",
        endpoint: typeof path.endpoint === "string" ? path.endpoint : "unknown",
        slack: typeof path.slack === "number" ? path.slack : 0,
        delay: typeof path.delay === "number" ? path.delay : null,
        path_group: typeof path.path_group === "string" ? path.path_group : null,
        severity: typeof path.severity === "string" ? path.severity : undefined,
        message: typeof path.message === "string" ? path.message : null,
      }]
    }),
    source: parsed.source && typeof parsed.source === "object" ? (parsed.source as STAResponse["source"]) : null,
    message: typeof parsed.message === "string" ? parsed.message : undefined,
  }
}

export function STASlackWidget() {
  const { params } = useSessionLayout()
  const [open, setOpen] = createSignal(false)
  const [fetchError, setFetchError] = createSignal<string | null>(null)

  const [data] = createResource(
    () => params.id,
    async (sessionId) => {
      setFetchError(null)
      try {
        const response = await fetch(`${getAgenticBase()}/build/sta/session/${encodeURIComponent(sessionId)}`)
        if (!response.ok) throw new Error(`STA endpoint ${response.status}: ${response.statusText}`)
        return normalizeSTAResponse(await response.json())
      } catch (e) {
        setFetchError(e instanceof Error ? e.message : String(e))
        return null
      }
    },
  )

  const hasEvidence = () => !!data() && data()?.status !== "missing" && data()?.wns !== null
  const isViolating = () => data()?.status === "violating" || (data()?.wns ?? 0) < 0
  const sourceLabel = () => {
    const source = data()?.source
    if (!source) return data()?.message ?? "No STA evidence found"
    if (source.type === "file" && source.path) return source.path
    if (source.type === "checkpoint") return [source.tool, source.stage].filter(Boolean).join(" / ") || "checkpoint"
    return source.type ?? "STA evidence"
  }

  return (
    <div class="relative font-sans">
      <button
        onClick={() => setOpen(!open())}
        class="flex items-center gap-1.5 px-2 py-1 text-12-regular border border-border-weaker-base rounded-sm cursor-pointer transition-none"
        classList={{
          "text-text-weak": data.loading,
          "text-text-on-critical-base bg-surface-critical-base border-border-critical-base": !data.loading && isViolating(),
          "text-text-on-success-base bg-surface-success-base border-border-success-base": !data.loading && !isViolating() && hasEvidence(),
          "text-text-weak bg-surface-base border-border-weaker-base": !data.loading && !hasEvidence(),
        }}
      >
        <Icon
          name={data.loading || !hasEvidence() ? "help" : isViolating() ? "warning" : "check"}
          size="small"
          class="shrink-0"
          classList={{
            "text-icon-weak": data.loading || !hasEvidence(),
          }}
        />
        <span>
          {data.loading
            ? "STA..."
            : hasEvidence()
              ? `WNS: ${data()!.wns! > 0 ? "+" : ""}${data()!.wns!.toFixed(2)} ns`
              : "STA --"}
        </span>
      </button>

      <Show when={fetchError()}>
        <div class="absolute top-full left-0 mt-1 bg-background-stronger border border-border-critical-base px-2 py-1 text-11-regular text-text-on-critical-base z-50 rounded-sm whitespace-nowrap">
          {fetchError()}
        </div>
      </Show>

      <Show when={open() && data()}>
        <div class="absolute top-full right-0 mt-1 bg-background-stronger border border-border-base z-50 w-96 rounded-sm shadow-[var(--v2-elevation-raised)]">
          <div class="px-3 py-1.5 text-11-regular text-text-weaker border-b border-border-weaker-base uppercase tracking-wider">
            Critical Timing Paths
          </div>
          <div class="px-3 py-1.5 text-11-regular text-text-weaker border-b border-border-weaker-base truncate" title={sourceLabel()}>
            Source: {sourceLabel()}
          </div>
          <div class="max-h-72 overflow-y-auto">
            <table class="w-full text-11-regular border-collapse">
              <thead>
                <tr class="text-text-weaker text-left border-b border-border-weaker-base">
                  <th class="px-3 py-1 font-normal">Startpoint</th>
                  <th class="px-3 py-1 font-normal">Endpoint</th>
                  <th class="px-3 py-1 font-normal text-right">Slack</th>
                  <th class="px-3 py-1 font-normal text-right">Delay</th>
                </tr>
              </thead>
              <tbody>
                <For each={data()!.paths}>
                  {(path) => (
                    <tr class="border-b border-border-weaker-base last:border-b-0 hover:bg-surface-base">
                      <td class="px-3 py-1 text-text-strong font-mono max-w-32 truncate" title={path.startpoint}>{path.startpoint}</td>
                      <td class="px-3 py-1 text-text-strong font-mono max-w-32 truncate" title={path.endpoint}>{path.endpoint}</td>
                      <td class="px-3 py-1 text-right font-mono" classList={{ "text-text-on-critical-base": path.slack < 0, "text-text-on-success-base": path.slack >= 0 }}>{path.slack.toFixed(2)}</td>
                      <td class="px-3 py-1 text-right font-mono text-text-weak">{typeof path.delay === "number" ? path.delay.toFixed(2) : "--"}</td>
                    </tr>
                  )}
                </For>
              </tbody>
            </table>
          </div>
          <Show when={data()?.paths.length === 0}>
            <div class="px-3 py-2 text-11-regular text-text-weaker">
              {hasEvidence() ? "No detailed paths in report" : "Run STA or place a timing report under sta/ or reports/."}
            </div>
          </Show>
          <div class="px-3 py-1 border-t border-border-weaker-base text-11-regular text-text-weaker text-right">
            TNS: <span classList={{ "text-text-on-critical-base": (data()?.tns ?? 0) < 0, "text-text-on-success-base": (data()?.tns ?? 0) >= 0 }}>{typeof data()?.tns === "number" ? data()!.tns!.toFixed(2) : "--"} ns</span>
          </div>
        </div>
      </Show>
    </div>
  )
}
