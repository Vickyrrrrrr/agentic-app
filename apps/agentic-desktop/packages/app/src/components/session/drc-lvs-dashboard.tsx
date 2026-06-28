import { createResource, createSignal, For, Show, Switch, Match } from "solid-js"
import { Icon } from "@opencode-ai/ui/icon"
import { useSessionLayout } from "@/pages/session/session-layout"
import { getAgenticBase } from "@/utils/agentic"

type SignoffStatus = "clean" | "violating" | "missing" | "partial" | "unknown"

type EvidenceSource = {
  type?: string
  path?: string
  tool?: string
  stage?: string
}

type Diagnostic = {
  severity?: string
  rule?: string
  count?: number
  message?: string
  log_line?: number
}

type Evidence = {
  kind: "drc" | "lvs"
  status: SignoffStatus
  source?: EvidenceSource | null
  metrics: Record<string, unknown>
  summary: { diagnostic_count?: number; error_count?: number; warning_count?: number }
  diagnostics: Diagnostic[]
  message?: string
}

type SignoffResponse = {
  status: SignoffStatus
  drc: Evidence
  lvs: Evidence
  message?: string
}

const emptyEvidence = (kind: "drc" | "lvs"): Evidence => ({
  kind,
  status: "missing",
  source: null,
  metrics: {},
  summary: {},
  diagnostics: [],
})

function normalizeEvidence(raw: unknown, kind: "drc" | "lvs"): Evidence {
  const item = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {}
  const diagnostics = Array.isArray(item.diagnostics) ? item.diagnostics : []
  const summary = item.summary && typeof item.summary === "object" ? item.summary as Evidence["summary"] : {}
  return {
    kind,
    status: isStatus(item.status) ? item.status : "unknown",
    source: item.source && typeof item.source === "object" ? item.source as EvidenceSource : null,
    metrics: item.metrics && typeof item.metrics === "object" ? item.metrics as Record<string, unknown> : {},
    summary,
    diagnostics: diagnostics.flatMap((rawDiagnostic) => {
      if (!rawDiagnostic || typeof rawDiagnostic !== "object") return []
      const diagnostic = rawDiagnostic as Record<string, unknown>
      return [{
        severity: typeof diagnostic.severity === "string" ? diagnostic.severity : undefined,
        rule: typeof diagnostic.rule === "string" ? diagnostic.rule : undefined,
        count: typeof diagnostic.count === "number" ? diagnostic.count : undefined,
        message: typeof diagnostic.message === "string" ? diagnostic.message : undefined,
        log_line: typeof diagnostic.log_line === "number" ? diagnostic.log_line : undefined,
      }]
    }),
    message: typeof item.message === "string" ? item.message : undefined,
  }
}

function normalizeSignoff(raw: unknown): SignoffResponse {
  const parsed = raw && typeof raw === "object" ? raw as Record<string, unknown> : {}
  return {
    status: isStatus(parsed.status) ? parsed.status : "unknown",
    drc: normalizeEvidence(parsed.drc, "drc"),
    lvs: normalizeEvidence(parsed.lvs, "lvs"),
    message: typeof parsed.message === "string" ? parsed.message : undefined,
  }
}

function isStatus(value: unknown): value is SignoffStatus {
  return value === "clean" || value === "violating" || value === "missing" || value === "partial" || value === "unknown"
}

function sourceLabel(source?: EvidenceSource | null) {
  if (!source) return "no evidence"
  if (source.type === "file" && source.path) return source.path
  if (source.type === "checkpoint") return [source.tool, source.stage].filter(Boolean).join(" / ") || "checkpoint"
  return source.type ?? "evidence"
}

function metricValue(value: unknown) {
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(3)
  if (typeof value === "boolean") return value ? "yes" : "no"
  if (typeof value === "string") return value
  return "--"
}

export function DRCLVSDashboard() {
  const { params } = useSessionLayout()
  const [active, setActive] = createSignal<"drc" | "lvs" | "timing">("drc")
  const [fetchError, setFetchError] = createSignal<string | null>(null)

  const [data] = createResource(
    () => params.id,
    async (sessionId) => {
      setFetchError(null)
      try {
        const response = await fetch(`${getAgenticBase()}/build/signoff/session/${encodeURIComponent(sessionId)}`)
        if (!response.ok) throw new Error(`Signoff endpoint ${response.status}: ${response.statusText}`)
        return normalizeSignoff(await response.json())
      } catch (e) {
        setFetchError(e instanceof Error ? e.message : String(e))
        return {
          status: "missing" as const,
          drc: emptyEvidence("drc"),
          lvs: emptyEvidence("lvs"),
          message: "No DRC/LVS evidence loaded.",
        }
      }
    },
  )

  const [timingData] = createResource(
    () => params.id,
    async (sessionId) => {
      try {
        const response = await fetch(`${getAgenticBase()}/build/sta/session/${encodeURIComponent(sessionId)}`)
        if (!response.ok) throw new Error(`Timing endpoint ${response.status}: ${response.statusText}`)
        return await response.json()
      } catch {
        return {
          status: "missing",
          wns: null,
          tns: null,
          paths: [],
          message: "No timing report loaded.",
        }
      }
    }
  )

  const report = () => data()
  const current = () => (active() === "drc" ? report()?.drc : report()?.lvs) ?? emptyEvidence(active() as "drc" | "lvs")

  const timingEvidence = () => {
    const t = timingData()
    return {
      kind: "timing" as const,
      status: t?.status === "ready" ? "clean" as const : t?.status === "violating" ? "violating" as const : "missing" as const,
      source: t?.source,
      metrics: {
        wns_ns: t?.wns ?? "--",
        tns_ns: t?.tns ?? "--",
      },
      diagnostics: [],
    }
  }

  const activeEvidence = () => {
    if (active() === "timing") return timingEvidence()
    return current()
  }

  const overallStatus = () => {
    if (data.loading || timingData.loading) return "unknown"
    if (report()?.status === "violating" || timingData()?.status === "violating") return "violating"
    if (report()?.status === "clean" && timingData()?.status === "ready") return "clean"
    return "missing"
  }

  return (
    <div class="flex h-full flex-col bg-background-stronger font-sans">
      <div class="border-b border-border-weaker-base px-3 py-2">
        <div class="flex items-center justify-between gap-2">
          <div class="text-12-medium text-text-strong">Verification & Signoff</div>
          <StatusPill status={overallStatus()} />
        </div>
        <div class="mt-1 text-11-regular text-text-weaker">
          {data.loading || timingData.loading ? "Loading verification reports..." : (active() === "timing" ? (timingData()?.message || "No timing report loaded.") : (report()?.message || "No signoff evidence loaded."))}
        </div>
      </div>

      <Show when={fetchError()}>
        <div class="border-b border-border-critical-base px-3 py-1.5 text-11-regular text-text-on-critical-base">
          {fetchError()}
        </div>
      </Show>

      <div class="grid grid-cols-3 border-b border-border-weaker-base">
        <EvidenceTab label="DRC" evidence={report()?.drc ?? emptyEvidence("drc")} active={active() === "drc"} onClick={() => setActive("drc")} />
        <EvidenceTab label="LVS" evidence={report()?.lvs ?? emptyEvidence("lvs")} active={active() === "lvs"} onClick={() => setActive("lvs")} />
        <EvidenceTab label="Timing" evidence={timingEvidence() as any} active={active() === "timing"} onClick={() => setActive("timing")} />
      </div>

      <div class="border-b border-border-weaker-base px-3 py-1.5 text-11-regular text-text-weaker">
        <span class="uppercase tracking-wide">{active()}</span>
        <span class="px-1">/</span>
        <span class="font-mono" title={sourceLabel(activeEvidence().source)}>{sourceLabel(activeEvidence().source)}</span>
      </div>

      <div class="grid grid-cols-2 border-b border-border-weaker-base">
        <For each={Object.entries(activeEvidence().metrics).slice(0, 6)}>
          {([key, value]) => (
            <div class="min-w-0 border-r border-b border-border-weaker-base px-3 py-1.5 last:border-r-0">
              <div class="truncate text-10-uppercase text-text-weaker">{key.replaceAll("_", " ")}</div>
              <div class="truncate font-mono text-12-medium text-text-strong" style={key.includes("wns") && typeof value === "number" && value < 0 ? { color: "#ef4444" } : {}}>{metricValue(value)}</div>
            </div>
          )}
        </For>
        <Show when={Object.keys(activeEvidence().metrics).length === 0}>
          <div class="col-span-2 px-3 py-2 text-11-regular text-text-weaker">No metrics extracted.</div>
        </Show>
      </div>

      <div class="min-h-0 flex-1 overflow-y-auto">
        <Switch>
          <Match when={active() === "timing"}>
            <Show
              when={(timingData()?.paths?.length ?? 0) > 0}
              fallback={
                <div class="px-3 py-4 text-12-regular text-text-weaker">
                  {timingEvidence().status === "missing"
                    ? "No timing reports found under sta/, reports/, or runs/."
                    : "No setup/hold timing violations found."}
                </div>
              }
            >
              <table class="w-full table-fixed border-collapse text-11-regular">
                <thead class="sticky top-0 bg-background-stronger">
                  <tr class="border-b border-border-weaker-base text-left text-text-weaker">
                    <th class="w-20 px-3 py-1 font-normal">Slack (ns)</th>
                    <th class="px-3 py-1 font-normal">Startpoint</th>
                    <th class="px-3 py-1 font-normal">Endpoint</th>
                    <th class="w-18 px-3 py-1 text-right font-normal">Delay</th>
                  </tr>
                </thead>
                <tbody>
                  <For each={timingData()?.paths}>
                    {(path: any) => (
                      <tr class="border-b border-border-weaker-base align-top hover:bg-surface-base">
                        <td
                          class="px-3 py-1 font-mono font-semibold"
                          style={{ color: path.slack < 0 ? "#ef4444" : "var(--color-text-on-success-base, #10b981)" }}
                        >
                          {path.slack.toFixed(3)}
                        </td>
                        <td class="truncate px-3 py-1 font-mono text-text-strong" title={path.startpoint}>
                          {path.startpoint}
                        </td>
                        <td class="truncate px-3 py-1 font-mono text-text-strong" title={path.endpoint}>
                          {path.endpoint}
                        </td>
                        <td class="px-3 py-1 text-right font-mono text-text-weaker">
                          {typeof path.delay === "number" ? path.delay.toFixed(3) : "--"}
                        </td>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
            </Show>
          </Match>
          <Match when={true}>
            <Show
              when={current().diagnostics.length > 0}
              fallback={
                <div class="px-3 py-4 text-12-regular text-text-weaker">
                  {current().status === "missing"
                    ? `No ${active().toUpperCase()} report found under signoff/, reports/, or runs/.`
                    : `No ${active().toUpperCase()} diagnostics in the selected evidence.`}
                </div>
              }
            >
              <table class="w-full table-fixed border-collapse text-11-regular">
                <thead class="sticky top-0 bg-background-stronger">
                  <tr class="border-b border-border-weaker-base text-left text-text-weaker">
                    <th class="w-16 px-3 py-1 font-normal">Line</th>
                    <th class="w-32 px-3 py-1 font-normal">{active() === "drc" ? "Rule" : "Class"}</th>
                    <th class="px-3 py-1 font-normal">Message</th>
                    <th class="w-14 px-3 py-1 text-right font-normal">Count</th>
                  </tr>
                </thead>
                <tbody>
                  <For each={current().diagnostics}>
                    {(item) => (
                      <tr class="border-b border-border-weaker-base align-top hover:bg-surface-base">
                        <td class="px-3 py-1 font-mono text-text-weaker">{item.log_line ?? "--"}</td>
                        <td class="truncate px-3 py-1 font-mono text-text-strong" title={item.rule ?? item.severity ?? ""}>
                          {item.rule ?? item.severity ?? "--"}
                        </td>
                        <td class="px-3 py-1 text-text-strong">{item.message ?? "--"}</td>
                        <td class="px-3 py-1 text-right font-mono text-text-weaker">{item.count ?? "--"}</td>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
            </Show>
          </Match>
        </Switch>
      </div>
    </div>
  )
}

function EvidenceTab(props: { label: string; evidence: Evidence; active: boolean; onClick: () => void }) {
  return (
    <button
      class="min-w-0 border-r border-border-weaker-base px-3 py-2 text-left last:border-r-0"
      classList={{
        "bg-surface-base": props.active,
        "hover:bg-surface-base": !props.active,
      }}
      onClick={props.onClick}
    >
      <div class="flex items-center justify-between gap-2">
        <span class="text-12-medium text-text-strong">{props.label}</span>
        <StatusPill status={props.evidence.status} compact />
      </div>
      <div class="mt-1 truncate text-11-regular text-text-weaker" title={sourceLabel(props.evidence.source)}>
        {sourceLabel(props.evidence.source)}
      </div>
    </button>
  )
}

function StatusPill(props: { status: SignoffStatus; compact?: boolean }) {
  const label = () => props.status === "clean" ? "clean" : props.status === "violating" ? "fail" : props.status
  return (
    <span
      class="inline-flex shrink-0 items-center gap-1 border px-1.5 py-0.5 font-mono text-10-uppercase"
      classList={{
        "border-border-success-base text-text-on-success-base": props.status === "clean",
        "border-border-critical-base text-text-on-critical-base": props.status === "violating",
        "border-border-weaker-base text-text-weaker": props.status !== "clean" && props.status !== "violating",
      }}
    >
      <Icon name={props.status === "clean" ? "check" : props.status === "violating" ? "warning" : "help"} size="small" />
      <Show when={!props.compact}>{label()}</Show>
    </span>
  )
}
