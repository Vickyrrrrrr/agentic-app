import { createResource, createSignal, createEffect, For, Show, Switch, Match } from "solid-js"
import { Icon } from "@opencode-ai/ui/icon"
import { getFilename } from "@opencode-ai/core/util/path"
import { useSessionLayout } from "@/pages/session/session-layout"
import { callAgenticResolve, callAgenticTool } from "@/utils/agentic"
import { decode64 } from "@/utils/base64"
import { schematicTab } from "./schematic-viewer"

export function SchematicExplorer() {
  const { params, tabs } = useSessionLayout()
  const [selectedFile, setSelectedFile] = createSignal<string | null>(null)
  const [fetchError, setFetchError] = createSignal<string | null>(null)
  const [contextError, setContextError] = createSignal<string | null>(null)

  const [context] = createResource(
    () => params.id,
    async (sessionId) => {
      setContextError(null)
      try {
        const ctx = await callAgenticResolve({ session_id: sessionId })
        return ctx as Record<string, unknown>
      } catch (e) {
        setContextError(e instanceof Error ? e.message : String(e))
        return null
      }
    },
  )

  const [rtlFiles] = createResource(
    () => params.id,
    async (sessionId) => {
      setFetchError(null)
      try {
        const res = await callAgenticTool(
          "workspace",
          { session_id: sessionId, workspace_root: decode64(params.dir) ?? "" },
          { action: "list", pattern: "**/*" }
        )
        const files: string[] = res.result
          ? res.result.split("\n").map((f) => f.trim()).filter(Boolean)
          : []
        return files.filter((f) => f.endsWith(".v") || f.endsWith(".sv") || f.endsWith(".vhdl"))
      } catch (e) {
        setFetchError(e instanceof Error ? e.message : String(e))
        return []
      }
    },
  )

  const [parsedMods] = createResource(
    () => selectedFile(),
    async (path) => {
      if (!path) return null
      try {
        const res = await callAgenticTool(
          "workspace",
          { session_id: params.id!, workspace_root: decode64(params.dir) ?? "" },
          { action: "read", path }
        )
        if (!res.success) return null
        const mods: { name: string }[] = []
        const re = /module\s+(\w+)\s*[#(]/g
        let match: RegExpExecArray | null
        while ((match = re.exec(res.result)) !== null) {
          mods.push({ name: match[1] })
        }
        return mods.length > 0 ? mods : null
      } catch {
        return null
      }
    },
  )

  const [lintDiagnostics] = createResource(
    () => selectedFile(),
    async (path) => {
      if (!path) return null
      try {
        const res = await callAgenticTool(
          "workspace",
          { session_id: params.id!, workspace_root: decode64(params.dir) ?? "" },
          { action: "lint", path }
        )
        if (res.success && res.result) {
          const parsed = JSON.parse(res.result)
          return Array.isArray(parsed.errors) ? parsed.errors : []
        }
      } catch {
        return null
      }
      return null
    },
  )

  const [subTab, setSubTab] = createSignal<"diagnostics" | "schematic" | "registers">("diagnostics")

  const [moduleParse] = createResource(
    () => selectedFile(),
    async (path) => {
      if (!path) return null
      try {
        const res = await callAgenticTool(
          "workspace",
          { session_id: params.id!, workspace_root: decode64(params.dir) ?? "" },
          { action: "parse_module", path }
        )
        if (!res.success || !res.result) return null
        return JSON.parse(res.result)
      } catch {
        return null
      }
    }
  )

  const ctx = () => context() as Record<string, any> | undefined
  const rtl = () => rtlFiles() ?? []
  const mods = () => parsedMods() ?? []

  const renderRegisters = () => {
    const parse = moduleParse()
    if (!parse) return <div class="p-3 text-12-regular text-text-weak">No register data.</div>
    const regList = parse.registers || []

    return (
      <div class="flex-1 flex flex-col min-h-0 bg-background-stronger">
        <Show
          when={regList.length > 0}
          fallback={
            <div class="px-3 py-4 text-12-regular text-text-weaker">
              No bus registers detected in this module.
            </div>
          }
        >
          <table class="w-full table-fixed border-collapse text-11-regular">
            <thead class="sticky top-0 bg-background-stronger">
              <tr class="border-b border-border-weaker-base text-left text-text-weaker">
                <th class="w-18 px-3 py-1 font-normal">Offset</th>
                <th class="px-3 py-1 font-normal">Register</th>
                <th class="w-24 px-3 py-1 font-normal">Access</th>
                <th class="w-20 px-3 py-1 font-normal">Width</th>
              </tr>
            </thead>
            <tbody>
              <For each={regList}>
                {(reg: any) => (
                  <tr class="border-b border-border-weaker-base align-top hover:bg-surface-base">
                    <td class="px-3 py-1 font-mono text-text-weaker">{reg.address}</td>
                    <td class="truncate px-3 py-1 font-mono text-text-strong font-semibold" title={reg.name}>
                      {reg.name}
                    </td>
                    <td class="px-3 py-1 text-text-strong">
                      <span class="px-1.5 py-0.5 border border-border-weaker-base rounded text-10-uppercase font-mono">
                        {reg.access}
                      </span>
                    </td>
                    <td class="px-3 py-1 font-mono text-text-weaker">{reg.width}</td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </Show>
      </div>
    )
  }

  const [lintStatuses, setLintStatuses] = createSignal<Record<string, { hasErrors: boolean; count: number }>>({})

  // Background syntax scanner for file tree badges
  createEffect(() => {
    const list = rtl()
    if (list.length === 0) return
    list.forEach(async (file) => {
      try {
        const res = await callAgenticTool(
          "workspace",
          { session_id: params.id!, workspace_root: decode64(params.dir) ?? "" },
          { action: "lint", path: file }
        )
        if (res.success && res.result) {
          const parsed = JSON.parse(res.result)
          const errors = Array.isArray(parsed.errors) ? parsed.errors : []
          if (errors.length > 0) {
            setLintStatuses((prev) => ({
              ...prev,
              [file]: { hasErrors: true, count: errors.length }
            }))
          }
        }
      } catch {
        // Ignore background fetch errors gracefully
      }
    })
  })

  return (
    <div class="flex flex-col w-full h-full bg-background-stronger font-sans">
      <div class="flex items-center gap-1.5 px-3 py-1.5 border-b border-border-weaker-base text-12-medium text-text-strong">
        <Icon name="file-tree" size="small" class="text-icon-base shrink-0" />
        Design Hierarchy
      </div>

      <Show when={context.loading && !context() && !contextError()}>
        <div class="p-4 text-12-regular text-text-weak">Resolving design context...</div>
      </Show>

      <Show when={contextError()}>
        <div class="px-3 py-1.5 text-11-regular text-text-weaker border-b border-border-weaker-base">{contextError()}</div>
      </Show>

      <Show when={ctx() && !context.loading}>
        <div class="px-3 py-1.5 border-b border-border-weaker-base text-11-regular space-y-1">
          <div class="text-text-weaker text-10-regular uppercase tracking-wider">Session</div>
          <div class="text-text-strong">{String(ctx()?.session?.design_name || params.id || "—")}</div>
          <Show when={ctx()?.workflow}>
            <div class="text-text-weaker text-10-regular uppercase tracking-wider mt-1.5">Workflow</div>
            <div class="text-text-strong">{String(ctx()?.workflow?.mode || "—")} &mdash; {String(ctx()?.workflow?.intent || "")}</div>
          </Show>
          <Show when={ctx()?.kernel_scope}>
            <div class="text-text-weaker text-10-regular uppercase tracking-wider mt-1.5">Scope</div>
            <div class="text-text-base">{String(ctx()?.kernel_scope)}</div>
          </Show>
          <Show when={ctx()?.design_intent}>
            <div class="text-text-weaker text-10-regular uppercase tracking-wider mt-1.5">Design Intent</div>
            <div class="text-text-base">{String(ctx()?.design_intent)}</div>
          </Show>
        </div>
      </Show>

      <div class="flex flex-col min-h-0 border-b border-border-weaker-base" classList={{ "flex-1": !selectedFile(), "h-48 shrink-0": !!selectedFile() }}>
        <div class="px-3 py-1.5 text-11-regular text-text-weaker uppercase tracking-wider border-b border-border-weaker-base flex items-center justify-between">
          <span>RTL Files</span>
          <span class="text-text-weaker">{rtl().length}</span>
        </div>
        <Show when={fetchError()}>
          <div class="px-3 py-1 text-11-regular text-text-weaker">{fetchError()}</div>
        </Show>
        <div class="overflow-y-auto contain-strict flex-1">
          <For each={rtl()}>
            {(file) => (
              <div
                onClick={() => setSelectedFile(file)}
                title={file}
                class="flex items-center justify-between gap-1.5 px-3 py-1.5 text-12-regular cursor-pointer border-b border-border-weaker-base last:border-b-0"
                classList={{
                  "text-text-strong bg-surface-base": selectedFile() === file,
                  "text-text-base hover:text-text-strong hover:bg-surface-base": selectedFile() !== file,
                }}
              >
                <div class="flex items-center gap-1.5 min-w-0">
                  <Icon name="code" size="small" class="text-icon-weak shrink-0" />
                  <span class="truncate font-mono">{getFilename(file)}</span>
                </div>
                <Show when={lintStatuses()[file]?.hasErrors}>
                  <span class="shrink-0 inline-flex items-center justify-center px-1.5 py-0.5 rounded-full text-10-medium bg-red-500/10 text-red-500 border border-red-500/20 font-mono">
                    {lintStatuses()[file].count}
                  </span>
                </Show>
              </div>
            )}
          </For>
        </div>
      </div>

      <Show when={selectedFile()}>
        <div class="flex flex-col flex-1 min-h-0 border-t border-border-weaker-base bg-background-strong">
          <div class="grid grid-cols-3 border-b border-border-weaker-base bg-background-normal shrink-0">
            <button
              class="px-3 py-1.5 text-11-medium text-center hover:bg-surface-base border-r border-border-weaker-base"
              classList={{ "bg-surface-base text-text-strong font-semibold": subTab() === "diagnostics", "text-text-weaker": subTab() !== "diagnostics" }}
              onClick={() => setSubTab("diagnostics")}
            >
              Diagnostics
            </button>
            <button
              class="px-3 py-1.5 text-11-medium text-center hover:bg-surface-base border-r border-border-weaker-base"
              classList={{ "bg-surface-base text-text-strong font-semibold": subTab() === "schematic", "text-text-weaker": subTab() !== "schematic" }}
              onClick={() => setSubTab("schematic")}
            >
              Schematic
            </button>
            <button
              class="px-3 py-1.5 text-11-medium text-center hover:bg-surface-base"
              classList={{ "bg-surface-base text-text-strong font-semibold": subTab() === "registers", "text-text-weaker": subTab() !== "registers" }}
              onClick={() => setSubTab("registers")}
            >
              Registers
            </button>
          </div>

          <Switch>
            <Match when={subTab() === "diagnostics"}>
              <div class="flex flex-col flex-1 min-h-0">
                <div class="px-3 py-1.5 text-11-regular text-text-weaker uppercase tracking-wider border-b border-border-weaker-base flex items-center justify-between">
                  <span>Modules</span>
                  <span class="text-text-weaker">{parsedMods.loading ? "…" : mods().length}</span>
                </div>
                <div class="h-28 overflow-y-auto border-b border-border-weaker-base">
                  <Show when={parsedMods.loading}>
                    <div class="p-3 text-12-regular text-text-weak font-mono">Parsing...</div>
                  </Show>
                  <For each={mods()}>
                    {(mod) => (
                      <div class="flex items-center gap-1.5 px-3 py-1 text-12-regular text-text-strong border-b border-border-weaker-base last:border-b-0">
                        <Icon name="branch" size="small" class="text-icon-weak shrink-0" />
                        <span class="font-mono">{mod.name}</span>
                      </div>
                    )}
                  </For>
                  <Show when={!parsedMods.loading && parsedMods() === null}>
                    <div class="p-3 text-12-regular text-text-weak">No module declarations found</div>
                  </Show>
                </div>

                <div class="flex flex-col flex-1 min-h-0">
                  <div class="px-3 py-1.5 text-11-regular text-text-weaker uppercase tracking-wider border-b border-border-weaker-base flex items-center justify-between bg-background-normal">
                    <span>Syntax Errors & Warnings</span>
                    <span class="text-text-weaker">
                      {lintDiagnostics.loading ? "…" : (lintDiagnostics()?.length ?? 0)}
                    </span>
                  </div>
                  <div class="flex-1 overflow-y-auto p-2 space-y-1.5 bg-background-stronger">
                    <Show when={lintDiagnostics.loading}>
                      <div class="text-12-regular text-text-weak font-mono p-1">Linting...</div>
                    </Show>
                    <Show when={!lintDiagnostics.loading && (!lintDiagnostics() || lintDiagnostics()?.length === 0)}>
                      <div class="text-12-regular text-text-weaker font-sans p-1">No syntax errors found.</div>
                    </Show>
                    <For each={lintDiagnostics()}>
                      {(err: any) => (
                        <div
                          class="p-2 border rounded text-11-regular font-mono flex flex-col gap-0.5"
                          style={{
                            "border-color": err.severity === "error" ? "rgba(239, 68, 68, 0.4)" : "var(--border-warning-base)",
                            "background-color": err.severity === "error" ? "rgba(239, 68, 68, 0.05)" : "rgba(251, 221, 70, 0.05)",
                            "color": err.severity === "error" ? "#ef4444" : "var(--syntax-warning)"
                          }}
                        >
                          <div class="font-semibold flex items-center gap-1">
                            <Icon
                              name="warning"
                              size="small"
                              class="shrink-0"
                            />
                            <span>Line {err.line}</span>
                          </div>
                          <div class="text-text-strong break-all">{err.message}</div>
                        </div>
                      )}
                    </For>
                  </div>
                </div>
              </div>
            </Match>
            <Match when={subTab() === "schematic"}>
              <Show
                when={selectedFile()}
                fallback={<div class="p-3 text-12-regular text-text-weak">Select an RTL file.</div>}
              >
                <div class="flex-1 flex flex-col items-center justify-center gap-3 p-6 text-center">
                  <Icon name="code-lines" class="text-text-weaker shrink-0" />
                  <div class="text-12-medium text-text-strong font-mono">
                    {selectedFile()!.split("/").pop()}
                  </div>
                  <div class="text-11-regular text-text-weak max-w-64">
                    Open an interactive Yosys-generated schematic in a full viewer with pan, zoom, and hover-to-inspect.
                  </div>
                  <button
                    class="inline-flex items-center gap-1.5 px-3 py-1.5 text-11-medium bg-surface-base hover:bg-surface-stronger text-text-strong rounded-md border border-border-weaker-base focus:outline-none transition-colors"
                    onClick={() => {
                      const tab = schematicTab(selectedFile()!)
                      tabs().open(tab)
                      tabs().setActive(tab)
                    }}
                  >
                    <Icon name="expand" size="small" class="text-icon-base shrink-0" />
                    <span>Open Schematic Viewer</span>
                  </button>
                </div>
              </Show>
            </Match>
            <Match when={subTab() === "registers"}>
              <Show when={moduleParse.loading}>
                <div class="p-3 text-12-regular text-text-weak font-mono">Parsing register map...</div>
              </Show>
              <Show when={!moduleParse.loading}>
                {renderRegisters()}
              </Show>
            </Match>
          </Switch>
        </div>
      </Show>
    </div>
  )
}
