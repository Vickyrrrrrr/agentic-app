import { createSignal, createResource, For, Show } from "solid-js"
import { Icon } from "@opencode-ai/ui/icon"
import { getAgenticBase } from "@/utils/agentic"

interface ExecutionResult {
  step_id: string
  stdout: string
  stderr: string
  success: boolean
  execution_time_ms: number
  is_ipython: boolean
  active_variables: string[]
}

interface RlmState {
  is_ipython: boolean
  variables: Record<string, any>
  trajectory_count: number
  refinement: {
    learned_patterns: string[]
    last_refined_at: number
  }
}

export function RlmIpythonConsole() {
  const [code, setCode] = createSignal(
    `# Prime-Agent RLM IPython Harness\nimport os, json\nprint("Active Workspace:", workspace_root)\nprint("Active EDA Tools:", active_tools)`
  )
  const [lastResult, setLastResult] = createSignal<ExecutionResult | null>(null)
  const [isExecuting, setIsExecuting] = createSignal(false)
  const [statusMsg, setStatusMsg] = createSignal<string | null>(null)

  const [rlmState, { refetch: refetchState }] = createResource(async () => {
    try {
      const res = await fetch(`${getAgenticBase()}/opencode/rlm/state`)
      if (!res.ok) return null
      return (await res.json()) as RlmState
    } catch {
      return null
    }
  })

  const handleRunCode = async () => {
    if (!code().trim()) return
    setIsExecuting(true)
    setStatusMsg(null)
    try {
      const res = await fetch(`${getAgenticBase()}/opencode/rlm/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code() }),
      })
      const data = await res.json()
      setLastResult(data)
      refetchState()
    } catch (e: any) {
      setStatusMsg(`Execution error: ${e.message}`)
    } finally {
      setIsExecuting(false)
    }
  }

  const handleRefine = async () => {
    try {
      const res = await fetch(`${getAgenticBase()}/opencode/rlm/refine`, {
        method: "POST",
      })
      if (res.ok) {
        setStatusMsg("Refinement loop completed! Harness prompt & patterns optimized.")
        refetchState()
      }
    } catch (e: any) {
      setStatusMsg(`Refine error: ${e.message}`)
    }
  }

  return (
    <div class="flex flex-col h-full bg-neutral-900 text-neutral-100 rounded-lg border border-neutral-800 p-4 font-mono select-none overflow-hidden">
      {/* Header */}
      <div class="flex items-center justify-between border-b border-neutral-800 pb-3 mb-3">
        <div class="flex items-center gap-2">
          <Icon name="terminal" class="w-5 h-5 text-emerald-400" />
          <span class="font-bold text-sm tracking-wide text-neutral-200 uppercase font-sans">
            Prime-Agent RLM IPython Harness
          </span>
          <Show when={rlmState()?.is_ipython}>
            <span class="px-2 py-0.5 text-[10px] rounded bg-emerald-950 text-emerald-300 border border-emerald-800">
              IPython Kernel Active
            </span>
          </Show>
        </div>
        <div class="flex gap-2">
          <button
            onClick={handleRefine}
            class="px-2.5 py-1 text-xs font-semibold rounded bg-purple-700 hover:bg-purple-600 text-white transition flex items-center gap-1 font-sans"
          >
            <Icon name="brain" class="w-3.5 h-3.5" />
            /refine Harness
          </button>
          <button
            onClick={handleRunCode}
            disabled={isExecuting()}
            class="px-3 py-1 text-xs font-semibold rounded bg-emerald-600 hover:bg-emerald-500 text-white transition flex items-center gap-1 font-sans"
          >
            <Icon name="code" class="w-3.5 h-3.5" />
            {isExecuting() ? "Running..." : "Run Cell"}
          </button>
        </div>
      </div>

      <Show when={statusMsg()}>
        <div class="mb-3 p-2 bg-neutral-800 text-purple-300 text-xs rounded border border-purple-500/30 flex items-center justify-between font-sans">
          <span>{statusMsg()}</span>
          <button onClick={() => setStatusMsg(null)} class="text-neutral-400 hover:text-white">✕</button>
        </div>
      </Show>

      {/* Editor & Output Split view */}
      <div class="grid grid-cols-2 gap-3 flex-1 overflow-hidden">
        {/* Left: Interactive Cell Editor */}
        <div class="flex flex-col h-full bg-neutral-950 border border-neutral-800 rounded p-2 overflow-hidden">
          <span class="text-[11px] font-bold text-neutral-400 uppercase tracking-wider mb-1 font-sans">Python / REPL Code Cell</span>
          <textarea
            value={code()}
            onInput={(e) => setCode(e.currentTarget.value)}
            class="flex-1 bg-transparent text-emerald-300 font-mono text-xs p-2 focus:outline-none resize-none overflow-auto"
            spellcheck={false}
          />
        </div>

        {/* Right: Output & Variables State */}
        <div class="flex flex-col h-full bg-neutral-950 border border-neutral-800 rounded p-2 overflow-hidden">
          <span class="text-[11px] font-bold text-neutral-400 uppercase tracking-wider mb-1 font-sans">Kernel stdout / Execution Output</span>
          
          <div class="flex-1 overflow-y-auto bg-black p-2 rounded text-xs border border-neutral-900">
            <Show when={lastResult()} fallback={
              <div class="text-neutral-600 italic text-[11px] font-sans">No output yet. Write Python code and click "Run Cell".</div>
            }>
              {(res) => (
                <div class="space-y-2">
                  <div class="flex items-center justify-between text-[10px] text-neutral-500 border-b border-neutral-800 pb-1 font-sans">
                    <span>Status: {res().success ? "SUCCESS" : "ERROR"}</span>
                    <span>Exec time: {res().execution_time_ms.toFixed(1)} ms</span>
                  </div>
                  <Show when={res().stdout}>
                    <pre class="text-neutral-200 whitespace-pre-wrap">{res().stdout}</pre>
                  </Show>
                  <Show when={res().stderr}>
                    <pre class="text-rose-400 whitespace-pre-wrap">{res().stderr}</pre>
                  </Show>
                </div>
              )}
            </Show>
          </div>

          {/* Active Variable State */}
          <div class="mt-2 pt-2 border-t border-neutral-800">
            <span class="text-[10px] font-bold text-neutral-400 uppercase tracking-wider block mb-1 font-sans">Persistent Scope Variables</span>
            <div class="flex flex-wrap gap-1">
              <For each={Object.keys(rlmState()?.variables || {})}>
                {(key) => (
                  <span class="px-1.5 py-0.5 text-[10px] rounded bg-neutral-800 text-neutral-300 font-mono">
                    {key}: {JSON.stringify(rlmState()?.variables[key]).slice(0, 20)}
                  </span>
                )}
              </For>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
