import { Show, createEffect, createSignal, onCleanup, onMount } from "solid-js"
import type * as Monaco from "monaco-editor"
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker"
import { callAgenticTool } from "@/utils/agentic"
import { decode64 } from "@/utils/base64"
import { useFile } from "@/context/file"
import { usePrompt } from "@/context/prompt"
import { useSessionLayout } from "@/pages/session/session-layout"
import { showToast } from "@/utils/toast"

type Selection = {
  startLine: number
  startCharacter: number
  endLine: number
  endCharacter: number
}

type RtlDiagnostic = {
  severity?: string
  category?: string
  line?: number | null
  message?: string
  hint?: string
}

let languageRegistered = false

function registerSystemVerilog(monaco: typeof Monaco) {
  if (languageRegistered) return
  languageRegistered = true

  monaco.languages.register({ id: "systemverilog", extensions: [".v", ".sv", ".vh", ".svh"] })
  monaco.languages.setLanguageConfiguration("systemverilog", {
    comments: { lineComment: "//", blockComment: ["/*", "*/"] },
    brackets: [["{", "}"], ["[", "]"], ["(", ")"]],
    autoClosingPairs: [
      { open: "{", close: "}" },
      { open: "[", close: "]" },
      { open: "(", close: ")" },
      { open: '"', close: '"' },
    ],
  })
  monaco.languages.setMonarchTokensProvider("systemverilog", {
    keywords: [
      "always", "always_comb", "always_ff", "always_latch", "assign", "automatic", "begin", "case", "casex", "casez",
      "default", "else", "end", "endcase", "endfunction", "endgenerate", "endmodule", "endpackage", "endtask", "for",
      "foreach", "function", "generate", "genvar", "if", "import", "inout", "input", "interface", "logic", "module",
      "output", "package", "parameter", "localparam", "reg", "return", "signed", "struct", "task", "typedef", "union",
      "unsigned", "wire", "while",
    ],
    systemTasks: ["display", "error", "fatal", "finish", "fwrite", "monitor", "random", "readmemh", "signed", "time", "unsigned"],
    tokenizer: {
      root: [
        [/`[A-Za-z_][\w$]*/, "annotation"],
        [/\$[A-Za-z_][\w$]*/, { cases: { "@systemTasks": "predefined", "@default": "predefined" } }],
        [/[A-Za-z_][\w$]*/, { cases: { "@keywords": "keyword", "@default": "identifier" } }],
        [/\d+'[bBoOdDhH][0-9a-fA-F_xXzZ?]+/, "number.hex"],
        [/\d+(?:\.\d+)?/, "number"],
        [/\/\/.*$/, "comment"],
        [/\/\*/, "comment", "@comment"],
        [/"/, "string", "@string"],
        [/[{}()\[\]]/, "@brackets"],
        [/[;,.]/, "delimiter"],
        [/[+\-*\/%<>=!&|^~?:]+/, "operator"],
      ],
      comment: [
        [/[^*/]+/, "comment"],
        [/\*\//, "comment", "@pop"],
        [/[*/]/, "comment"],
      ],
      string: [
        [/[^\\"]+/, "string"],
        [/\\./, "string.escape"],
        [/"/, "string", "@pop"],
      ],
    },
  })
}

export function HdlEditorTab(props: { path: string; source: string }) {
  const { params } = useSessionLayout()
  const file = useFile()
  const prompt = usePrompt()
  const [dirty, setDirty] = createSignal(false)
  const [saving, setSaving] = createSignal(false)
  const [evidence, setEvidence] = createSignal("No local HDL evidence yet")
  const [selection, setSelection] = createSignal<Selection | undefined>()
  let host!: HTMLDivElement
  let editor: Monaco.editor.IStandaloneCodeEditor | undefined
  let model: Monaco.editor.ITextModel | undefined
  let monaco: typeof Monaco | undefined
  let suppressDirty = false

  const agentContext = (intent: string) => {
    const selected = selection()
    prompt.context.add({
      type: "file",
      path: props.path,
      selection: selected
        ? {
            startLine: selected.startLine,
            startChar: selected.startCharacter,
            endLine: selected.endLine,
            endChar: selected.endCharacter,
          }
        : undefined,
    })
    prompt.set([{ type: "text", content: intent, start: 0, end: intent.length }])
    showToast({ title: "RTL selection added to AgentIC context" })
  }

  const applyDiagnostics = (diagnostics: RtlDiagnostic[]) => {
    if (!monaco || !model) return
    const editorApi = monaco
    const activeModel = model
    editorApi.editor.setModelMarkers(
      activeModel,
      "agentic-rtl",
      diagnostics.flatMap((diagnostic) => {
        if (!diagnostic.line || !diagnostic.message) return []
        const severity = diagnostic.severity?.toLowerCase() === "warning"
          ? editorApi.MarkerSeverity.Warning
          : editorApi.MarkerSeverity.Error
        const line = Math.min(Math.max(1, diagnostic.line), activeModel.getLineCount())
        return [{
          severity,
          message: diagnostic.hint ? `${diagnostic.message}\n\nAgentIC: ${diagnostic.hint}` : diagnostic.message,
          code: diagnostic.category,
          startLineNumber: line,
          startColumn: 1,
          endLineNumber: line,
          endColumn: activeModel.getLineMaxColumn(line),
        }]
      }),
    )
  }

  const validate = async () => {
    if (dirty()) {
      setEvidence("Save this revision before verification")
      showToast({ title: "Verification uses the governed saved revision" })
      return
    }
    try {
      const response = await callAgenticTool(
        "workspace",
        { session_id: params.id ?? "", workspace_root: decode64(params.dir) ?? "" },
        { action: "rtl_repair_diagnose", path: props.path },
      )
      if (!response.success) throw new Error(response.result || "RTL evidence check failed")
      const parsed = JSON.parse(response.result) as {
        status?: string
        diagnostic_count?: number
        linter_engine?: string
        diagnostics?: RtlDiagnostic[]
      }
      const diagnostics = parsed.diagnostics ?? []
      applyDiagnostics(diagnostics)
      if (diagnostics.length > 0) {
        setEvidence(`${diagnostics.length} RTL ${diagnostics.length === 1 ? "issue" : "issues"} • ${parsed.linter_engine ?? "AgentIC"}`)
      } else {
        setEvidence(`RTL evidence clean • ${parsed.linter_engine ?? "AgentIC"}`)
      }
    } catch (error) {
      applyDiagnostics([])
      setEvidence(error instanceof Error ? error.message : "RTL evidence check is unavailable")
    }
  }

  const trace = async () => {
    try {
      const session = { session_id: params.id ?? "", workspace_root: decode64(params.dir) ?? "" }
      let response = await callAgenticTool("design_contract", session, { action: "get" })
      if (!response.success) throw new Error(response.result || "Design evidence is unavailable")
      let contract = JSON.parse(response.result) as {
        top_module?: string
        module_count?: number
        status?: { contract?: string }
      }
      if (contract.status?.contract === "missing") {
        response = await callAgenticTool("design_contract", session, { action: "infer" })
        if (!response.success) throw new Error(response.result || "Could not infer design evidence")
        contract = JSON.parse(response.result) as typeof contract
      }
      const identity = contract.top_module || "unresolved top"
      const count = contract.module_count ?? 0
      setEvidence(`Trace lens • ${identity} • ${count} modules`)
    } catch (error) {
      setEvidence(error instanceof Error ? error.message : "Trace evidence is unavailable")
    }
    agentContext("Trace the selected SystemVerilog object through hierarchy, drivers, timing evidence, and related waveforms. Use AgentIC structured tools before proposing a change.")
  }

  const save = async () => {
    if (!editor || !dirty() || saving()) return
    setSaving(true)
    try {
      const response = await callAgenticTool(
        "write",
        { session_id: params.id ?? "", workspace_root: decode64(params.dir) ?? "" },
        { path: props.path, content: editor.getValue() },
      )
      if (!response.success) throw new Error(response.result || "AgentIC rejected the RTL update")
      setDirty(false)
      setEvidence("Saved through AgentIC policy; refreshing RTL evidence…")
      await file.load(props.path, { force: true })
      await validate()
      showToast({ title: "RTL saved and checked" })
    } catch (error) {
      showToast({ variant: "error", title: "RTL was not saved", description: error instanceof Error ? error.message : String(error) })
    } finally {
      setSaving(false)
    }
  }

  onMount(async () => {
    monaco = await import("monaco-editor")
    ;(self as typeof globalThis & { MonacoEnvironment?: { getWorker: () => Worker } }).MonacoEnvironment = {
      getWorker: () => new EditorWorker(),
    }
    registerSystemVerilog(monaco)
    model = monaco.editor.createModel(props.source, "systemverilog", monaco.Uri.file(props.path))
    editor = monaco.editor.create(host, {
      model,
      automaticLayout: true,
      minimap: { enabled: false },
      lineNumbers: "on",
      folding: true,
      glyphMargin: true,
      renderLineHighlight: "all",
      scrollBeyondLastLine: false,
      smoothScrolling: true,
      fontLigatures: true,
      fontSize: 13,
      lineHeight: 21,
      padding: { top: 12, bottom: 20 },
      guides: { indentation: true, bracketPairs: true },
      bracketPairColorization: { enabled: true },
      wordWrap: "off",
      stickyScroll: { enabled: true },
      accessibilitySupport: "auto",
      theme: "vs-dark",
    })
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => void save())
    editor.onDidChangeModelContent(() => {
      if (!suppressDirty) setDirty(true)
    })
    editor.onDidChangeCursorSelection((event) => {
      const range = event.selection
      setSelection({
        startLine: range.startLineNumber,
        startCharacter: range.startColumn - 1,
        endLine: range.endLineNumber,
        endCharacter: range.endColumn - 1,
      })
    })
  })

  createEffect(() => {
    const next = props.source
    if (!model || model.getValue() === next || dirty()) return
    suppressDirty = true
    model.setValue(next)
    suppressDirty = false
  })

  onCleanup(() => {
    editor?.dispose()
    model?.dispose()
  })

  return (
    <div class="flex h-full min-h-0 flex-col bg-background-stronger">
      <div class="flex min-h-9 items-center gap-2 border-b border-border-weaker-base px-3 text-11-regular text-text-weaker">
        <span class="font-mono text-text-strong">SystemVerilog</span>
        <span>•</span>
        <span>{dirty() ? "Modified" : "Saved"}</span>
        <span class="ml-auto max-w-96 truncate" title={evidence()}>{evidence()}</span>
        <button class="rounded px-2 py-1 text-text-base hover:bg-surface-base disabled:opacity-50" disabled={saving() || !dirty()} onClick={() => void save()}>
          {saving() ? "Saving…" : "Save"}
        </button>
        <button class="rounded px-2 py-1 text-text-base hover:bg-surface-base" onClick={() => void validate()}>
          Verify
        </button>
        <button class="rounded px-2 py-1 text-text-base hover:bg-surface-base" onClick={() => void trace()}>
          Trace
        </button>
        <button class="rounded px-2 py-1 text-text-base hover:bg-surface-base" onClick={() => agentContext("Review the selected SystemVerilog code as a hardware change. Explain its functional, reset/CDC, latency, and verification impact using AgentIC evidence.")}>
          Ask AgentIC
        </button>
      </div>
      <div ref={host} class="min-h-0 flex-1" />
      <Show when={dirty()}>
        <div class="border-t border-border-weaker-base px-3 py-1 text-11-regular text-text-weaker">Edits are saved through AgentIC policy and RTL quality gates.</div>
      </Show>
    </div>
  )
}
