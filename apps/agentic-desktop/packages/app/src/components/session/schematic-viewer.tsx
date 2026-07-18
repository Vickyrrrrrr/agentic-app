import { createResource, createSignal, createEffect, For, Show, createMemo, onCleanup } from "solid-js"
import { Icon } from "@opencode-ai/ui/icon"
import { Tabs } from "@opencode-ai/ui/tabs"
import { callAgenticTool } from "@/utils/agentic"
import { decode64 } from "@/utils/base64"
import { useSessionLayout } from "@/pages/session/session-layout"

export const SCHEMATIC_TAB_PREFIX = "schematic:"
export function schematicTab(path: string): string {
  return SCHEMATIC_TAB_PREFIX + path
}
export function pathFromSchematicTab(tab: string): string | undefined {
  if (!tab.startsWith(SCHEMATIC_TAB_PREFIX)) return undefined
  return tab.slice(SCHEMATIC_TAB_PREFIX.length)
}

type Bit = number | string
type YosysPort = { direction: "input" | "output" | "inout"; bits: Bit[] }
type YosysCell = {
  type: string
  connections: Record<string, Bit[]>
  port_directions?: Record<string, string>
}
type YosysModule = {
  ports: Record<string, YosysPort>
  cells: Record<string, YosysCell>
  netnames?: Record<string, { bits: Bit[]; hide_name?: number | string }>
}
type YosysJson = { modules: Record<string, YosysModule> }

type SchematicResponse = {
  available: boolean
  large_design?: boolean
  reason?: string
  log?: string
  module?: string
  yosys_json?: YosysJson
  ports?: number
  cells?: number
  cache_ref?: string
  netlist_summary?: {
    named_net_count?: number
    health?: {
      bit_count?: number
      undriven_count?: number
      unloaded_count?: number
      multidriven_count?: number
      high_fanout_count?: number
      constant_bit_uses?: Record<string, number>
      high_fanout_preview?: { bit: string; fanout: number; driver: string }[]
      undriven_preview?: { bit: string; load_count: number; loads: string[] }[]
      multidriven_preview?: { bit: string; driver_count: number; drivers: string[] }[]
    }
    complexity?: {
      recommended_mode?: string
      full_json_cell_limit?: number
      full_json_net_limit?: number
    }
  }
}

type NodeKind = "port" | "cell" | "const"
type LayoutNode = {
  id: string
  kind: NodeKind
  label: string
  sub?: string
  direction?: string
  width?: number
  column: number
  row: number
  x: number
  y: number
  w: number
  h: number
  nets: { name: string; bits: Bit[]; dir: string }[]
}
type Endpoint = { node: string; pin: string; dir: string }
type LayoutEdge = {
  id: string
  from: string
  to: string
  fromPin: string
  toPin: string
  bits: Bit[]
  netNames: string[]
  width: number
  lane: number
}

const CELL_W = 160
const CELL_H = 36
const COL_GAP = 84
const ROW_GAP = 16
const PORT_W = 144

function bitKey(b: Bit): string {
  return String(b)
}

function isConstBit(bit: Bit): boolean {
  return typeof bit === "string" && /^[01xz]$/i.test(bit)
}

function bitsPreview(bits: Bit[]): string {
  if (bits.length === 0) return "-"
  if (bits.length <= 4) return bits.map(bitKey).join(",")
  return `${bits.length}b · ${bitKey(bits[0])}…${bitKey(bits[bits.length - 1])}`
}

function bitLabel(bit: Bit, bitNames: Map<string, string[]>): string {
  if (isConstBit(bit)) return `1'b${String(bit).toLowerCase()}`
  const names = bitNames.get(bitKey(bit)) ?? []
  return names[0] ?? `#${bitKey(bit)}`
}

function busLabel(name: string, bits: Bit[], bitNames: Map<string, string[]>): string {
  if (bits.length === 0) return name
  if (bits.length === 1) return bitLabel(bits[0], bitNames)
  return `${name}[${bits.length - 1}:0]`
}

function buildBitNameIndex(ymod: YosysModule) {
  const bitNames = new Map<string, string[]>()
  const note = (bit: Bit, name: string) => {
    if (isConstBit(bit)) return
    const key = bitKey(bit)
    const names = bitNames.get(key) ?? []
    if (!names.includes(name)) names.push(name)
    bitNames.set(key, names)
  }

  for (const [portName, port] of Object.entries(ymod.ports ?? {})) {
    const bits = port.bits ?? []
    if (bits.length === 1) note(bits[0], portName)
    else bits.forEach((bit, idx) => note(bit, `${portName}[${idx}]`))
  }

  for (const [netName, net] of Object.entries(ymod.netnames ?? {})) {
    if (String(net.hide_name ?? "0") === "1") continue
    const bits = net.bits ?? []
    if (bits.length === 1) note(bits[0], netName)
    else bits.forEach((bit, idx) => note(bit, `${netName}[${idx}]`))
  }

  return bitNames
}

function buildLayout(resp: SchematicResponse) {
  const mod = resp.module!
  const ymod = resp.yosys_json!.modules[mod]
  if (!ymod) return { nodes: [] as LayoutNode[], edges: [] as LayoutEdge[], columns: 0, stats: { bitNets: 0, edges: 0 } }

  const ports = ymod.ports ?? {}
  const cells = ymod.cells ?? {}
  const bitNames = buildBitNameIndex(ymod)

  const inputPorts = Object.entries(ports).filter(([, p]) => p.direction === "input")
  const outputPorts = Object.entries(ports).filter(([, p]) => p.direction !== "input")

  const driverByBit = new Map<string, Endpoint>()
  const loadsByBit = new Map<string, Endpoint[]>()
  const constNodes = new Set<string>()

  const noteDriver = (bit: Bit, endpoint: Endpoint) => {
    if (isConstBit(bit)) {
      constNodes.add(String(bit).toLowerCase())
      driverByBit.set(bitKey(bit), { node: `const:${String(bit).toLowerCase()}`, pin: String(bit).toLowerCase(), dir: "output" })
      return
    }
    const key = bitKey(bit)
    if (!driverByBit.has(key)) driverByBit.set(key, endpoint)
  }
  const noteLoad = (bit: Bit, endpoint: Endpoint) => {
    const key = bitKey(bit)
    const arr = loadsByBit.get(key) ?? []
    arr.push(endpoint)
    loadsByBit.set(key, arr)
  }

  // input ports drive their nets
  for (const [name, p] of inputPorts) {
    for (const bit of p.bits ?? []) {
      noteDriver(bit, { node: `port:${name}`, pin: name, dir: "output" })
    }
  }
  // output ports consume their nets
  for (const [name, p] of outputPorts) {
    for (const bit of p.bits ?? []) {
      noteLoad(bit, { node: `port:${name}`, pin: name, dir: "input" })
      if (p.direction === "inout") noteDriver(bit, { node: `port:${name}`, pin: name, dir: "inout" })
    }
  }

  // cells: figure out input/output ports of the cell
  const cellNets: Record<string, { in: string[]; out: string[] }> = {}
  for (const [inst, cell] of Object.entries(cells)) {
    const ins: string[] = []
    const outs: string[] = []
    const dirs = cell.port_directions ?? {}
    for (const [pn, bits] of Object.entries(cell.connections ?? {})) {
      const dir = dirs[pn] ?? "input"
      for (const bit of bits ?? []) {
        const key = bitKey(bit)
        if (dir === "input" || dir === "inout") {
          ins.push(key)
          noteLoad(bit, { node: `cell:${inst}`, pin: pn, dir })
        }
        if (dir === "output" || dir === "inout") {
          outs.push(key)
          noteDriver(bit, { node: `cell:${inst}`, pin: pn, dir })
        }
      }
    }
    cellNets[inst] = { in: ins, out: outs }
  }

  // topological depth (longest path from any input)
  const depth = new Map<string, number>()
  const cellList = Object.keys(cells)
  const visited = new Set<string>()
  const visiting = new Set<string>()
  const computeDepth = (inst: string): number => {
    if (depth.has(inst)) return depth.get(inst)!
    if (visiting.has(inst)) return 0
    visiting.add(inst)
    let max = 0
    const ins = cellNets[inst]?.in ?? []
    for (const bit of ins) {
      const drv = driverByBit.get(bit)
      if (drv?.node.startsWith("cell:")) {
        const d = computeDepth(drv.node.slice(5)) + 1
        if (d > max) max = d
      }
    }
    visiting.delete(inst)
    visited.add(inst)
    depth.set(inst, max)
    return max
  }
  for (const inst of cellList) computeDepth(inst)

  const maxDepth = cellList.reduce((m, inst) => Math.max(m, depth.get(inst) ?? 0), 0)
  const columns = maxDepth + 3 // inputs(0) ... cells(1..maxDepth+1) outputs(maxDepth+2)

  // assign rows within columns
  const byCol: Record<number, LayoutNode[]> = {}
  const place = (node: LayoutNode) => {
    const col = node.column
    ;(byCol[col] ??= []).push(node)
  }

  for (const [name, p] of inputPorts) {
    place({
      id: `port:${name}`,
      kind: "port",
      label: name,
      direction: p.direction,
      width: p.bits.length,
      column: 0,
      row: 0,
      x: 0,
      y: 0,
      w: PORT_W,
      h: CELL_H,
      nets: [{ name: busLabel(name, p.bits ?? [], bitNames), bits: p.bits, dir: p.direction }],
    })
  }
  for (const value of [...constNodes].sort()) {
    place({
      id: `const:${value}`,
      kind: "const",
      label: `1'b${value}`,
      direction: "const",
      width: 1,
      column: 0,
      row: 0,
      x: 0,
      y: 0,
      w: PORT_W,
      h: CELL_H,
      nets: [{ name: `1'b${value}`, bits: [value], dir: "output" }],
    })
  }
  for (const [inst, cell] of Object.entries(cells)) {
    const col = (depth.get(inst) ?? 0) + 1
    const nets = Object.entries(cell.connections ?? {}).map(([pn, bits]) => ({
      name: pn,
      bits,
      dir: cell.port_directions?.[pn] ?? "input",
    }))
    place({
      id: `cell:${inst}`,
      kind: "cell",
      label: inst,
      sub: cell.type,
      column: col,
      row: 0,
      x: 0,
      y: 0,
      w: CELL_W,
      h: CELL_H,
      nets,
    })
  }
  for (const [name, p] of outputPorts) {
    place({
      id: `port:${name}`,
      kind: "port",
      label: name,
      direction: p.direction,
      width: p.bits.length,
      column: columns - 1,
      row: 0,
      x: 0,
      y: 0,
      w: PORT_W,
      h: CELL_H,
      nets: [{ name: busLabel(name, p.bits ?? [], bitNames), bits: p.bits, dir: p.direction }],
    })
  }

  // compute x,y from column/row
  const colWidths: number[] = []
  for (let c = 0; c < columns; c++) {
    const items = byCol[c] ?? []
    colWidths.push(items.reduce((m, n) => Math.max(m, n.w), 0))
  }
  const colX: number[] = [0]
  for (let c = 1; c < columns; c++) colX.push(colX[c - 1] + colWidths[c - 1] + COL_GAP)

  const nodes: LayoutNode[] = []
  for (let c = 0; c < columns; c++) {
    const items = byCol[c] ?? []
    let y = 0
    for (const n of items) {
      n.x = colX[c]
      n.y = y
      n.row = y
      nodes.push(n)
      y += n.h + ROW_GAP
    }
  }

  // Build grouped bit-accurate edges. Each edge represents one real
  // driver/load pin relation and carries the exact Yosys bits it contains.
  const edgeGroups = new Map<string, LayoutEdge>()
  for (const [bit, drv] of driverByBit) {
    const loads = loadsByBit.get(bit) ?? []
    for (const load of loads) {
      if (!drv || drv.node === load.node) continue
      const groupKey = `${drv.node}|${load.node}|${drv.pin}|${load.pin}`
      let edge = edgeGroups.get(groupKey)
      if (!edge) {
        edge = {
          id: groupKey,
          from: drv.node,
          to: load.node,
          fromPin: drv.pin,
          toPin: load.pin,
          bits: [],
          netNames: [],
          width: 0,
          lane: 0,
        }
        edgeGroups.set(groupKey, edge)
      }
      edge.bits.push(bit)
      const name = bitLabel(bit, bitNames)
      if (!edge.netNames.includes(name)) edge.netNames.push(name)
    }
  }
  const pairLane = new Map<string, number>()
  const edges = [...edgeGroups.values()].map((edge) => {
    edge.width = edge.bits.length
    const pairKey = `${edge.from}|${edge.to}`
    const lane = pairLane.get(pairKey) ?? 0
    pairLane.set(pairKey, lane + 1)
    edge.lane = lane
    return edge
  })

  return {
    nodes,
    edges,
    columns: colX[columns - 1] + colWidths[columns - 1] + 40,
    stats: { bitNets: driverByBit.size, edges: edges.length },
  }
}

export function SchematicViewer(props: { file: string }) {
  const { params } = useSessionLayout()
  const [hover, setHover] = createSignal<{ node: LayoutNode; x: number; y: number } | null>(null)
  const [view, setView] = createSignal({ tx: 20, ty: 20, scale: 1 })
  const containerRef: { current: HTMLDivElement | undefined } = { current: undefined }
  let dragging = false
  let lastX = 0
  let lastY = 0

  const [resp] = createResource(
    () => props.file,
    async (file) => {
      const res = await callAgenticTool(
        "workspace",
        { session_id: params.id!, workspace_root: decode64(params.dir) ?? "" },
        { action: "schematic_json", path: file },
      )
      if (!res.success) return { available: false, reason: res.result || "request failed" } as SchematicResponse
      try {
        return JSON.parse(res.result) as SchematicResponse
      } catch {
        return { available: false, reason: "invalid response" } as SchematicResponse
      }
    },
  )

  const layout = createMemo(() => {
    const r = resp()
    if (!r || !r.available || !r.yosys_json || !r.module) return null
    return buildLayout(r)
  })

  const health = createMemo(() => resp()?.netlist_summary?.health)

  const nodeById = createMemo(() => {
    const l = layout()
    if (!l) return new Map<string, LayoutNode>()
    return new Map(l.nodes.map((n) => [n.id, n]))
  })

  const visibleNodes = createMemo(() => {
    const l = layout()
    const el = containerRef.current
    if (!l || !el) return l?.nodes ?? []
    if (l.nodes.length < 800) return l.nodes
    const cur = view()
    const margin = 240 / cur.scale
    const left = (-cur.tx / cur.scale) - margin
    const top = (-cur.ty / cur.scale) - margin
    const right = ((el.clientWidth - cur.tx) / cur.scale) + margin
    const bottom = ((el.clientHeight - cur.ty) / cur.scale) + margin
    return l.nodes.filter((n) =>
      n.x + n.w >= left &&
      n.x <= right &&
      n.y + n.h >= top &&
      n.y <= bottom
    )
  })

  const visibleNodeIds = createMemo(() => new Set(visibleNodes().map((n) => n.id)))

  const visibleEdges = createMemo(() => {
    const l = layout()
    if (!l) return []
    if (l.nodes.length < 800) return l.edges
    const ids = visibleNodeIds()
    return l.edges.filter((edge) => ids.has(edge.from) && ids.has(edge.to))
  })

  const bounds = createMemo(() => {
    const l = layout()
    if (!l || l.nodes.length === 0) return { w: 400, h: 300 }
    let w = 0
    let h = 0
    for (const n of l.nodes) {
      w = Math.max(w, n.x + n.w)
      h = Math.max(h, n.y + n.h)
    }
    return { w: w + 40, h: h + 40 }
  })

  const fitView = () => {
    const el = containerRef.current
    const b = bounds()
    if (!el) return
    const cw = el.clientWidth
    const ch = el.clientHeight
    if (cw <= 0 || ch <= 0 || b.w <= 0 || b.h <= 0) return
    const scale = Math.min(cw / b.w, ch / b.h) * 0.92
    const s = Math.min(2.5, Math.max(0.15, scale))
    const tx = (cw - b.w * s) / 2
    const ty = (ch - b.h * s) / 2
    setView({ tx, ty, scale: s })
  }

  createEffect(() => {
    const l = layout()
    if (!l) return
    queueMicrotask(fitView)
  })

  const onWheel = (e: WheelEvent) => {
    e.preventDefault()
    const cur = view()
    const delta = -e.deltaY * 0.0015
    const next = Math.min(2.5, Math.max(0.15, cur.scale * (1 + delta)))
    const rect = containerRef.current?.getBoundingClientRect()
    if (rect) {
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      const wx = (px - cur.tx) / cur.scale
      const wy = (py - cur.ty) / cur.scale
      setView({ tx: px - wx * next, ty: py - wy * next, scale: next })
    } else {
      setView({ ...cur, scale: next })
    }
  }

  const onPointerDown = (e: PointerEvent) => {
    dragging = true
    lastX = e.clientX
    lastY = e.clientY
    ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
  }
  const onPointerMove = (e: PointerEvent) => {
    if (!dragging) return
    const cur = view()
    setView({ ...cur, tx: cur.tx + (e.clientX - lastX), ty: cur.ty + (e.clientY - lastY) })
    lastX = e.clientX
    lastY = e.clientY
  }
  const onPointerUp = (e: PointerEvent) => {
    dragging = false
    ;(e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId)
  }

  const zoomBy = (factor: number) => {
    const cur = view()
    const next = Math.min(2.5, Math.max(0.15, cur.scale * factor))
    if (next === cur.scale) return
    const rect = containerRef.current?.getBoundingClientRect()
    if (!rect) {
      setView({ ...cur, scale: next })
      return
    }
    const px = rect.width / 2
    const py = rect.height / 2
    const wx = (px - cur.tx) / cur.scale
    const wy = (py - cur.ty) / cur.scale
    setView({ tx: px - wx * next, ty: py - wy * next, scale: next })
  }

  createEffect(() => {
    const l = layout()
    if (!l) return
    const el = containerRef.current
    if (!el) return
    el.addEventListener("wheel", onWheel, { passive: false })
    onCleanup(() => el.removeEventListener("wheel", onWheel))
  })

  return (
    <div class="flex-1 flex flex-col min-h-0 bg-background-stronger">
      <Show when={resp.loading}>
        <div class="p-3 text-12-regular text-text-weak font-mono">Synthesizing schematic with Yosys...</div>
      </Show>

      <Show when={!resp.loading && resp() && !resp()!.available}>
        <div class="flex-1 flex flex-col items-center justify-center gap-2 p-6 text-center">
          <Icon name="warning" class="text-text-weaker" />
          <div class="text-12-medium text-text-strong">Live schematic unavailable</div>
          <div class="text-11-regular text-text-weak max-w-72 font-mono break-all">
            {resp()!.reason}
          </div>
          <div class="text-10-regular text-text-weaker max-w-72">
            Check your Verilog file for syntax errors or verify that all instantiated sub-modules exist in the design directory.
          </div>
        </div>
      </Show>

      <Show when={!resp.loading && resp()?.available && resp()?.large_design}>
        <div class="flex-1 overflow-y-auto p-4">
          <div class="max-w-3xl mx-auto border border-border-weaker-base rounded-lg bg-background-strong p-4">
            <div class="flex items-start justify-between gap-4">
              <div>
                <div class="text-13-medium text-text-strong font-mono">{resp()!.module}</div>
                <div class="text-11-regular text-text-weaker mt-1">
                  {resp()!.cells?.toLocaleString()} cells · {resp()!.ports} ports · {resp()!.netlist_summary?.named_net_count?.toLocaleString()} named nets
                </div>
              </div>
              <div class="px-2 py-1 rounded border border-border-weaker-base text-10-medium text-text-weaker font-mono">
                Complex chip mode
              </div>
            </div>

            <div class="grid grid-cols-2 md:grid-cols-4 gap-2 mt-4">
              <For each={[
                ["Bits", health()?.bit_count ?? 0],
                ["Undriven", health()?.undriven_count ?? 0],
                ["Multi-driven", health()?.multidriven_count ?? 0],
                ["High fanout", health()?.high_fanout_count ?? 0],
              ]}>
                {(item) => (
                  <div class="border border-border-weaker-base rounded-md px-3 py-2 bg-background-stronger">
                    <div class="text-10-regular text-text-weaker uppercase tracking-wider">{item[0]}</div>
                    <div class="text-16-medium text-text-strong font-mono">{Number(item[1]).toLocaleString()}</div>
                  </div>
                )}
              </For>
            </div>

            <Show when={health()?.high_fanout_preview?.length}>
              <div class="mt-4">
                <div class="text-11-medium text-text-strong mb-2">High Fanout Preview</div>
                <div class="space-y-1">
                  <For each={health()!.high_fanout_preview!.slice(0, 8)}>
                    {(item) => (
                      <div class="flex items-center justify-between gap-3 text-11-regular font-mono border border-border-weaker-base rounded px-2 py-1">
                        <span class="truncate text-text-base">bit {item.bit}</span>
                        <span class="text-text-weaker shrink-0">fanout {item.fanout} · {item.driver}</span>
                      </div>
                    )}
                  </For>
                </div>
              </div>
            </Show>

            <Show when={health()?.undriven_preview?.length || health()?.multidriven_preview?.length}>
              <div class="mt-4">
                <div class="text-11-medium text-text-strong mb-2">Connectivity Issues</div>
                <div class="space-y-1">
                  <For each={(health()?.undriven_preview ?? []).slice(0, 5)}>
                    {(item) => (
                      <div class="text-11-regular font-mono border border-red-500/30 rounded px-2 py-1 text-red-300 bg-red-500/5">
                        undriven bit {item.bit} · {item.load_count} load(s)
                      </div>
                    )}
                  </For>
                  <For each={(health()?.multidriven_preview ?? []).slice(0, 5)}>
                    {(item) => (
                      <div class="text-11-regular font-mono border border-yellow-500/30 rounded px-2 py-1 text-yellow-200 bg-yellow-500/5">
                        multi-driven bit {item.bit} · {item.driver_count} driver(s)
                      </div>
                    )}
                  </For>
                </div>
              </div>
            </Show>

            <div class="mt-4 text-11-regular text-text-weaker leading-relaxed">
              Full schematic JSON is kept locally at <span class="font-mono text-text-base">{resp()!.cache_ref}</span>. AgentIC returned this compact health/index view to avoid freezing the browser or wasting model context on a large chip.
            </div>
          </div>
        </div>
      </Show>

      <Show when={layout()}>
        <div class="flex items-center justify-between px-4 py-2 border-b border-border-weaker-base shrink-0">
          <div class="flex items-baseline gap-2.5 min-w-0">
            <span class="text-13-medium text-text-strong font-mono truncate">{resp()!.module}</span>
            <span class="text-11-regular text-text-weaker shrink-0">
              {resp()!.cells} cells · {resp()!.ports} ports · {layout()!.stats.bitNets} nets · {layout()!.stats.edges} edges
            </span>
          </div>
          <div class="flex items-center gap-1 text-text-weaker">
            <button
              class="w-6 h-6 flex items-center justify-center border border-border-weaker-base rounded hover:bg-surface-base text-text-base"
              onClick={() => zoomBy(1 / 1.2)}
              aria-label="Zoom out"
            >
              −
            </button>
            <span class="font-mono w-10 text-center text-11-regular">{Math.round(view().scale * 100)}%</span>
            <button
              class="w-6 h-6 flex items-center justify-center border border-border-weaker-base rounded hover:bg-surface-base text-text-base"
              onClick={() => zoomBy(1.2)}
              aria-label="Zoom in"
            >
              +
            </button>
            <button
              class="px-1.5 h-6 flex items-center border border-border-weaker-base rounded hover:bg-surface-base text-text-base ml-1"
              onClick={fitView}
            >
              Fit
            </button>
          </div>
        </div>
        <div
          ref={(el) => (containerRef.current = el)}
          class="flex-1 min-h-0 overflow-hidden relative cursor-grab active:cursor-grabbing"
          style={{ "touch-action": "none" }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={onPointerUp}
          onMouseMove={(e) => {
            const h = hover()
            if (h) setHover({ ...h, x: e.clientX, y: e.clientY })
          }}
        >
          <svg
            width="100%"
            height="100%"
            style={{ overflow: "visible" }}
          >
            <defs>
              <pattern id="eda-grid" width="24" height="24" patternUnits="userSpaceOnUse">
                <circle cx="1.5" cy="1.5" r="1" fill="rgba(255, 255, 255, 0.05)" />
              </pattern>
            </defs>
            <rect width="100%" height="100%" fill="url(#eda-grid)" />
            <g transform={`translate(${view().tx} ${view().ty}) scale(${view().scale})`}>
              <For each={visibleEdges()}>

                {(edge) => {
                  const from = nodeById().get(edge.from)
                  const to = nodeById().get(edge.to)
                  if (!from || !to) return null
                  const forward = from.x <= to.x
                  const x1 = forward ? from.x + from.w : from.x
                  const y1 = from.y + from.h / 2
                  const x2 = forward ? to.x : to.x + to.w
                  const y2 = to.y + to.h / 2
                  const mx = (x1 + x2) / 2
                  const laneOffset = (edge.lane % 7 - 3) * 4
                  const displayName = edge.netNames.length === 1
                    ? edge.netNames[0]
                    : edge.width > 1
                      ? `${edge.netNames[0]} +${edge.width - 1}`
                      : edge.netNames[0]
                  
                  const isHovered = () => {
                    const h = hover()
                    if (!h) return false
                    return h.node.id === edge.from || h.node.id === edge.to
                  }

                  return (
                    <g>
                    <path
                      d={`M ${x1} ${y1 + laneOffset} C ${mx} ${y1 + laneOffset}, ${mx} ${y2 + laneOffset}, ${x2} ${y2 + laneOffset}`}
                      fill="none"
                      stroke={isHovered() ? "var(--text-interactive-base, #9dbefe)" : "var(--border-base, #3e3e42)"}
                      stroke-width={isHovered() ? Math.min(4, 1.8 + edge.width * 0.2) : Math.min(3, 1 + Math.log2(edge.width))}
                      stroke-opacity={hover() ? (isHovered() ? "1" : "0.15") : "0.5"}
                      style={{ transition: "stroke 0.2s, stroke-width 0.2s, stroke-opacity 0.2s" }}
                      vector-effect="non-scaling-stroke"
                    />
                    <Show when={edge.width > 1 && (isHovered() || view().scale > 0.85)}>
                      <text
                        x={mx}
                        y={(y1 + y2) / 2 + laneOffset - 3}
                        text-anchor="middle"
                        fill="var(--text-weaker, #707076)"
                        style={{ "font-size": "9px", "font-family": "var(--font-family-mono)", "paint-order": "stroke", stroke: "var(--background-stronger, #111)", "stroke-width": "3px" }}
                      >
                        {edge.width}b
                      </text>
                    </Show>
                    <title>
                      {`${edge.fromPin} -> ${edge.toPin} · ${edge.width} bit${edge.width > 1 ? "s" : ""} · ${displayName}`}
                    </title>
                    </g>
                  )
                }}
              </For>
              <For each={visibleNodes()}>
                {(node) => {
                  const isPort = node.kind === "port"
                  const isConst = node.kind === "const"
                  const isHovered = () => hover()?.node.id === node.id
                  const isAnyHovered = () => hover() !== null
                  
                  const accent = isConst
                    ? "var(--text-weaker, #707076)"
                    : node.direction === "input"
                    ? "var(--surface-success-strong, #12c905)"
                    : node.direction === "output"
                      ? "var(--border-interactive-active, #034cff)"
                      : node.direction === "inout"
                        ? "var(--surface-warning-strong, #fbdd46)"
                        : "var(--text-interactive-base, #9dbefe)"

                  const accentX = node.direction === "output" ? node.w - 3 : 0
                  
                  return (
                    <g
                      transform={`translate(${node.x}, ${node.y})`}
                      onMouseEnter={(e: MouseEvent) => setHover({ node, x: e.clientX, y: e.clientY })}
                      onMouseLeave={() => setHover(null)}
                      style={{ 
                        cursor: "pointer", 
                        opacity: isAnyHovered() ? (isHovered() ? "1" : "0.4") : "1",
                        transition: "opacity 0.2s"
                      }}
                    >
                      <rect
                        width={node.w}
                        height={node.h}
                        rx="8"
                        fill={isPort || isConst ? "var(--surface-weak, rgba(25, 25, 28, 0.75))" : "var(--surface-base, rgba(20, 20, 23, 0.85))"}
                        stroke={isHovered() ? "var(--text-interactive-base, #9dbefe)" : "var(--border-weaker-base, #2b2b2f)"}
                        stroke-width={isHovered() ? "1.5" : "1"}
                        style={{ transition: "stroke 0.2s, stroke-width 0.2s" }}
                      />
                      <rect
                        x={accentX}
                        width={3}
                        height={node.h}
                        rx={1.5}
                        fill={accent}
                      />
                      <text
                        x="12"
                        y={node.h / 2 + 4}
                        fill={isHovered() ? "var(--text-strong, #ffffff)" : "var(--text-weak, #d1d1d6)"}
                        style={{ "font-size": "11px", "font-family": "var(--font-family-mono)", transition: "fill 0.2s" }}
                      >
                        {node.label.length > 20 ? node.label.slice(0, 19) + "…" : node.label}
                      </text>
                      <Show when={node.sub}>
                        <text
                          x={node.w - 12}
                          y={node.h / 2 + 4}
                          text-anchor="end"
                          fill="var(--text-weaker, #707076)"
                          style={{ "font-size": "10px", "font-family": "var(--font-family-mono)" }}
                        >
                          {node.sub!.length > 14 ? node.sub!.slice(0, 13) + "…" : node.sub}
                        </text>
                      </Show>
                      <Show when={isPort && node.width && node.width > 1}>
                        <text
                          x={node.w - 12}
                          y={node.h / 2 + 4}
                          text-anchor="end"
                          fill="var(--text-interactive-base, #9dbefe)"
                          style={{ "font-size": "10px", "font-family": "var(--font-family-mono)" }}
                        >
                          [{node.width}]
                        </text>
                      </Show>
                    </g>
                  )
                }}
              </For>
            </g>
          </svg>
 
          <Show when={hover()}>
            {(h) => (
              <div
                class="fixed z-50 pointer-events-none px-3.5 py-2.5 rounded-xl border shadow-2xl text-11-regular backdrop-blur-md"
                style={{
                  left: `${Math.min(h().x + 14, (containerRef.current?.clientWidth ?? 0) - 240)}px`,
                  top: `${h().y + 14}px`,
                  "background-color": "rgba(22, 22, 26, 0.92)",
                  "border-color": "var(--border-weaker-base, #2f2f33)",
                  "max-width": "260px",
                  "box-shadow": "0 10px 30px -10px rgba(0, 0, 0, 0.7)",
                }}
              >
                <div class="text-text-strong font-mono font-semibold mb-1 text-12-medium">{h().node.label}</div>
                <Show when={h().node.sub}>
                  <div class="text-text-interactive-base font-mono text-10-regular">type · {h().node.sub}</div>
                </Show>
                <Show when={h().node.direction}>
                  <div class="text-text-base font-mono text-10-regular">
                    {h().node.direction} · {h().node.width} bit{h().node.width! > 1 ? "s" : ""}
                  </div>
                </Show>
                <Show when={h().node.nets.length > 0}>
                  <div class="mt-1.5 pt-1.5 border-t border-border-weaker-base text-text-weaker font-mono text-10-regular">
                    <div class="mb-1">
                      {h().node.nets.length} pin{h().node.nets.length > 1 ? "s" : ""}
                    </div>
                    <For each={h().node.nets.slice(0, 8)}>
                      {(pin) => (
                        <div class="flex items-center justify-between gap-3">
                          <span class="truncate text-text-base">{pin.name}</span>
                          <span class="shrink-0 text-text-weaker">{pin.dir} · {bitsPreview(pin.bits)}</span>
                        </div>
                      )}
                    </For>
                    <Show when={h().node.nets.length > 8}>
                      <div class="text-text-weaker">+{h().node.nets.length - 8} more</div>
                    </Show>
                  </div>
                </Show>
              </div>
            )}
          </Show>
        </div>

      </Show>
    </div>
  )
}

export function SchematicTabContent(props: { tab: string }) {
  const path = createMemo(() => pathFromSchematicTab(props.tab))
  return (
    <Tabs.Content value={props.tab} class="flex flex-col h-full overflow-hidden contain-strict">
      <Show
        when={path()}
        fallback={<div class="p-6 text-12-regular text-text-weak">No file selected.</div>}
      >
        {(p) => <SchematicViewer file={p()} />}
      </Show>
    </Tabs.Content>
  )
}
