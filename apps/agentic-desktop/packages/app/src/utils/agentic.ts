const DEFAULT_AGENTIC_URL = typeof window !== "undefined" && window.location.origin !== "null" ? window.location.origin : "http://127.0.0.1:7860"

export function getAgenticBase(): string {
  return localStorage.getItem("agentic_local_api_base")?.replace(/\/+$/, "") || DEFAULT_AGENTIC_URL
}

export type AgenticSessionPayload = {
  session_id: string
  agentic_mode?: string
  workspace_root?: string
  pdk_profile?: string
  design_name?: string
}

async function agenticModeForSession(base: string, session: AgenticSessionPayload | { session_id: string; agentic_mode?: string }) {
  if (session.agentic_mode) return session.agentic_mode
  try {
    const response = await fetch(`${base}/opencode/session/mode/${encodeURIComponent(session.session_id)}`)
    if (response.ok) {
      const data = await response.json()
      if (data?.success && data.agentic_mode) return data.agentic_mode
    }
  } catch {
    // Fall back to advisor when the bridge is not ready yet.
  }
  return "advisor"
}

type AgenticToolResponse = {
  success: boolean
  result: string
  session?: { design_name?: string; design_root?: string; run_id?: string }
}

export async function callAgenticTool(
  toolName: string,
  session: AgenticSessionPayload,
  args: Record<string, unknown>,
): Promise<AgenticToolResponse> {
  const base = getAgenticBase()
  const agenticMode = await agenticModeForSession(base, session)
  const response = await fetch(`${base}/opencode/tool`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      session_id: session.session_id,
      agent: "agentic-vlsi",
      agentic_mode: agenticMode,
      workspace_root: session.workspace_root || "",
      pdk_profile: session.pdk_profile || "",
      design_name: session.design_name || "",
      name: toolName,
      args,
    }),
  })
  if (!response.ok) {
    throw new Error(`AgentIC backend ${response.status}: ${response.statusText}`)
  }
  return response.json()
}

export async function callAgenticResolve(session: {
  session_id: string
  agentic_mode?: string
  workspace_root?: string
  user_text?: string
}): Promise<Record<string, unknown>> {
  const base = getAgenticBase()
  const agenticMode = await agenticModeForSession(base, session)
  const response = await fetch(`${base}/opencode/session/resolve`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      session_id: session.session_id,
      agent: "agentic-vlsi",
      agentic_mode: agenticMode,
      workspace_root: session.workspace_root || "",
      user_text: session.user_text || "",
    }),
  })
  if (!response.ok) {
    throw new Error(`AgentIC backend ${response.status}: ${response.statusText}`)
  }
  return response.json()
}
