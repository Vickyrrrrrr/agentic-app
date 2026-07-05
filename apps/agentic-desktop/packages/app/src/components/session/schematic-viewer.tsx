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
}
type YosysJson = { modules: Record<string, YosysModule> }

type SchematicResponse = {
  available: boolean
  reason?: string
  log?: string
  module?: string
  yosys_json?: YosysJson
  ports?: number
  cells?: number
}

type NodeKind = "port" | "cell"
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

const CELL_W = 160
const CELL_H = 36
const COL_GAP = 84
const ROW_GAP = 16
const PORT_W = 144

function bitKey(b: Bit): string {
  return String(b)
}

function netOfBits(bits: Bit[]): string {
  return bits.map(bitKey).join(",")
}

function buildLayout(resp: SchematicResponse) {
  const mod = resp.module!
  const ymod = resp.yosys_json!.modules[mod]
  if (!ymod) return { nodes: [] as LayoutNode[], edges: [] as { from: string; to: string }[], columns: 0 }

  const ports = ymod.ports ?? {}
  const cells = ymod.cells ?? {}

  const inputPorts = Object.entries(ports).filter(([, p]) => p.direction === "input")
  const outputPorts = Object.entries(ports).filter(([, p]) => p.direction !== "input")

  // net id -> driver node id
  const driverByNet = new Map<string, string>()
  const loadsByNet = new Map<string, string[]>()

  const noteDriver = (net: string, nodeId: string) => {
    if (!driverByNet.has(net)) driverByNet.set(net, nodeId)
  }
  const noteLoad = (net: string, nodeId: string) => {
    const arr = loadsByNet.get(net) ?? []
    arr.push(nodeId)
    loadsByNet.set(net, arr)
  }

  // input ports drive their nets
  for (const [name, p] of inputPorts) {
    const net = netOfBits(p.bits)
    noteDriver(net, `port:${name}`)
    noteLoad(net, `port:${name}`)
  }
  // output ports consume their nets
  for (const [name, p] of outputPorts) {
    const net = netOfBits(p.bits)
    noteLoad(net, `port:${name}`)
  }

  // cells: figure out input/output ports of the cell
  const cellNets: Record<string, { in: string[]; out: string[] }> = {}
  for (const [inst, cell] of Object.entries(cells)) {
    const ins: string[] = []
    const outs: string[] = []
    const dirs = cell.port_directions ?? {}
    for (const [pn, bits] of Object.entries(cell.connections ?? {})) {
      const net = netOfBits(bits)
      const dir = dirs[pn] ?? "input"
      if (dir === "input") {
        ins.push(net)
        noteLoad(net, `cell:${inst}`)
      } else {
        outs.push(net)
        noteDriver(net, `cell:${inst}`)
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
    for (const net of ins) {
      const drv = driverByNet.get(net)
      if (drv && drv.startsWith("cell:")) {
        const d = computeDepth(drv.slice(5)) + 1
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
      nets: [{ name, bits: p.bits, dir: p.direction }],
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
      nets: [{ name, bits: p.bits, dir: p.direction }],
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

  // build edges (driver -> load) for visible wires
  const edges: { from: string; to: string }[] = []
  for (const [net, drv] of driverByNet) {
    const loads = loadsByNet.get(net) ?? []
    for (const load of loads) {
      if (drv && drv !== load) edges.push({ from: drv, to: load })
    }
  }

  return { nodes, edges, columns: colX[columns - 1] + colWidths[columns - 1] + 40 }
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

      <Show when={layout()}>
        <div class="flex items-center justify-between px-4 py-2 border-b border-border-weaker-base shrink-0">
          <div class="flex items-baseline gap-2.5 min-w-0">
            <span class="text-13-medium text-text-strong font-mono truncate">{resp()!.module}</span>
            <span class="text-11-regular text-text-weaker shrink-0">
              {resp()!.cells} cells · {resp()!.ports} ports
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
                  const x1 = from.x + from.w
                  const y1 = from.y + from.h / 2
                  const x2 = to.x
                  const y2 = to.y + to.h / 2
                  const mx = (x1 + x2) / 2
                  
                  const isHovered = () => {
                    const h = hover()
                    if (!h) return false
                    return h.node.id === edge.from || h.node.id === edge.to
                  }

                  return (
                    <path
                      d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`}
                      fill="none"
                      stroke={isHovered() ? "var(--text-interactive-base, #9dbefe)" : "var(--border-base, #3e3e42)"}
                      stroke-width={isHovered() ? "1.8" : "1"}
                      stroke-opacity={hover() ? (isHovered() ? "1" : "0.15") : "0.5"}
                      style={{ transition: "stroke 0.2s, stroke-width 0.2s, stroke-opacity 0.2s" }}
                      vector-effect="non-scaling-stroke"
                    />
                  )
                }}
              </For>
              <For each={visibleNodes()}>
                {(node) => {
                  const isPort = node.kind === "port"
                  const isHovered = () => hover()?.node.id === node.id
                  const isAnyHovered = () => hover() !== null
                  
                  const accent = node.direction === "input"
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
                        fill={isPort ? "var(--surface-weak, rgba(25, 25, 28, 0.75))" : "var(--surface-base, rgba(20, 20, 23, 0.85))"}
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
                    {h().node.nets.length} pin{h().node.nets.length > 1 ? "s" : ""}
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
