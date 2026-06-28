import { createResource, createSignal, For, Show } from "solid-js"
import { Icon } from "@opencode-ai/ui/icon"
import { useSessionLayout } from "@/pages/session/session-layout"
import { callAgenticTool, getAgenticBase } from "@/utils/agentic"
import { decode64 } from "@/utils/base64"

type PDKLibrary = {
  name: string
  version?: string
}

export function PDKCatalogDock() {
  const { params } = useSessionLayout()
  const [activeTab, setActiveTab] = createSignal<"stdcell" | "macros">("stdcell")
  const [selectedLib, setSelectedLib] = createSignal<string>("")
  const [fetchError, setFetchError] = createSignal<string | null>(null)

  const [libraries] = createResource(
    () => params.id,
    async (sessionId) => {
      try {
        const res = await callAgenticTool(
          "query_pdk",
          { session_id: sessionId, workspace_root: decode64(params.dir) ?? "" },
          { query_type: "list_libraries" }
        )
        if (!res.success || !res.result) throw new Error(res.result || "No PDK data returned")
        const parsed = JSON.parse(res.result)
        const rawLibs = Array.isArray(parsed.libraries) ? parsed.libraries : Array.isArray(parsed) ? parsed : []
        const libs: PDKLibrary[] = rawLibs.map((item: any) => {
          if (typeof item === "string") return { name: item }
          return { name: item?.name || String(item) }
        })
        if (libs.length > 0 && !selectedLib()) setSelectedLib(libs[0].name)
        return libs
      } catch (e) {
        setFetchError(e instanceof Error ? e.message : String(e))
        return []
      }
    },
  )

  const [cells] = createResource(
    () => selectedLib(),
    async (lib) => {
      if (!lib) return []
      try {
        const res = await callAgenticTool(
          "query_pdk",
          { session_id: params.id!, workspace_root: decode64(params.dir) ?? "" },
          { query_type: "find_cell", cell_type: lib }
        )
        if (!res.success || !res.result) return []
        const parsed = JSON.parse(res.result)
        const rawCells = Array.isArray(parsed.results) ? parsed.results : Array.isArray(parsed.cells) ? parsed.cells : Array.isArray(parsed) ? parsed : []
        return rawCells
          .filter((item: any) => !item || typeof item === "string" || !item.library || item.library === lib)
          .map((item: any) => typeof item === "string" ? item : item?.cell || String(item))
      } catch {
        return []
      }
    },
  )

  const [macros] = createResource(
    () => selectedLib(),
    async (lib) => {
      if (!lib) return []
      try {
        const res = await callAgenticTool(
          "query_pdk",
          { session_id: params.id!, workspace_root: decode64(params.dir) ?? "" },
          { query_type: "find_memory" }
        )
        if (!res.success || !res.result) return []
        const parsed = JSON.parse(res.result)
        
        const binding = parsed.binding || {}
        const list: any[] = []
        if (binding.selected && binding.selected.macro) {
          list.push(binding.selected.macro)
        }
        if (Array.isArray(binding.alternatives)) {
          for (const alt of binding.alternatives) {
            if (alt && alt.macro) {
              list.push(alt.macro)
            }
          }
        }
        
        const seen = new Set<string>()
        const uniqueList = list.filter((item) => {
          if (!item || !item.name) return false
          if (seen.has(item.name)) return false
          seen.add(item.name)
          return true
        })

        if (uniqueList.length > 0) return uniqueList

        const rawMacros = Array.isArray(parsed.macros) ? parsed.macros : Array.isArray(parsed.results) ? parsed.results : Array.isArray(parsed) ? parsed : []
        return rawMacros
      } catch {
        return []
      }
    },
  )

  const libs = () => libraries() ?? []
  const isUnavailable = () => !libraries.loading && libs().length === 0

  const btn = (tab: "stdcell" | "macros", label: string) => (
    <button
      onClick={() => setActiveTab(tab)}
      class="flex-1 px-2 py-1.5 text-12-medium"
      classList={{
        "text-text-strong bg-surface-base": activeTab() === tab,
        "text-text-weak hover:text-text-base": activeTab() !== tab,
      }}
    >
      {label}
    </button>
  )

  return (
    <div class="flex flex-col h-full bg-background-stronger font-sans">
      <div class="flex border-b border-border-weaker-base">
        {btn("stdcell", "Std Cells")}
        {btn("macros", "Memory Macros")}
      </div>

      <Show when={libraries.loading && libs().length === 0}>
        <div class="p-4 text-12-regular text-text-weak">Querying PDK...</div>
      </Show>

      <Show when={fetchError()}>
        <div class="px-3 py-1.5 text-11-regular text-text-weaker border-b border-border-weaker-base">
          {fetchError()}
        </div>
      </Show>

      <Show when={!libraries.loading && !isUnavailable()}>
        <div class="flex items-center gap-1.5 px-2 py-1.5 bg-surface-base border-b border-border-weaker-base">
          <Icon name="folder" size="small" class="text-icon-base shrink-0" />
          <select
            value={selectedLib()}
            onChange={(e) => setSelectedLib(e.currentTarget.value)}
            class="flex-1 bg-background-base text-12-regular text-text-strong border border-border-weak-base rounded-sm px-1.5 py-0.5 font-mono"
          >
            <For each={libs()}>
              {(lib) => <option value={lib.name}>{lib.name}</option>}
            </For>
          </select>
        </div>
      </Show>

      <div class="flex-1 overflow-y-auto contain-strict">
        <Show when={isUnavailable() && !libraries.loading && !fetchError()}>
          <div class="p-4 text-12-regular text-text-weaker text-center">
            <div>No PDK data available</div>
            <div class="text-11-regular mt-1">Run AgentIC backend at {getAgenticBase()}</div>
          </div>
        </Show>

        <Show when={activeTab() === "stdcell" && !isUnavailable()}>
          <div class="px-3 py-1.5 text-11-regular text-text-weaker border-b border-border-weaker-base">
            {selectedLib() || "PDK"} &mdash; {cells.loading ? "loading..." : `${cells().length} cells`}
          </div>
          <For each={cells()}>
            {(cell: string) => (
              <div
                class="flex items-center gap-1.5 px-3 py-1 text-12-regular text-text-strong hover:bg-surface-base cursor-pointer border-b border-border-weaker-base last:border-b-0 font-mono"
                onClick={() => navigator.clipboard.writeText(cell)}
              >
                <Icon name="copy" size="small" class="text-icon-weak shrink-0" />
                {cell}
              </div>
            )}
          </For>
        </Show>

        <Show when={activeTab() === "macros" && !isUnavailable()}>
          <div class="px-3 py-1.5 text-11-regular text-text-weaker border-b border-border-weaker-base">
            {selectedLib() || "PDK"} &mdash; {macros.loading ? "loading..." : `${macros().length} macros`}
          </div>
          <For each={macros()}>
            {(macro: any) => (
              <div class="flex items-center gap-1.5 px-3 py-1 text-12-regular text-text-strong border-b border-border-weaker-base last:border-b-0">
                <Icon name="archive" size="small" class="text-icon-weak shrink-0" />
                <span>{macro.name || macro}</span>
                <Show when={macro.words && macro.bits}>
                  <span class="text-text-weaker ml-auto text-11-regular">{macro.words}w x {macro.bits}b</span>
                </Show>
              </div>
            )}
          </For>
        </Show>
      </div>
    </div>
  )
}
