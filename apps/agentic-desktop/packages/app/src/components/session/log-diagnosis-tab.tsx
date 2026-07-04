import { createResource, For, Show, createMemo } from "solid-js"
import { Icon } from "@opencode-ai/ui/icon"
import { showToast } from "@/utils/toast"
import { callAgenticTool } from "@/utils/agentic"
import { decode64 } from "@/utils/base64"
import { useSessionLayout } from "@/pages/session/session-layout"

type Diagnostic = {
  severity?: string
  message?: string
  file?: string
  line?: number
  log_line?: number
}

type LogDiagnosis = {
  path: string
  tool: string
  stage: string
  line_count: number
  summary: {
    diagnostic_count?: number
    error_count?: number
    warning_count?: number
  }
  metrics: Record<string, unknown>
  diagnostics: Diagnostic[]
  compact_for_agent: string
}

export function LogDiagnosisTab(props: { path: string }) {
  const { params } = useSessionLayout()

  const [data] = createResource(
    () => props.path,
    async (logPath) => {
      const res = await callAgenticTool(
        "workspace",
        { session_id: params.id!, workspace_root: decode64(params.dir) ?? "" },
        { action: "parse_log", path: logPath },
      )
      if (!res.success) return null
      try {
        return JSON.parse(res.result) as LogDiagnosis
      } catch {
        return null
      }
    },
  )

  const errorCount = () => data()?.summary?.error_count ?? 0
  const warnCount = () => data()?.summary?.warning_count ?? 0
  const cellCount = () => (data()?.metrics?.cell_count as number) ?? null
  const status = () => {
    if (errorCount() > 0) return "failed"
    if (warnCount() > 0) return "warnings"
    return "clean"
  }

  const handleSendToAgent = () => {
    const compact = data()?.compact_for_agent
    if (!compact) return
    navigator.clipboard.writeText(compact)
    showToast({ title: "Log summary copied", description: "Paste in chat to send to the agent." })
  }

  const statusColor = () => {
    const s = status()
    if (s === "failed") return "text-red-500"
    if (s === "warnings") return "text-yellow-500"
    return "text-green-500"
  }

  const statusIcon = () => {
    const s = status()
    if (s === "failed") return "warning"
    if (s === "warnings") return "warning"
    return "check"
  }

  return (
    <div class="h-full overflow-y-auto bg-background-stronger">
      <Show when={data.loading}>
        <div class="p-6 text-14-regular text-text-weak font-mono">Parsing EDA log...</div>
      </Show>

      <Show when={!data.loading && !data()}>
        <div class="p-6 text-14-regular text-text-weak">Failed to parse log file.</div>
      </Show>

      <Show when={data()}>
        {(d) => (
          <div class="max-w-3xl mx-auto p-6 space-y-4">
            {/* Header */}
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-3">
                <Icon name={statusIcon()} class={statusColor()} />
                <div>
                  <div class="text-14-medium text-text-strong font-mono">{d().tool} {d().stage}</div>
                  <div class="text-11-regular text-text-weaker">{d().path} · {d().line_count} lines</div>
                </div>
              </div>
              <button
                class="inline-flex items-center gap-1.5 px-3 py-1.5 text-12-medium bg-surface-base hover:bg-surface-stronger text-text-strong rounded-md border border-border-weaker-base transition-colors"
                onClick={handleSendToAgent}
              >
                <Icon name="share" size="small" class="text-icon-base shrink-0" />
                Send to agent
              </button>
            </div>

            {/* Metrics */}
            <Show when={cellCount() !== null || Object.keys(d().metrics).length > 0}>
              <div class="flex flex-wrap gap-3">
                <Show when={cellCount() !== null}>
                  <div class="px-3 py-2 rounded-md bg-surface-base border border-border-weaker-base">
                    <div class="text-10-regular text-text-weaker uppercase tracking-wider">Cells</div>
                    <div class="text-14-medium text-text-strong font-mono">{cellCount()?.toLocaleString()}</div>
                  </div>
                </Show>
                <Show when={(d().metrics as any).wire_count}>
                  <div class="px-3 py-2 rounded-md bg-surface-base border border-border-weaker-base">
                    <div class="text-10-regular text-text-weaker uppercase tracking-wider">Wires</div>
                    <div class="text-14-medium text-text-strong font-mono">{(d().metrics as any).wire_count?.toLocaleString()}</div>
                  </div>
                </Show>
                <Show when={(d().metrics as any).wns_ns !== undefined}>
                  <div class="px-3 py-2 rounded-md bg-surface-base border border-border-weaker-base">
                    <div class="text-10-regular text-text-weaker uppercase tracking-wider">WNS</div>
                    <div class="text-14-medium font-mono" classList={{
                      "text-red-500": (d().metrics as any).wns_ns < 0,
                      "text-green-500": (d().metrics as any).wns_ns >= 0,
                    }}>
                      {(d().metrics as any).wns_ns}ns
                    </div>
                  </div>
                </Show>
                <div class="px-3 py-2 rounded-md bg-surface-base border border-border-weaker-base">
                  <div class="text-10-regular text-text-weaker uppercase tracking-wider">Errors</div>
                  <div class="text-14-medium font-mono" classList={{
                    "text-red-500": errorCount() > 0,
                    "text-green-500": errorCount() === 0,
                  }}>{errorCount()}</div>
                </div>
                <div class="px-3 py-2 rounded-md bg-surface-base border border-border-weaker-base">
                  <div class="text-10-regular text-text-weaker uppercase tracking-wider">Warnings</div>
                  <div class="text-14-medium font-mono" classList={{
                    "text-yellow-500": warnCount() > 0,
                    "text-green-500": warnCount() === 0,
                  }}>{warnCount()}</div>
                </div>
              </div>
            </Show>

            {/* Diagnostics list */}
            <Show when={d().diagnostics.length > 0}>
              <div class="space-y-1.5">
                <div class="text-11-medium text-text-weaker uppercase tracking-wider pt-2">Diagnostics ({d().diagnostics.length})</div>
                <For each={d().diagnostics}>
                  {(diag) => (
                    <div
                      class="flex items-start gap-2.5 px-3 py-2 rounded-md border text-12-regular"
                      classList={{
                        "border-red-500/30 bg-red-500/5": diag.severity === "error",
                        "border-yellow-500/30 bg-yellow-500/5": diag.severity === "warning",
                        "border-border-weaker-base bg-surface-base": diag.severity === "info" || !diag.severity,
                      }}
                    >
                      <Icon
                        name={diag.severity === "error" ? "warning" : diag.severity === "warning" ? "warning" : "check-small"}
                        size="small"
                        class="shrink-0 mt-0.5"
                        classList={{
                          "text-red-500": diag.severity === "error",
                          "text-yellow-500": diag.severity === "warning",
                          "text-text-weaker": diag.severity !== "error" && diag.severity !== "warning",
                        }}
                      />
                      <div class="flex-1 min-w-0">
                        <div class="text-text-strong break-words font-mono">{diag.message}</div>
                        <Show when={diag.file}>
                          <div class="text-10-regular text-text-weaker mt-0.5">
                            {diag.file}{diag.line ? `:${diag.line}` : ""} · log:{diag.log_line}
                          </div>
                        </Show>
                      </div>
                    </div>
                  )}
                </For>
              </div>
            </Show>

            {/* Clean state */}
            <Show when={d().diagnostics.length === 0 && status() === "clean"}>
              <div class="flex items-center gap-2 px-3 py-4 rounded-md bg-green-500/5 border border-green-500/20">
                <Icon name="check" class="text-green-500" />
                <span class="text-12-medium text-text-strong">No issues found. Log is clean.</span>
              </div>
            </Show>
          </div>
        )}
      </Show>
    </div>
  )
}
