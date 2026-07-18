import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs"
import { basename, dirname, join, relative, resolve } from "node:path"

type Port = {
  name: string
  direction: "input" | "output" | "inout"
  width: string
  role: string
}

type Instance = {
  module: string
  instance: string
}

type ContractModule = {
  file: string
  port_count: number
  ports: Port[]
  instances: Instance[]
  always_count: number
  assign_count: number
  nontrivial_assign_count: number
  line: number
}

type ContractIssue = {
  code: string
  severity: "error" | "warning"
  message: string
  path?: string
}

type DesignContract = {
  schema_version: "agentic.design_contract.v1"
  generated_at?: number
  validated_at?: number
  design_name: string
  top_module: string | null
  top_file: string | null
  clock_ports: string[]
  reset_ports: string[]
  io_ports: string[]
  submodules: string[]
  rtl_files: string[]
  module_count: number
  modules: Record<string, ContractModule>
  constraints: {
    sdc_files: string[]
    clock_defined: boolean
    clock_names: string[]
  }
  policy: Record<string, boolean>
  status: Record<string, string | boolean>
  validation_issues: ContractIssue[]
  next_actions: string[]
  compact_for_agent: string
}

type DesignState = Record<string, any> & {
  design_name: string
  created_at: number
  updated_at: number
  design_contract?: DesignContract
  stage_status: Record<string, { status?: string }>
  checkpoints: Array<Record<string, any>>
  evidence_graph: { nodes: Record<string, any>; edges: Array<Record<string, any>> }
  timeline: Array<Record<string, any>>
}

const EXCLUDE_DIRS = new Set([".git", ".agentic", "node_modules", "out", "dist", "build", ".venv", "venv", "__pycache__"])
const RTL_EXTENSIONS = [".v", ".sv"]
const EXTERNAL_PREFIXES = ["sky130_", "gf180", "asap7", "tsmc", "saed", "nangate"]
const SV_KEYWORDS = new Set([
  "always",
  "always_comb",
  "always_ff",
  "always_latch",
  "assign",
  "begin",
  "case",
  "else",
  "end",
  "for",
  "function",
  "generate",
  "if",
  "initial",
  "input",
  "inout",
  "localparam",
  "logic",
  "module",
  "output",
  "parameter",
  "reg",
  "task",
  "wire",
])

export async function runDesignContractTool(payload: {
  workspace_root?: string
  design_name?: string
  args?: Record<string, unknown>
}) {
  const workspaceRoot = resolveWorkspaceRoot(payload.workspace_root || process.cwd())
  const designName = safeDesignName(String(payload.design_name || "scratch"))
  const action = String(payload.args?.action || "get").trim().toLowerCase()

  if (action === "infer") {
    const contract = inferDesignContract(workspaceRoot, designName)
    saveDesignContract(workspaceRoot, designName, contract)
    return bridgeResult(contract, designName, workspaceRoot)
  }

  if (action === "validate") {
    const existing = loadDesignState(workspaceRoot, designName).design_contract
    const contract = validateDesignContract(existing || inferDesignContract(workspaceRoot, designName), workspaceRoot, designName)
    saveDesignContract(workspaceRoot, designName, contract)
    return bridgeResult(contract, designName, workspaceRoot)
  }

  if (action === "get") {
    const contract = loadDesignState(workspaceRoot, designName).design_contract
    if (!contract) {
      return bridgeResult({
        schema_version: "agentic.design_contract.v1",
        status: { contract: "missing" },
        message: "No active design contract is stored yet. Run agentic_design_contract(action='infer') first.",
        next_actions: ["Run design_contract infer", "Then run design_contract validate"],
      }, designName, workspaceRoot)
    }
    return bridgeResult(compactDesignContract(contract), designName, workspaceRoot)
  }

  return {
    success: false,
    error: `Unknown design_contract action '${action}'. Use infer, validate, or get.`,
    session: { design_name: designName, design_root: workspaceRoot },
  }
}

export function inferDesignContract(workspaceRoot: string, designName = "scratch"): DesignContract {
  const rtlFiles = findRtlFiles(workspaceRoot)
  const modules: Record<string, ContractModule> = {}
  const referenced = new Set<string>()

  for (const relPath of rtlFiles) {
    let content = ""
    try {
      content = readFileSync(join(workspaceRoot, relPath), "utf8")
    } catch {
      continue
    }
    for (const module of parseRtlModules(content, relPath)) {
      modules[module.name] = compactModule(module)
      for (const instance of module.instances) {
        if (instance.module) referenced.add(instance.module)
      }
    }
  }

  const topModule = selectTopModule(modules, referenced)
  const top = topModule ? modules[topModule] : undefined
  const sdcFiles = findSdcFiles(workspaceRoot)
  const clockPorts = clockPortsFor(top?.ports || [])
  const resetPorts = resetPortsFor(top?.ports || [])
  const [clockDefined, clockNames] = sdcClockStatus(workspaceRoot, sdcFiles, clockPorts)
  const validationIssues: ContractIssue[] = []
  if (!topModule) validationIssues.push(contractIssue("top_missing", "error", "No top module candidate could be inferred from RTL."))

  const contract: DesignContract = {
    schema_version: "agentic.design_contract.v1",
    generated_at: Date.now() / 1000,
    design_name: designName,
    top_module: topModule,
    top_file: top?.file || null,
    clock_ports: clockPorts,
    reset_ports: resetPorts,
    io_ports: (top?.ports || []).map((port) => port.name),
    submodules: sortedUnique((top?.instances || []).map((instance) => instance.module).filter(Boolean)),
    rtl_files: rtlFiles.slice(0, 200),
    module_count: Object.keys(modules).length,
    modules: Object.fromEntries(Object.entries(modules).sort(([a], [b]) => a.localeCompare(b))),
    constraints: {
      sdc_files: sdcFiles.slice(0, 50),
      clock_defined: clockDefined,
      clock_names: clockNames,
    },
    policy: {
      top_structural_only: true,
      one_module_per_file: true,
      constraints_required: true,
      no_behavioral_large_memory: true,
    },
    status: {
      contract: validationIssues.length ? "draft" : "inferred",
      top_valid: Boolean(topModule),
      sdc_clock: clockDefined ? "present" : "missing",
      ...contractStageStatus(workspaceRoot, designName),
    },
    validation_issues: validationIssues,
    next_actions: [],
    compact_for_agent: "",
  }
  contract.next_actions = contractNextActions(contract)
  contract.compact_for_agent = contractCompactText(contract)
  return contract
}

export function validateDesignContract(contract: DesignContract, workspaceRoot: string, designName = "scratch"): DesignContract {
  const inferred = inferDesignContract(workspaceRoot, designName)
  const activeTop = contract.top_module || inferred.top_module
  const merged: DesignContract = { ...inferred }
  if (activeTop && inferred.modules[activeTop]) {
    merged.top_module = activeTop
    merged.top_file = contract.top_file || inferred.modules[activeTop].file
  }

  const issues = [...(merged.validation_issues || [])]
  const top = merged.top_module ? merged.modules[merged.top_module] : undefined
  if (!top) {
    issues.push(contractIssue("top_missing", "error", "Active top module does not exist in scanned RTL."))
  } else {
    if (top.always_count > 0 || top.nontrivial_assign_count > 2) {
      issues.push(contractIssue(
        "top_not_structural",
        "error",
        `Top module '${merged.top_module}' has ${top.always_count} always block(s) and ${top.nontrivial_assign_count} non-trivial assign(s).`,
        top.file,
      ))
    }
    for (const child of merged.submodules.filter((name) => !merged.modules[name] && !externalModuleOk(name)).slice(0, 12)) {
      issues.push(contractIssue("missing_submodule", "warning", `Top instantiates '${child}', but no local RTL module was found. It may be an external macro/IP.`, top.file))
    }
    if (merged.clock_ports.length === 0) issues.push(contractIssue("clock_missing", "warning", "No obvious top clock port was detected."))
    if (merged.reset_ports.length === 0) issues.push(contractIssue("reset_missing", "warning", "No obvious top reset port was detected."))
    if (!merged.constraints.clock_defined) issues.push(contractIssue("sdc_clock_missing", "warning", "No SDC create_clock matching the top clock was found."))
    issues.push(...topQualityIssues(workspaceRoot, top.file))
  }

  merged.validation_issues = dedupeIssues(issues)
  const hasErrors = merged.validation_issues.some((issue) => issue.severity === "error")
  const hasWarnings = merged.validation_issues.some((issue) => issue.severity === "warning")
  merged.status.contract = hasErrors ? "invalid" : hasWarnings ? "valid_with_warnings" : "valid"
  merged.status.top_valid = !merged.validation_issues.some((issue) => issue.severity === "error" && ["top_missing", "top_not_structural"].includes(issue.code))
  merged.next_actions = contractNextActions(merged)
  merged.compact_for_agent = contractCompactText(merged)
  merged.validated_at = Date.now() / 1000
  return merged
}

function bridgeResult(result: unknown, designName: string, workspaceRoot: string) {
  return {
    success: true,
    result: JSON.stringify(result, null, 2),
    session: { design_name: designName, design_root: workspaceRoot },
  }
}

function resolveWorkspaceRoot(workspaceRoot: string) {
  const normalized = workspaceRoot.replace(/\\/g, "/")
  if (process.platform !== "win32" && normalized.startsWith("//wsl.localhost/")) {
    const parts = normalized.split("/").filter(Boolean)
    if (parts.length >= 3) return "/" + parts.slice(2).join("/")
  }
  if (process.platform !== "win32" && normalized.startsWith("//wsl$/")) {
    const parts = normalized.split("/").filter(Boolean)
    if (parts.length >= 3) return "/" + parts.slice(2).join("/")
  }
  if (process.platform !== "win32" && normalized.length >= 2 && normalized[1] === ":") {
    return `/mnt/${normalized[0].toLowerCase()}${normalized.substring(2)}`
  }
  return resolve(workspaceRoot)
}

function safeDesignName(designName: string) {
  const cleaned = [...(designName || "scratch")]
    .map((ch) => (/^[A-Za-z0-9_.-]$/.test(ch) ? ch : "_"))
    .join("")
    .replace(/^[._]+|[._]+$/g, "")
  return cleaned || "scratch"
}

function statePath(workspaceRoot: string, designName: string) {
  return join(workspaceRoot, ".agentic", "design_state", `${safeDesignName(designName)}.json`)
}

function loadDesignState(workspaceRoot: string, designName: string): DesignState {
  const now = Date.now() / 1000
  const base: DesignState = {
    version: 1,
    design_name: safeDesignName(designName),
    created_at: now,
    updated_at: now,
    intent: {},
    design_contract: undefined,
    module_ownership: {},
    implementation_policy: {},
    mental_model: { spec: {}, interfaces: {}, constraints: {}, assumptions: {}, decisions: {} },
    stage_status: {},
    artifacts: {},
    artifact_index: { schema_version: "agentic.artifact_kernel.v1", current_by_key: {}, entries: {}, superseded: {}, revisions: [] },
    checkpoints: [],
    commands: [],
    handoffs: [],
    evidence_graph: { nodes: {}, edges: [] },
    timeline: [],
  }
  const path = statePath(workspaceRoot, designName)
  if (!existsSync(path)) return base
  try {
    const loaded = JSON.parse(readFileSync(path, "utf8"))
    return {
      ...base,
      ...loaded,
      design_name: loaded.design_name || base.design_name,
      updated_at: now,
      stage_status: loaded.stage_status || {},
      checkpoints: Array.isArray(loaded.checkpoints) ? loaded.checkpoints : [],
      evidence_graph: loaded.evidence_graph || { nodes: {}, edges: [] },
      timeline: Array.isArray(loaded.timeline) ? loaded.timeline : [],
    }
  } catch {
    return base
  }
}

function saveDesignContract(workspaceRoot: string, designName: string, contract: DesignContract) {
  const state = loadDesignState(workspaceRoot, designName)
  const now = Date.now() / 1000
  state.updated_at = now
  state.design_contract = contract
  state.timeline.push({
    time: now,
    kind: "contract",
    action: "design_contract_updated",
    payload: {
      top_module: contract.top_module,
      top_file: contract.top_file,
      status: contract.status.contract,
      issue_count: contract.validation_issues.length,
    },
  })
  state.timeline = state.timeline.slice(-300)

  const path = statePath(workspaceRoot, designName)
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, `${JSON.stringify(state, null, 2)}\n`, "utf8")
}

function findRtlFiles(workspaceRoot: string) {
  const results: string[] = []
  walk(workspaceRoot, (fullPath, relPath, isDirectory) => {
    if (isDirectory) return
    if (relPath.split(/[\\/]/).some((part) => ["sim", "tb", "testbench", "verification"].includes(part.toLowerCase()))) return
    if (RTL_EXTENSIONS.some((ext) => relPath.toLowerCase().endsWith(ext))) results.push(relPath)
  })
  return results.sort((a, b) => Number(!a.toLowerCase().includes("/rtl/")) - Number(!b.toLowerCase().includes("/rtl/")) || a.localeCompare(b)).slice(0, 300)
}

function findSdcFiles(workspaceRoot: string) {
  const results: string[] = []
  walk(workspaceRoot, (_fullPath, relPath, isDirectory) => {
    if (!isDirectory && relPath.toLowerCase().endsWith(".sdc")) results.push(relPath)
  })
  return results.sort().slice(0, 100)
}

function walk(root: string, visitor: (fullPath: string, relPath: string, isDirectory: boolean) => void) {
  if (!existsSync(root)) return
  const stack = [root]
  while (stack.length > 0) {
    const dir = stack.pop()
    if (!dir) continue
    let entries: string[] = []
    try {
      entries = readdirSync(dir)
    } catch {
      continue
    }
    for (const entry of entries) {
      if (EXCLUDE_DIRS.has(entry)) continue
      const full = join(dir, entry)
      let stat
      try {
        stat = statSync(full)
      } catch {
        continue
      }
      const relPath = relative(root, full).replace(/\\/g, "/")
      const isDirectory = stat.isDirectory()
      visitor(full, relPath, isDirectory)
      if (isDirectory) stack.push(full)
    }
  }
}

function parseRtlModules(content: string, relPath: string) {
  const text = stripSvComments(content)
  const modules: Array<ContractModule & { name: string }> = []
  const moduleRegex = /\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b([\s\S]*?)\bendmodule\b/g
  let match: RegExpExecArray | null
  while ((match = moduleRegex.exec(text))) {
    const name = match[1]
    const body = match[2] || ""
    const header = body.includes(";") ? body.split(";", 1)[0] : body.slice(0, 1000)
    const ports = contractPorts(`${header}\n${body}`)
    const instances = contractInstances(body)
    modules.push({
      name,
      file: relPath,
      port_count: ports.length,
      ports,
      instances,
      always_count: (body.match(/^\s*always(?:_[a-z]+)?\b/gm) || []).length,
      assign_count: (body.match(/^\s*assign\s+/gm) || []).length,
      nontrivial_assign_count: contractNontrivialAssignCount(body),
      line: offsetToLine(text, match.index),
    })
  }
  return modules
}

function compactModule(module: ContractModule & { name: string }): ContractModule {
  return {
    file: module.file,
    port_count: module.port_count,
    ports: module.ports.slice(0, 40),
    instances: module.instances.slice(0, 80),
    always_count: module.always_count,
    assign_count: module.assign_count,
    nontrivial_assign_count: module.nontrivial_assign_count,
    line: module.line,
  }
}

function stripSvComments(content: string) {
  return content
    .replace(/\/\*[\s\S]*?\*\//g, (block) => block.replace(/[^\n]/g, " "))
    .replace(/\/\/.*$/gm, "")
}

function contractPorts(text: string) {
  const ports: Record<string, Port> = {}
  for (const statement of text.split(/[;\n]/)) {
    if (!/\b(input|output|inout)\b/i.test(statement)) continue
    let direction: Port["direction"] | undefined
    let width = "1"
    for (const rawPart of statement.split(",")) {
      let part = rawPart.trim()
      const directionMatch = part.match(/\b(input|output|inout)\b/i)
      if (directionMatch) {
        direction = directionMatch[1].toLowerCase() as Port["direction"]
        part = part.slice(directionMatch.index! + directionMatch[0].length).trim()
      }
      if (!direction) continue
      const widthMatch = part.match(/\[[^\]]+\]/)
      if (widthMatch) width = widthMatch[0].trim()
      part = part
        .replace(/\b(wire|reg|logic|signed)\b/gi, " ")
        .replace(/\[[^\]]+\]/g, " ")
        .replace(/=.*/g, " ")
        .replace(/[().]/g, " ")
        .trim()
      const name = part.match(/[A-Za-z_][A-Za-z0-9_$]*$/)?.[0] || ""
      if (name && !SV_KEYWORDS.has(name.toLowerCase())) {
        ports[name] = { name, direction, width, role: portRole(name) }
      }
    }
  }
  return Object.values(ports).slice(0, 256)
}

function contractInstances(body: string) {
  const instances: Instance[] = []
  const instanceRegex = /(?:^|;)\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\([\s\S]*?\)\s*)?([A-Za-z_][A-Za-z0-9_$]*)\s*\(/gm
  let match: RegExpExecArray | null
  while ((match = instanceRegex.exec(body))) {
    const moduleType = match[1]
    const instanceName = match[2]
    if (SV_KEYWORDS.has(moduleType.toLowerCase()) || SV_KEYWORDS.has(instanceName.toLowerCase())) continue
    instances.push({ module: moduleType, instance: instanceName })
  }
  return instances.slice(0, 256)
}

function contractNontrivialAssignCount(body: string) {
  let count = 0
  const assignRegex = /^\s*assign\s+[^=]+=\s*([^;]+);/gm
  let match: RegExpExecArray | null
  while ((match = assignRegex.exec(body))) {
    const rhs = match[1].trim()
    if (!/^[A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?$/.test(rhs)) count += 1
  }
  return count
}

function selectTopModule(modules: Record<string, ContractModule>, referenced: Set<string>) {
  const names = Object.keys(modules)
  if (!names.length) return null
  const candidates = names.filter((name) => !referenced.has(name))
  const pool = candidates.length ? candidates : names
  return pool.sort((a, b) => scoreTop(modules[b], b) - scoreTop(modules[a], a) || a.localeCompare(b))[0]
}

function scoreTop(module: ContractModule, name: string) {
  const lowerName = name.toLowerCase()
  const lowerFile = module.file.toLowerCase()
  let score = 0
  if (["chip_top", "soc_top", "top"].includes(lowerName)) score += 100
  if (lowerName.endsWith("_top") || lowerName.endsWith("top")) score += 60
  if (`/${lowerFile}`.includes("/top/") || lowerFile.endsWith("_top.v") || lowerFile.endsWith("_top.sv")) score += 40
  score += Math.min(module.instances.length, 20)
  score += Math.min(module.ports.length, 20)
  return score
}

function clockPortsFor(ports: Port[]) {
  return ports.filter((port) => port.direction === "input" && /(^clk$|clock|clk_?|_clk)/i.test(port.name)).map((port) => port.name).slice(0, 16)
}

function resetPortsFor(ports: Port[]) {
  return ports.filter((port) => port.direction === "input" && /(rst|reset)/i.test(port.name)).map((port) => port.name).slice(0, 16)
}

function portRole(name: string) {
  const lower = name.toLowerCase()
  if (/(^clk$|clock|clk_?|_clk)/.test(lower)) return "clock"
  if (lower.includes("rst") || lower.includes("reset")) return "reset"
  if (lower.includes("valid") || lower.includes("ready")) return "handshake"
  if (lower.includes("addr")) return "address"
  if (lower.includes("data")) return "data"
  return "signal"
}

function sdcClockStatus(workspaceRoot: string, sdcFiles: string[], clockPorts: string[]): [boolean, string[]] {
  const clocks = new Set<string>()
  for (const relPath of sdcFiles.slice(0, 20)) {
    let text = ""
    try {
      text = readFileSync(join(workspaceRoot, relPath), "utf8")
    } catch {
      continue
    }
    for (const line of text.match(/create_clock\b[^\n;]*/gi) || []) {
      for (const match of line.matchAll(/\b(?:get_ports|get_pins)\s+([A-Za-z_][A-Za-z0-9_$]*)/g)) {
        clocks.add(match[1])
      }
      for (const port of clockPorts) {
        if (new RegExp(`\\b${escapeRegExp(port)}\\b`).test(line)) clocks.add(port)
      }
    }
  }
  const unique = [...clocks].sort()
  if (!clockPorts.length) return [unique.length > 0, unique]
  return [clockPorts.some((port) => clocks.has(port)), unique]
}

function contractStageStatus(workspaceRoot: string, designName: string) {
  const state = loadDesignState(workspaceRoot, designName)
  const statuses: Record<string, string> = {}
  for (const stage of ["rtl", "lint", "simulation", "synthesis", "sta", "drc", "lvs", "signoff"]) {
    statuses[stage] = state.stage_status?.[stage]?.status || "missing"
  }
  for (const checkpoint of state.checkpoints || []) {
    const stage = String(checkpoint.stage || "").toLowerCase()
    const mapped = stage.includes("sim")
      ? "simulation"
      : stage.includes("synth")
        ? "synthesis"
        : stage.includes("sta") || stage.includes("timing")
          ? "sta"
          : stage.includes("drc")
            ? "drc"
            : stage.includes("lvs")
              ? "lvs"
              : stage.includes("lint")
                ? "lint"
                : stage
    if (mapped in statuses) statuses[mapped] = checkpoint.pass ? "passed" : "failed"
  }
  return statuses
}

function topQualityIssues(workspaceRoot: string, relPath: string) {
  const issues: ContractIssue[] = []
  let content = ""
  try {
    content = readFileSync(join(workspaceRoot, relPath), "utf8")
  } catch {
    return issues
  }
  const modules = parseRtlModules(content, relPath)
  if (modules.length > 1) {
    issues.push(contractIssue("multiple_modules_in_file", "error", "Design file contains more than one module.", relPath))
  }
  if (/\binitial\b/.test(content) || /(^|[^$])#\s*\d+/.test(content) || /\$system\b/.test(content)) {
    issues.push(contractIssue("non_synthesizable_in_design", "error", "Top file contains non-synthesizable initial/delay/system constructs.", relPath))
  }
  if (/\breg\s*(?:\[[^\]]+\]\s*)?[A-Za-z_][A-Za-z0-9_$]*\s*\[[^\]]+\]/.test(content)) {
    issues.push(contractIssue("behavioral_memory_in_design", "warning", "Top file declares behavioral memory; large memories should use PDK macro wrappers.", relPath))
  }
  return issues
}

function contractIssue(code: string, severity: ContractIssue["severity"], message: string, path?: string): ContractIssue {
  return path ? { code, severity, message, path } : { code, severity, message }
}

function dedupeIssues(issues: ContractIssue[]) {
  const seen = new Set<string>()
  const result: ContractIssue[] = []
  for (const issue of issues) {
    const key = `${issue.code}:${issue.severity}:${issue.message}:${issue.path || ""}`
    if (seen.has(key)) continue
    seen.add(key)
    result.push(issue)
  }
  return result.slice(0, 80)
}

function externalModuleOk(name: string) {
  const lower = name.toLowerCase()
  return EXTERNAL_PREFIXES.some((prefix) => lower.startsWith(prefix)) || lower.includes("__")
}

function contractNextActions(contract: DesignContract) {
  const actions: string[] = []
  const status = contract.status || {}
  if (!contract.top_module) actions.push("Select or create a top module, then rerun design_contract infer.")
  if (status.top_valid === false) actions.push("Fix top module structure: keep top as ports, wires, instances, and simple wiring only.")
  if (status.sdc_clock === "missing") actions.push("Create or update SDC with create_clock for the detected top clock.")
  if (["missing", "failed"].includes(String(status.lint || "")) && contract.top_file) actions.push(`Run rtl_repair_diagnose on ${contract.top_file}.`)
  if (status.simulation === "missing") actions.push("Run or create a self-checking simulation checkpoint.")
  if (["missing", "failed"].includes(String(status.sta || ""))) actions.push("Run STA or inspect timing evidence before timing claims.")
  return actions.slice(0, 8)
}

function contractCompactText(contract: DesignContract) {
  const status = contract.status || {}
  return [
    `Design contract: top=${contract.top_module || "unknown"}`,
    `file=${contract.top_file || "unknown"}`,
    `clocks=${contract.clock_ports.join(",") || "none"}`,
    `resets=${contract.reset_ports.join(",") || "none"}`,
    `submodules=${contract.submodules.length}`,
    `sdc_clock=${contract.constraints.clock_defined}`,
    `contract_status=${status.contract}`,
    `lint=${status.lint}`,
    `sim=${status.simulation}`,
    `sta=${status.sta}`,
    `drc=${status.drc}`,
    `lvs=${status.lvs}.`,
  ].join(" ")
}

function compactDesignContract(contract: DesignContract) {
  return {
    schema_version: contract.schema_version,
    design_name: contract.design_name,
    top_module: contract.top_module,
    top_file: contract.top_file,
    clock_ports: contract.clock_ports || [],
    reset_ports: contract.reset_ports || [],
    io_port_count: contract.io_ports?.length || 0,
    submodules: (contract.submodules || []).slice(0, 80),
    rtl_file_count: contract.rtl_files?.length || 0,
    constraints: contract.constraints || {},
    policy: contract.policy || {},
    status: contract.status || {},
    validation_issues: (contract.validation_issues || []).slice(0, 20),
    next_actions: contract.next_actions || [],
    compact_for_agent: contract.compact_for_agent || contractCompactText(contract),
  }
}

function sortedUnique(values: string[]) {
  return [...new Set(values)].sort()
}

function offsetToLine(text: string, offset: number) {
  return text.slice(0, offset).split("\n").length
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}
