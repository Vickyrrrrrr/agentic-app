import type { Hooks, PluginInput, ToolContext, ToolResult } from "@opencode-ai/plugin"
import { tool } from "@opencode-ai/plugin"
import { z } from "zod"

const AGENT_NAME = "agentic-vlsi"
const DEFAULT_AGENTIC_LOCAL_URL = "http://127.0.0.1:7860"
const DEFAULT_FAST_CONTEXT_TIMEOUT_MS = 900

type AgenticSession = {
  session_id: string
  message_id?: string
  agent: string
  agentic_mode?: string
  user_text?: string
  workspace_root: string
  pdk_profile?: string
  design_name?: string
}

type AgenticToolPayload = AgenticSession & {
  name: string
  args: Record<string, unknown>
}

function agenticBaseUrl() {
  return (process.env.AGENTIC_LOCAL_URL || DEFAULT_AGENTIC_LOCAL_URL).replace(/\/+$/, "")
}

function agenticHeaders() {
  const headers: Record<string, string> = {
    "content-type": "application/json",
  }
  const bridgeToken = process.env.AGENTIC_OPENCODE_BRIDGE_TOKEN
  if (bridgeToken) headers["x-agentic-bridge-token"] = bridgeToken
  const auth = process.env.AGENTIC_AUTHORIZATION
  if (auth) headers.authorization = auth
  return headers
}

async function callAgentic<T>(path: string, payload: Record<string, unknown>, options: { timeoutMs?: number } = {}): Promise<T> {
  const controller = options.timeoutMs ? new AbortController() : undefined
  const timeout = options.timeoutMs
    ? setTimeout(() => controller?.abort(), options.timeoutMs)
    : undefined
  let response: Response
  try {
    response = await fetch(`${agenticBaseUrl()}${path}`, {
      method: "POST",
      headers: agenticHeaders(),
      body: JSON.stringify(payload),
      signal: controller?.signal,
    })
  } finally {
    if (timeout) clearTimeout(timeout)
  }
  const text = await response.text()
  let body: any = text
  try {
    body = text ? JSON.parse(text) : {}
  } catch {
    body = { detail: text }
  }
  if (!response.ok) {
    const detail = body?.detail || body?.error || text || response.statusText
    throw new Error(`AgentIC bridge ${response.status} ${response.statusText}: ${detail}`)
  }
  return body as T
}

async function getAgentic<T>(path: string): Promise<T> {
  const response = await fetch(`${agenticBaseUrl()}${path}`, {
    method: "GET",
    headers: agenticHeaders(),
  })
  const text = await response.text()
  let body: any = text
  try {
    body = text ? JSON.parse(text) : {}
  } catch {
    body = { detail: text }
  }
  if (!response.ok) {
    const detail = body?.detail || body?.error || text || response.statusText
    throw new Error(`AgentIC bridge ${response.status} ${response.statusText}: ${detail}`)
  }
  return body as T
}

async function agenticModeForSession(sessionID?: string) {
  if (sessionID) {
    try {
      const response = await getAgentic<{ success?: boolean; agentic_mode?: string }>(
        `/opencode/session/mode/${encodeURIComponent(sessionID)}`,
      )
      if (response?.success && response.agentic_mode) return response.agentic_mode === "builder" ? "builder" : "advisor"
    } catch {
      // Fall through to the process default when the local bridge is not reachable yet.
    }
  }
  return process.env.AGENTIC_MODE === "builder" ? "builder" : "advisor"
}

async function sessionAgent(input: PluginInput, sessionID?: string) {
  if (!sessionID) return undefined
  try {
    const info = await input.client.session.get({ path: { id: sessionID } } as any)
    return (info as any)?.agent || (info as any)?.info?.agent
  } catch {
    return undefined
  }
}

async function sessionInfo(input: PluginInput, sessionID?: string) {
  if (!sessionID) return { agent: undefined as string | undefined, directory: input.worktree }
  try {
    const info = await input.client.session.get({ path: { id: sessionID } } as any)
    return {
      agent: (info as any)?.agent || (info as any)?.info?.agent,
      directory:
        (info as any)?.directory ||
        (info as any)?.info?.directory ||
        (info as any)?.data?.directory ||
        input.worktree,
    }
  } catch {
    return { agent: undefined as string | undefined, directory: input.worktree }
  }
}

async function latestUserText(input: PluginInput, sessionID?: string) {
  if (!sessionID) return ""
  try {
    const history = await input.client.session.messages({ path: { id: sessionID } } as any)
    const messages = Array.isArray(history) ? history : (history as any)?.data || []
    const userMessages = messages.filter((msg: any) => msg.role === "user" || msg.info?.role === "user")
    const last = userMessages[userMessages.length - 1]
    const parts = last?.parts || last?.info?.parts || []
    const textParts = Array.isArray(parts)
      ? parts
          .map((part: any) => part.text || part.content || part.data?.text || "")
          .filter(Boolean)
          .join("\n")
      : ""
    return last?.content || last?.info?.content || textParts || ""
  } catch {
    return ""
  }
}

async function sessionDirectory(input: PluginInput, sessionID?: string) {
  if (!sessionID) return input.worktree
  try {
    const info = await input.client.session.get({ path: { id: sessionID } } as any)
    return (info as any)?.directory || (info as any)?.info?.directory || (info as any)?.data?.directory || input.worktree
  } catch {
    return input.worktree
  }
}

function sessionPayload(input: PluginInput, context: ToolContext, extra: Partial<AgenticSession> = {}): AgenticSession {
  return {
    session_id: extra.session_id || context.sessionID,
    message_id: extra.message_id || context.messageID,
    agent: extra.agent || context.agent || AGENT_NAME,
    agentic_mode: extra.agentic_mode || (process.env.AGENTIC_MODE === "builder" ? "builder" : "advisor"),
    user_text: extra.user_text || "",
    workspace_root: extra.workspace_root || context.worktree || input.worktree,
    pdk_profile: extra.pdk_profile || process.env.AGENTIC_PDK_PROFILE || process.env.PDK || "",
    design_name: extra.design_name,
  }
}

async function runAgenticTool(input: PluginInput, context: ToolContext, payload: Omit<AgenticToolPayload, keyof AgenticSession>) {
  const worktree = await sessionDirectory(input, context.sessionID)
  const agenticMode = await agenticModeForSession(context.sessionID)
  const response = await callAgentic<{
    success: boolean
    result: string
    session?: { design_name?: string; design_root?: string; run_id?: string }
  }>("/opencode/tool", {
    ...sessionPayload(input, context, { workspace_root: worktree, agentic_mode: agenticMode }),
    ...payload,
  })
  const design = response.session?.design_name
  const run = response.session?.run_id
  const title = design ? `AgentIC ${payload.name}: ${design}` : `AgentIC ${payload.name}`
  const rawOutput = response.result || (response.success ? "AgentIC tool completed." : "AgentIC tool failed.")
  const output = formatAgenticToolOutput(payload.name, rawOutput)
  return {
    title,
    output,
    metadata: {
      agentic: true,
      success: response.success,
      design_name: design,
      run_id: run,
      tool: payload.name,
    },
  } satisfies ToolResult
}

const toolResult = (input: PluginInput, context: ToolContext, name: string, args: Record<string, unknown>) =>
  runAgenticTool(input, context, { name, args })

function formatAgenticToolOutput(name: string, rawOutput: string) {
  let parsed: any
  try {
    parsed = JSON.parse(rawOutput)
  } catch {
    return rawOutput
  }

  const hints: string[] = []
  if (name === "design_contract") {
    const status = parsed.status || {}
    hints.push(
      `Answer hint: design contract status=${status.contract || "unknown"}, top=${parsed.top_module || "unknown"}, file=${parsed.top_file || "unknown"}.`,
    )
    if (Array.isArray(parsed.validation_issues) && parsed.validation_issues.length > 0) {
      hints.push(`Main issue: ${parsed.validation_issues[0].code || "issue"} - ${parsed.validation_issues[0].message || "see JSON"}.`)
    }
  } else if (name === "eda_capability") {
    const ctx = parsed.agent_context || parsed
    hints.push(
      `Answer hint: capability tier=${ctx.capability_tier || "unknown"}, ready stages=${(ctx.ready_required_stages || []).join(",") || "none"}, missing stages=${(ctx.missing_required_stages || []).join(",") || "none"}.`,
    )
  } else if (name === "query_pdk") {
    hints.push(`Answer hint: PDK query returned ${Array.isArray(parsed.results) ? parsed.results.length : "structured"} result(s); use the JSON below as the local PDK evidence.`)
  } else if (name === "report") {
    const summary = parsed.summary || parsed.signoff || parsed
    hints.push(`Answer hint: this is the current checkpoint/signoff evidence. Do not run shell unless the user asked to execute the next missing stage.`)
    if (summary.status) hints.push(`Reported status: ${summary.status}.`)
  } else if (name === "timing_inspect" || name === "drc_inspect" || name === "layout_inspect") {
    hints.push("Answer hint: this inspector output is already structured evidence; answer from it unless a required report/file is missing.")
  }

  if (hints.length === 0) return rawOutput
  hints.push("Shell policy: do not call shell just to reinterpret this result; call shell only to create missing evidence or run a user-requested step.")
  return `${hints.join("\n")}\n\nRaw AgentIC JSON:\n${rawOutput}`
}

export async function AgenticVlsiPlugin(input: PluginInput): Promise<Hooks> {
  return {
    "experimental.chat.system.transform": async (ctxInput, ctxOutput) => {
      if (!ctxInput.sessionID) return
      const info = await sessionInfo(input, ctxInput.sessionID)
      const activeAgent = info.agent
      if (activeAgent && activeAgent !== AGENT_NAME) return
      const worktree = info.directory
      const agenticMode = await agenticModeForSession(ctxInput.sessionID)
      const response = await callAgentic<any>("/opencode/session/resolve", {
        session_id: ctxInput.sessionID,
        agent: activeAgent || AGENT_NAME,
        user_text: "",
        workspace_root: worktree,
        pdk_profile: process.env.AGENTIC_PDK_PROFILE || process.env.PDK || "",
        agentic_mode: agenticMode,
        fast: true,
      }, { timeoutMs: Number(process.env.AGENTIC_FAST_CONTEXT_TIMEOUT_MS || DEFAULT_FAST_CONTEXT_TIMEOUT_MS) })
        .catch((error) => ({ success: false, error: error instanceof Error ? error.message : String(error) }))

      if (!response?.success) {
        ctxOutput.system.push(
          [
            "## AgentIC VLSI bridge unavailable",
            "The AgentIC local backend did not provide VLSI context for this turn.",
            `Reason: ${response?.error || "unknown bridge error"}`,
            "Do not claim to have queried PDKs, tools, licenses, or AgentIC checkpoints until the bridge is restored.",
          ].join("\n"),
        )
        return
      }

      if (response.fast) {
        ctxOutput.system.push(
          [
            "## AgentIC Desktop Runtime Contract",
            `Session: ${response.session?.session_id}`,
            `Design: ${response.session?.design_name}`,
            `Design root: ${response.session?.design_root}`,
            `Linux design root: ${response.session?.linux_design_root || "n/a"}`,
            `AgentIC mode: ${agenticMode}`,
            "Fast context: true. Full VLSI context is intentionally deferred to reduce first-token latency.",
            "",
            "Use native read/grep/glob/write for normal file UX. Use AgentIC tools before making VLSI-specific claims.",
            "For chip work, use this evidence loop: agentic_design_contract(action='validate') -> agentic_eda_capability(scope='agent_context') -> agentic_query_pdk(query_type='readiness' or 'find_memory' when needed) -> agentic_run_flow -> inspect generated reports/logs -> answer from evidence.",
            "Use agentic_context for full context, agentic_design_state for durable chip facts, agentic_design_contract for top-module/clock/reset/hierarchy/SDC evidence, agentic_eda_capability for tool/PDK evidence, agentic_query_pdk for PDK/macro facts, agentic_run_flow for checkpointed EDA execution, agentic_timing_inspect for STA, agentic_drc_inspect for DRC/LVS logs, and agentic_layout_inspect for GDS.",
            agenticMode === "builder"
              ? "BUILDER mode is active: after any required approval, you may write files and run EDA tools."
              : "ADVISOR mode is active: inspect, explain, plan, and write docs/reports only.",
            "Do not invent PDK cells, SRAM macros, tool licenses, timing corners, or signoff readiness; retrieve evidence with AgentIC tools first.",
            "If evidence is missing, say exactly what is missing and the next AgentIC tool/command needed. Do not fill gaps from memory.",
          ].join("\n"),
        )
        return
      }

      ctxOutput.system.push(
        [
          "## AgentIC Desktop Runtime Contract",
          `Session: ${response.session?.session_id}`,
          `Design: ${response.session?.design_name}`,
          `Design root: ${response.session?.design_root}`,
          `Linux design root: ${response.session?.linux_design_root || "n/a"}`,
          `AgentIC mode: ${agenticMode}`,
          `Workflow: ${response.workflow?.mode} (${response.workflow?.intent})`,
          `Scope: ${response.kernel_scope}`,
          "",
          "Keep execution target selection calm and explicit: native, wsl, or docker. Default to native unless the user selected WSL/Docker or capability evidence shows the needed EDA tools are only available there.",
          "Use agentic_run_flow for EDA commands so AgentIC can execute on the selected target and capture checkpoints. Use the native bash tool for ordinary shell inspection when checkpointing is not needed.",
          "Use the native write tool for ALL file writes — it shows a diff view.",
          "Use the native read/grep/glob tools for file access — they have syntax highlighting.",
          "Before serious RTL/top/flow/signoff work, follow the AgentIC evidence loop: (1) validate the design contract, (2) inspect EDA/PDK capability, (3) query exact PDK readiness or macros when relevant, (4) run the smallest needed EDA step through agentic_run_flow, (5) inspect generated reports/logs, (6) record or cite the evidence.",
          "Use agentic_design_contract(action='infer'|'validate'|'get') to establish the active top module, clocks/resets, hierarchy, constraints, and evidence status before making top-level or flow claims. Prefer validate when RTL/SDC may have changed; prefer get only when you need the compact stored summary.",
          "After EDA commands, inspect generated logs/reports with agentic_drc_inspect or agentic_timing_inspect to get pass/fail, errors, warnings, and metrics. If the command produced a GDS, inspect it with agentic_layout_inspect before layout claims.",
          "After writing RTL files (.v/.sv), call agentic_workspace(action='rtl_repair_diagnose', path=<file>) to verify quality and get compact categorized repair guidance. Use action='lint' only when you need raw lint diagnostics.",
          "For GDS inspection, use agentic_layout_inspect. For STA analysis, use agentic_timing_inspect. For DRC/LVS log parsing, use agentic_drc_inspect.",
          "For PDK queries, use agentic_query_pdk. For design state, use agentic_ledger. For signoff reports, use agentic_report.",
          "When evidence is missing, stop at the missing gate and state the exact blocker plus the next command/tool to produce evidence. Do not turn assumptions into chip facts.",
          "",
          "## RTL Generation Methodology (Research-Backed)",
          "Derived from AutoChip, RTLFixer, MAGE, HDLFORGE, AutoVeriFix+, EvolVE, AoT, VerilogCoder, PDAGENT-BENCH, AgentDSE.",
          "",
          "1. EDA TOOL FEEDBACK LOOP (MANDATORY): NEVER generate RTL in a single shot. 55% of LLM-generated Verilog has syntax errors. Loop: generate → compile → read errors → fix → simulate → verify → repeat. EDA tool feedback improves success by 24% over zero-shot.",
          "2. REFERENCE MODEL FIRST: For non-trivial modules, write a Python reference model (tb/ref/<module>_ref.py) BEFORE writing Verilog. The Verilog must match the Python model cycle-for-cycle. Use it for causal debugging via cycle-accurate traces.",
          "3. STRUCTURED PLANNING: Do not jump to Verilog. Follow: (a) Classify pattern (FSM/pipeline/datapath/arbiter). (b) Write structured IR. (c) Write pseudocode. (d) Generate Verilog from pseudocode. Reduces tokens 1.8-5.2x, improves correctness.",
          "4. MULTIPLE CANDIDATES: For complex modules (FSMs, arbiters, pipelines), generate 2-3 candidates. Compile + simulate each. Cluster by output consistency. Select best. Not needed for trivial modules.",
          "5. ADAPTIVE FIX ESCALATION: When a module fails: (a) Minor fix (surgical edit). (b) Moderate fix (restructure block). (c) Full rewrite (if 2+ moderate fixes fail). Do not jump to full rewrite on first error.",
          "6. LLM ORCHESTRATES, EDA TOOLS EXECUTE: LLM proposes, EDA tools validate. Never claim a design works without EDA tool evidence. The simulator/synthesizer is the oracle.",
          "",
          "This context was returned by the AgentIC local bridge, so the local backend server is active.",
          "Do not confuse `flow_decision.backend: none` or `profile: setup_required` with the local backend being down; it means no usable EDA/PDK flow backend is selected yet.",
          "The context packet is a compact index and summary, not a full project dump. Use workspace(read/search/list), query_pdk, and bash/checkpoint log paths to retrieve exact details before making design, PDK, macro, timing, or signoff claims.",
          "On Windows, inspect `context_packet.environment_summary.wsl.tool_inventory`, `wsl_tools`, and `wsl_capabilities` before saying EDA tools are missing; this inventory includes open-source and proprietary commands detected across WSL distros.",
          "If the user names a WSL distro, use that distro. If several distros expose relevant tools and the user did not choose one, ask which distro to use.",
          "When builder mode is active and WSL or Docker is selected, pass target='wsl' or target='docker' to agentic_run_flow instead of manually wrapping commands.",
          "The `Design root` is the authoritative project directory for this session. Write RTL under `rtl/`, testbenches under `tb/` or `verification/`, scripts under `scripts/`, and reports under `reports/` relative to that root.",
          "Do not create another top-level project/session folder inside or beside the design root unless the user explicitly asks to create a separate design.",
          agenticMode === "builder"
            ? "You are in BUILDER mode. You are AUTHORIZED to write RTL/TB/scripts, run shell/EDA commands, and execute tools. Do NOT ask the user to switch to builder mode — you are already in it. Make a plan, wait for approval when starting implementation, then use agentic_run_flow for EDA execution and AgentIC inspect tools for VLSI verification."
            : "You are in ADVISOR mode. Do not write RTL/TB/scripts or run shell/EDA commands; only inspect/query and write docs/plans/diagrams/reports. If implementation is needed, tell the user to switch to builder mode.",
          "Do not invent PDK cells, SRAM macros, tool licenses, timing corners, or signoff readiness.",
          "If AgentIC reports a missing capability, stop the affected stage and explain the missing local evidence.",
          "",
          "## AgentIC kernel context contract",
          JSON.stringify(response.kernel_contract, null, 2),
          "",
          "## AgentIC compact context packet",
          JSON.stringify(response.context_packet, null, 2),
          "",
          "## AgentIC typed handoff schema catalog",
          JSON.stringify(response.schema_catalog, null, 2),
          "",
          "## AgentIC strict validation schema catalog",
          JSON.stringify(response.validation_schema_catalog, null, 2),
        ].join("\n"),
      )
    },
    tool: {
      agentic_context: tool({
        description: "Resolve the current AgentIC session into VLSI design context, workflow, PDK/tool capability state, and durable design mapping.",
        args: {
          user_text: z.string().default(""),
          pdk_profile: z.string().optional(),
          design_name: z.string().optional(),
        },
        async execute(args, context) {
          const agenticMode = await agenticModeForSession(context.sessionID)
          const response = await callAgentic<any>("/opencode/session/resolve", {
            ...sessionPayload(input, context, {
              agentic_mode: agenticMode,
              user_text: args.user_text,
              pdk_profile: args.pdk_profile,
              design_name: args.design_name,
            }),
          })
          return {
            title: `AgentIC context: ${response.session?.design_name || context.sessionID}`,
            output: JSON.stringify({
              bridge_status: "active",
              bridge_note:
                "AgentIC local bridge responded. `flow_decision.backend` describes the selected EDA flow backend, not whether this local server is running.",
              session: response.session,
              workflow: response.workflow,
              kernel_scope: response.kernel_scope,
              flow_decision: response.flow_decision,
              design_intent: response.design_intent,
              role_summary: response.role_summary,
            }, null, 2),
            metadata: { agentic: true, design_name: response.session?.design_name, run_id: response.session?.run_id },
          }
        },
      }),
      agentic_workspace: tool({
        description: "VLSI-specific workspace actions: diagnose RTL lint failures with compact categorized repair packets (action='rtl_repair_diagnose'), lint RTL (action='lint'), parse Verilog modules for ports/interfaces (action='parse_module'), generate Yosys schematics (action='schematic_json'). For reading/searching/listing files, use the native read/grep/glob tools instead. For EDA log parsing, use agentic_drc_inspect. For GDS inspection, use agentic_layout_inspect. For STA analysis, use agentic_timing_inspect.",
        args: {
          action: z.enum(["lint", "rtl_repair_diagnose", "parse_module", "schematic_json"]),
          path: z.string().default("."),
          pattern: z.string().default(""),
          module: z.string().default(""),
        },
        async execute(args, context) {
          return toolResult(input, context, "workspace", args)
        },
      }),
      agentic_design_state: tool({
        description: "Read AgentIC's durable VLSI design state: facts, handoffs, evidence graph, command/checkpoint history, and recent artifacts for the current chip session.",
        args: {
          max_events: z.number().default(20),
        },
        async execute(args, context) {
          return toolResult(input, context, "design_state", args)
        },
      }),
      agentic_design_contract: tool({
        description: "Infer, validate, or read the active AgentIC chip design contract: top module, top file, clocks/resets, hierarchy, SDC status, policy checks, evidence status, and next actions. Use validate before top-level edits, flow setup, signoff claims, or after RTL/SDC changes; use infer to create/refresh from scratch; use get only for the compact stored summary.",
        args: {
          action: z.enum(["infer", "validate", "get"]).default("get"),
        },
        async execute(args, context) {
          return toolResult(input, context, "design_contract", args)
        },
      }),
      agentic_eda_capability: tool({
        description: "Inspect local EDA/PDK capability evidence before choosing a tool, execution target, or flow. Use scope='agent_context' first for a compact decision packet, 'conflicts' for PATH/license/PDK conflicts, 'tools' for adapter details, 'readiness' for design gates, 'manifests' for capability manifests, or 'summary' for the graph.",
        args: {
          scope: z.enum(["agent_context", "summary", "tools", "readiness", "manifests", "conflicts"]).default("agent_context"),
        },
        async execute(args, context) {
          return toolResult(input, context, "eda_capability", args)
        },
      }),
      agentic_layout_inspect: tool({
        description: "Inspect a GDS layout as structured chip geometry evidence: top cell, hierarchy, polygon count, layers, and compact layout summary. Use this after PnR or when discussing physical layout.",
        args: {
          path: z.string().describe("Path to a GDS file inside the resolved design workspace."),
        },
        async execute(args, context) {
          return toolResult(input, context, "layout_inspect", args)
        },
      }),
      agentic_timing_inspect: tool({
        description: "Parse an STA timing report into closure evidence: WNS/TNS, violating paths, module hints, and targeted fix suggestions. Use this instead of reading large timing reports directly.",
        args: {
          path: z.string().describe("Path to an STA timing report inside the resolved design workspace."),
        },
        async execute(args, context) {
          return toolResult(input, context, "timing_inspect", args)
        },
      }),
      agentic_drc_inspect: tool({
        description: "Parse DRC/LVS or EDA verification logs into structured diagnostics, errors, warnings, metrics, and file/location evidence. Use this for signoff debug before proposing fixes.",
        args: {
          path: z.string().describe("Path to a DRC, LVS, or EDA verification log/report inside the resolved design workspace."),
        },
        async execute(args, context) {
          return toolResult(input, context, "drc_inspect", args)
        },
      }),
      agentic_run_flow: tool({
        description: "Run the smallest needed EDA step through AgentIC with one calm execution target: native, wsl, or docker. Always provide eda_tool and stage so the backend can capture command history, logs, checkpoints, and VLSI verdicts; pass log_file when the tool writes one.",
        args: {
          command: z.string(),
          target: z.enum(["native", "wsl", "docker"]).default("native"),
          wsl_distro: z.string().optional().describe("Required only when target='wsl' and a specific distro should be used."),
          linux_workdir: z.string().optional().describe("Optional Linux path to cd into for WSL runs."),
          docker_image: z.string().optional().describe("Required when target='docker'."),
          docker_args: z.string().optional().describe("Optional extra docker run flags such as --user or environment variables."),
          eda_tool: z.string().describe("EDA tool name such as iverilog, verilator, yosys, openroad, opensta, magic, klayout, netgen, calibre, genus, innovus, dc_shell, pt_shell, or xrun."),
          stage: z.string().describe("Flow stage such as simulation, lint, synthesis, sta, floorplan, placement, cts, routing, drc, lvs, pex, or signoff."),
          log_file: z.string().optional(),
          timeout: z.number().default(600),
        },
        async execute(args, context) {
          return toolResult(input, context, "run_flow", args)
        },
      }),
      agentic_query_pdk: tool({
        description: "Query AgentIC's local PDK/capability graph for exact libraries, cells, routing layers, memory macros, readiness gates, manifests, tool adapters, and flow evidence. Use before naming stdcells/macros/layers/corners/decks, and use find_memory before any SRAM/ROM decision.",
        args: {
          query_type: z.enum(["list_libraries", "find_cell", "get_layers", "capability_summary", "find_memory", "readiness", "manifest_status", "tool_adapters"]),
          cell_type: z.string().optional(),
        },
        async execute(args, context) {
          return toolResult(input, context, "query_pdk", args)
        },
      }),
      agentic_report: tool({
        description: "Generate the AgentIC checkpoint/signoff summary for the current VLSI design session. Use before claiming simulation, synthesis, STA, DRC, LVS, antenna, ERC, or tapeout readiness.",
        args: {},
        async execute(args, context) {
          return toolResult(input, context, "report", args)
        },
      }),
      agentic_ledger: tool({
        description: "Read or update AgentIC's durable VLSI design facts, role handoffs, and evidence graph for the current session.",
        args: {
          action: z.enum(["get_state", "record_fact", "record_handoff", "record_evidence"]),
          namespace: z.string().optional(),
          key: z.string().optional(),
          value: z.any().optional(),
          source: z.string().optional(),
          source_role: z.string().optional(),
          target_role: z.string().optional(),
          payload: z.any().optional(),
          kind: z.string().optional(),
          ref: z.string().optional(),
          links: z.any().optional(),
          max_events: z.number().default(12),
        },
        async execute(args, context) {
          return toolResult(input, context, "ledger", args)
        },
      }),
      agentic_import_repo: tool({
        description: "Clone a GitHub repository into the workspace. Use when the user provides a repo URL.",
        args: {
          url: z.string().describe("GitHub repo URL (https://github.com/user/repo or git@github.com:user/repo)"),
          target_dir: z.string().optional().describe("Optional subdirectory name"),
          branch: z.string().default("main"),
          token: z.string().default("").describe("GitHub PAT for private repos"),
        },
        async execute(args, context) {
          return toolResult(input, context, "git_clone", args)
        },
      }),
    },
  }
}
