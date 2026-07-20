import type { SnapshotFileDiff, VcsFileDiff } from "@opencode-ai/sdk/v2"
import type { Message } from "@opencode-ai/sdk/v2/client"

type Diff = SnapshotFileDiff | VcsFileDiff
type DisplayableDiff = Diff & {
  file: string
  patch: string
  additions: number
  deletions: number
}

function diff(value: unknown): value is DisplayableDiff {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  if (!("file" in value) || typeof value.file !== "string") return false
  if (!("patch" in value) || typeof value.patch !== "string") return false
  if (!("additions" in value) || typeof value.additions !== "number") return false
  if (!("deletions" in value) || typeof value.deletions !== "number") return false
  if (!("status" in value) || value.status === undefined) return true
  return value.status === "added" || value.status === "deleted" || value.status === "modified"
}

function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value)
}

/** Returns true for AgentIC internal backend state files and EDA/VLSI build artifacts that should never be shown in diffs. */
function isIrrelevantFile(file: string): boolean {
  if (file.includes("/.agentic/") || file.startsWith(".agentic/")) return true
  if (file.includes("/.git/") || file.startsWith(".git/")) return true
  if (file.includes("/node_modules/") || file.startsWith("node_modules/")) return true

  // EDA / VLSI Build & Execution Directories
  if (file.includes("/obj_dir/") || file.startsWith("obj_dir/")) return true
  if (file.includes("/csrc/") || file.startsWith("csrc/")) return true
  if (file.includes("/simv.daidir/") || file.startsWith("simv.daidir/")) return true
  if (file.includes("/simv.vdb/") || file.startsWith("simv.vdb/")) return true
  if (file.includes("/xcelium.d/") || file.startsWith("xcelium.d/")) return true
  if (file.includes("/work/") || file.startsWith("work/")) return true
  if (file.includes("/work._info/") || file.startsWith("work._info/")) return true
  if (file.includes("/runs/") || file.startsWith("runs/")) return true

  const lower = file.toLowerCase()
  const base = lower.split("/").pop() ?? ""

  if (base === "simv" || base === "xmsim.key") return true

  // Waveforms, compiled outputs, logs, reports, and object files
  if (
    lower.endsWith(".vcd") ||
    lower.endsWith(".fst") ||
    lower.endsWith(".wlf") ||
    lower.endsWith(".fsdb") ||
    lower.endsWith(".vpd") ||
    lower.endsWith(".vvp") ||
    lower.endsWith(".log") ||
    lower.endsWith(".rpt") ||
    lower.endsWith(".jou") ||
    lower.endsWith(".cmd") ||
    lower.endsWith(".history") ||
    lower.endsWith(".o") ||
    lower.endsWith(".a") ||
    lower.endsWith(".d") ||
    lower.endsWith(".out") ||
    lower.endsWith(".tmp") ||
    lower.endsWith(".swp") ||
    lower.endsWith(".bak")
  ) {
    return true
  }

  return false
}

export function diffs(value: unknown): Diff[] {
  if (Array.isArray(value) && value.every(diff)) return value.filter((d) => !isIrrelevantFile(d.file))
  if (Array.isArray(value)) return value.filter(diff).filter((d) => !isIrrelevantFile(d.file))
  if (diff(value)) return isIrrelevantFile(value.file) ? [] : [value]
  if (!object(value)) return []
  return Object.values(value).filter(diff).filter((d) => !isIrrelevantFile(d.file))
}

export function message(value: Message): Message {
  if (value.role !== "user") return value

  const raw = value.summary as unknown
  if (raw === undefined) return value
  if (!object(raw)) return { ...value, summary: undefined }

  const title = typeof raw.title === "string" ? raw.title : undefined
  const body = typeof raw.body === "string" ? raw.body : undefined
  const next = diffs(raw.diffs)

  if (title === raw.title && body === raw.body && next === raw.diffs) return value

  return {
    ...value,
    summary: {
      ...(title === undefined ? {} : { title }),
      ...(body === undefined ? {} : { body }),
      diffs: next,
    },
  }
}
