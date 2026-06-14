import type { Hooks, PluginInput, ToolContext, ToolResult } from "@opencode-ai/plugin"
import { tool } from "@opencode-ai/plugin"
import { z } from "zod"

const AGENT_NAME = "agentic-vlsi"
const DEFAULT_AGENTIC_LOCAL_URL = "http://127.0.0.1:7860"

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

async function callAgentic<T>(path: string, payload: Record<string, unknown>): Promise<T> {
  const response = await fetch(`${agenticBaseUrl()}${path}`, {
    method: "POST",
    headers: agenticHeaders(),
    body: JSON.stringify(payload),
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

async function sessionAgent(input: PluginInput, sessionID?: string) {
  if (!sessionID) return undefined
  try {
    const info = await input.client.session.get({ path: { id: sessionID } } as any)
    return (info as any)?.agent || (info as any)?.info?.agent
  } catch {
    return undefined
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

function sessionPayload(input: PluginInput, context: ToolContext, extra: Partial<AgenticSession> = {}): AgenticSession {
  return {
    session_id: extra.session_id || context.sessionID,
    message_id: extra.message_id || context.messageID,
    agent: extra.agent || context.agent || AGENT_NAME,
    agentic_mode: extra.agentic_mode || process.env.AGENTIC_MODE || "advisor",
    user_text: extra.user_text || "",
    workspace_root: extra.workspace_root || context.worktree || input.worktree,
    pdk_profile: extra.pdk_profile || process.env.AGENTIC_PDK_PROFILE || process.env.PDK || "",
    design_name: extra.design_name,
  }
}

async function runAgenticTool(input: PluginInput, context: ToolContext, payload: Omit<AgenticToolPayload, keyof AgenticSession>) {
  const response = await callAgentic<{
    success: boolean
    result: string
    session?: { design_name?: string; design_root?: string; run_id?: string }
  }>("/opencode/tool", {
    ...sessionPayload(input, context),
    ...payload,
  })
  const design = response.session?.design_name
  const run = response.session?.run_id
  const title = design ? `AgentIC ${payload.name}: ${design}` : `AgentIC ${payload.name}`
  const output = response.result || (response.success ? "AgentIC tool completed." : "AgentIC tool failed.")
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

export async function AgenticVlsiPlugin(input: PluginInput): Promise<Hooks> {
  return {
    "experimental.chat.system.transform": async (ctxInput, ctxOutput) => {
      if (!ctxInput.sessionID) return
      const activeAgent = await sessionAgent(input, ctxInput.sessionID)
      if (activeAgent && activeAgent !== AGENT_NAME) return
      const userText = await latestUserText(input, ctxInput.sessionID)
      const response = await callAgentic<any>("/opencode/session/resolve", {
        session_id: ctxInput.sessionID,
        agent: activeAgent || AGENT_NAME,
        user_text: userText,
        workspace_root: input.worktree,
        pdk_profile: process.env.AGENTIC_PDK_PROFILE || process.env.PDK || "",
        agentic_mode: process.env.AGENTIC_MODE || "advisor",
      }).catch((error) => ({ success: false, error: error instanceof Error ? error.message : String(error) }))

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

      ctxOutput.system.push(
        [
          "## AgentIC Desktop Runtime Contract",
          `Session: ${response.session?.session_id}`,
          `Design: ${response.session?.design_name}`,
          `Design root: ${response.session?.design_root}`,
          `AgentIC mode: ${response.session?.agentic_mode || "advisor"}`,
          `Workflow: ${response.workflow?.mode} (${response.workflow?.intent})`,
          `Scope: ${response.kernel_scope}`,
          "",
          "Use the desktop runtime for session/tool UX, but use AgentIC bridge tools for VLSI-specific decisions.",
          "In advisor mode, do not write RTL/TB/scripts or run shell/EDA commands; only inspect/query and write docs/plans/diagrams/reports.",
          "In builder mode, make a plan, wait for approval when starting implementation, then use AgentIC tools for edits and execution.",
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
          const response = await callAgentic<any>("/opencode/session/resolve", {
            ...sessionPayload(input, context, {
              user_text: args.user_text,
              pdk_profile: args.pdk_profile,
              design_name: args.design_name,
            }),
          })
          return {
            title: `AgentIC context: ${response.session?.design_name || context.sessionID}`,
            output: JSON.stringify({
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
        description: "Read, search, or list files inside the AgentIC-resolved VLSI design workspace.",
        args: {
          action: z.enum(["read", "search", "list"]),
          path: z.string().default("."),
          pattern: z.string().default(""),
        },
        async execute(args, context) {
          return toolResult(input, context, "workspace", args)
        },
      }),
      agentic_write: tool({
        description: "Create or surgically edit files inside the AgentIC-resolved VLSI design workspace.",
        args: {
          path: z.string(),
          content: z.string().default(""),
          old_string: z.string().default(""),
          new_string: z.string().default(""),
        },
        async execute(args, context) {
          return toolResult(input, context, "write", args)
        },
      }),
      agentic_bash: tool({
        description: "Run a local EDA command through AgentIC so checkpoints, logs, and VLSI failure evidence are captured.",
        args: {
          command: z.string(),
          eda_tool: z.string().optional(),
          stage: z.string().optional(),
          log_file: z.string().optional(),
          timeout: z.number().default(300),
        },
        async execute(args, context) {
          return toolResult(input, context, "bash", args)
        },
      }),
      agentic_query_pdk: tool({
        description: "Query AgentIC's local PDK/capability graph for libraries, cells, routing layers, memory macros, readiness gates, manifests, tool adapters, and flow evidence.",
        args: {
          query_type: z.enum(["list_libraries", "find_cell", "get_layers", "capability_summary", "find_memory", "readiness", "manifest_status", "tool_adapters"]),
          cell_type: z.string().optional(),
        },
        async execute(args, context) {
          return toolResult(input, context, "query_pdk", args)
        },
      }),
      agentic_report: tool({
        description: "Generate the AgentIC checkpoint/signoff summary for the current VLSI design session.",
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
    },
  }
}
