import { createResource, createSignal, Show, For, onMount, onCleanup, createMemo } from "solid-js"
import { Icon } from "@opencode-ai/ui/icon"
import { useSessionLayout } from "@/pages/session/session-layout"
import { callAgenticTool, getAgenticBase } from "@/utils/agentic"
import { decode64 } from "@/utils/base64"
import { parseGds, flattenCell, type GdsFile, type GdsPolygon } from "@/utils/gds-parser"

// PDK layer color presets (layer number → color)
const LAYER_COLORS: Record<number, string> = {
  0: "#8b8b8b",   // boundary
  1: "#a9d18e",   // metal 1
  2: "#ffd966",   // metal 2
  3: "#f4b183",   // metal 3
  4: "#9dc3e6",   // metal 4
  5: "#b4a7d6",   // metal 5
  10: "#e06666",  // nwell/diffusion
  11: "#6d9eeb",  // pwell
  20: "#76a5af",  // via 1
  21: "#76a5af",  // via 2
  22: "#76a5af",  // via 3
  23: "#76a5af",  // via 4
  30: "#cccccc",  // text/drawing
  31: "#cccccc",
  36: "#d5a6bd",  // ntap
  37: "#d5a6bd",  // ptap
  40: "#ffe599",  // li1
  41: "#ffe599",
  42: "#b6d7a8",  // li1 via
  43: "#b6d7a8",
  49: "#e06666",  // tap
  50: "#a4c2f4",  // tap via
  59: "#d9ead3",  // met1 pin
  65: "#b4a7d6",  // met2 pin
  66: "#b4a7d6",
  67: "#b4a7d6",
  68: "#b4a7d6",
  69: "#b4a7d6",
  70: "#4a86e8",  // via 2
  71: "#4a86e8",
  72: "#4a86e8",
}

function layerColor(layer: number): string {
  return LAYER_COLORS[layer] || `hsl(${(layer * 37) % 360}, 60%, 65%)`
}

export function GdsLayoutTab(props: { path: string }) {
  const { params } = useSessionLayout()
  const [view, setView] = createSignal({ tx: 0, ty: 0, scale: 1 })
  const [hoverInfo, setHoverInfo] = createSignal<string | null>(null)
  const [visibleLayers, setVisibleLayers] = createSignal<Set<number>>(new Set())
  const [showHierarchy, setShowHierarchy] = createSignal(false)
  let canvasRef: HTMLCanvasElement | undefined
  let containerRef: HTMLDivElement | undefined
  let dragging = false
  let lastX = 0
  let lastY = 0

  const [data] = createResource(
    () => props.path,
    async (gdsPath) => {
      const res = await callAgenticTool(
        "workspace",
        { session_id: params.id!, workspace_root: decode64(params.dir) ?? "" },
        { action: "read", path: gdsPath },
      )
      if (!res.success) return null
      // The result is the file content as text — but GDS is binary.
      // We need to fetch it as a blob.
      const base = getAgenticBase()
      const dir = decode64(params.dir) ?? ""
      const response = await fetch(`${base}/opencode/file?directory=${encodeURIComponent(dir)}&path=${encodeURIComponent(gdsPath)}`)
      if (!response.ok) return null
      const buffer = await response.arrayBuffer()
      const parsed = parseGds(buffer)
      return parsed
    },
  )

  const flattened = createMemo(() => {
    const gds = data()
    if (!gds || !gds.topCell) return [] as GdsPolygon[]
    return flattenCell(gds, gds.topCell)
  })

  const layers = createMemo(() => {
    const set = new Set<number>()
    for (const poly of flattened()) set.add(poly.layer)
    return [...set].sort((a, b) => a - b)
  })

  const cellCount = createMemo(() => data()?.cells.size ?? 0)
  const polyCount = createMemo(() => data()?.totalPolygons ?? 0)

  // Initialize visible layers when data loads
  createMemo(() => {
    const l = layers()
    if (l.length > 0 && visibleLayers().size === 0) {
      setVisibleLayers(new Set(l))
    }
  })

  const fitView = () => {
    const gds = data()
    const canvas = canvasRef
    if (!gds || !canvas || !gds.topCell) return
    const cell = gds.cells.get(gds.topCell)
    if (!cell) return
    const b = cell.bbox
    if (b.minX === Infinity) return
    const w = b.maxX - b.minX
    const h = b.maxY - b.minY
    if (w <= 0 || h <= 0) return
    const scaleX = canvas.width / w
    const scaleY = canvas.height / h
    const scale = Math.min(scaleX, scaleY) * 0.9
    const tx = (canvas.width - w * scale) / 2 - b.minX * scale
    const ty = (canvas.height - h * scale) / 2 - b.minY * scale
    setView({ tx, ty, scale })
  }

  const render = () => {
    const canvas = canvasRef
    if (!canvas) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    const polys = flattened()
    const v = view()
    const vis = visibleLayers()
    const dpr = window.devicePixelRatio || 1

    canvas.width = canvas.clientWidth * dpr
    canvas.height = canvas.clientHeight * dpr
    ctx.scale(dpr, dpr)

    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.fillStyle = "#0d0d0d"
    ctx.fillRect(0, 0, canvas.width, canvas.height)

    if (polys.length === 0) return

    // Draw polygons by layer (bottom layers first)
    const byLayer = new Map<number, GdsPolygon[]>()
    for (const poly of polys) {
      if (!vis.has(poly.layer)) continue
      const arr = byLayer.get(poly.layer) ?? []
      arr.push(poly)
      byLayer.set(poly.layer, arr)
    }

    for (const [layer, layerPolys] of byLayer) {
      ctx.fillStyle = layerColor(layer)
      ctx.strokeStyle = layerColor(layer)
      ctx.lineWidth = 0.5
      ctx.beginPath()
      for (const poly of layerPolys) {
        const pts = poly.points
        if (pts.length < 4) continue
        ctx.moveTo(pts[0] * v.scale + v.tx, pts[1] * v.scale + v.ty)
        for (let i = 2; i < pts.length; i += 2) {
          ctx.lineTo(pts[i] * v.scale + v.tx, pts[i + 1] * v.scale + v.ty)
        }
        ctx.closePath()
      }
      ctx.fill()
      ctx.stroke()
    }
  }

  // Re-render on view/data changes
  createMemo(() => {
    view()
    flattened()
    visibleLayers()
    requestAnimationFrame(render)
  })

  onMount(() => {
    const canvas = canvasRef
    if (!canvas) return

    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const cur = view()
      const delta = -e.deltaY * 0.0015
      const next = Math.min(50, Math.max(0.01, cur.scale * (1 + delta)))
      const rect = canvas.getBoundingClientRect()
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      const wx = (px - cur.tx) / cur.scale
      const wy = (py - cur.ty) / cur.scale
      setView({ tx: px - wx * next, ty: py - wy * next, scale: next })
    }

    const onPointerDown = (e: PointerEvent) => {
      dragging = true
      lastX = e.clientX
      lastY = e.clientY
      canvas.setPointerCapture(e.pointerId)
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
      canvas.releasePointerCapture(e.pointerId)
    }

    canvas.addEventListener("wheel", onWheel, { passive: false })
    canvas.addEventListener("pointerdown", onPointerDown)
    canvas.addEventListener("pointermove", onPointerMove)
    canvas.addEventListener("pointerup", onPointerUp)

    onCleanup(() => {
      canvas.removeEventListener("wheel", onWheel)
      canvas.removeEventListener("pointerdown", onPointerDown)
      canvas.removeEventListener("pointermove", onPointerMove)
      canvas.removeEventListener("pointerup", onPointerUp)
    })
  })

  const handleSendToAgent = () => {
    const gds = data()
    if (!gds) return
    const summary = [
      `GDS layout: ${props.path}`,
      `${cellCount()} cells, ${polyCount()} polygons`,
      `Top cell: ${gds.topCell}`,
      `Layers: ${layers().join(", ")}`,
    ].join("\n")
    navigator.clipboard.writeText(summary)
  }

  const toggleLayer = (layer: number) => {
    const vis = new Set(visibleLayers())
    if (vis.has(layer)) vis.delete(layer)
    else vis.add(layer)
    setVisibleLayers(vis)
  }

  return (
    <div class="h-full flex bg-background-stronger overflow-hidden">
      {/* Canvas */}
      <div class="flex-1 relative min-w-0" ref={(el) => (containerRef = el)}>
        <Show when={data.loading}>
          <div class="absolute inset-0 flex items-center justify-center text-14-regular text-text-weak font-mono">
            Parsing GDS...
          </div>
        </Show>

        <Show when={!data.loading && !data()}>
          <div class="absolute inset-0 flex items-center justify-center text-14-regular text-text-weak">
            Failed to load GDS file.
          </div>
        </Show>

        <canvas
          ref={(el) => (canvasRef = el)}
          class="w-full h-full cursor-grab active:cursor-grabbing"
          style={{ "touch-action": "none" }}
        />

        {/* Top bar */}
        <Show when={data()}>
          <div class="absolute top-0 left-0 right-0 flex items-center justify-between px-4 py-2 bg-gradient-to-b from-black/60 to-transparent">
            <div class="flex items-center gap-3">
              <Icon name="file-tree" class="text-text-strong shrink-0" />
              <div>
                <div class="text-13-medium text-text-strong font-mono">{props.path.split("/").pop()}</div>
                <div class="text-11-regular text-text-weaker">
                  {cellCount()} cells · {polyCount()} polygons · {layers().length} layers
                </div>
              </div>
            </div>
            <div class="flex items-center gap-2">
              <button
                class="px-2 py-1 text-11-medium text-text-base rounded border border-border-weaker-base hover:bg-surface-base transition-colors"
                onClick={fitView}
              >
                Fit
              </button>
              <button
                class="px-2 py-1 text-11-medium text-text-base rounded border border-border-weaker-base hover:bg-surface-base transition-colors"
                onClick={() => setShowHierarchy(!showHierarchy())}
              >
                {showHierarchy() ? "Hide" : "Layers"}
              </button>
              <button
                class="inline-flex items-center gap-1.5 px-2.5 py-1 text-11-medium bg-surface-base hover:bg-surface-stronger text-text-strong rounded border border-border-weaker-base transition-colors"
                onClick={handleSendToAgent}
              >
                <Icon name="share" size="small" class="text-icon-base shrink-0" />
                Send to agent
              </button>
            </div>
          </div>

          {/* Zoom indicator */}
          <div class="absolute bottom-3 left-3 px-2 py-1 rounded bg-black/50 text-10-regular text-text-weaker font-mono">
            {Math.round(view().scale * 100)}%
          </div>
        </Show>
      </div>

      {/* Layer panel */}
      <Show when={data() && showHierarchy()}>
        <div class="w-48 shrink-0 border-l border-border-weaker-base bg-background-base overflow-y-auto">
          <div class="px-3 py-2 text-10-medium text-text-weaker uppercase tracking-wider border-b border-border-weaker-base">
            Layers ({layers().length})
          </div>
          <For each={layers()}>
            {(layer) => (
              <button
                class="w-full flex items-center gap-2 px-3 py-1.5 text-12-regular hover:bg-surface-base transition-colors"
                onClick={() => toggleLayer(layer)}
              >
                <span
                  class="w-4 h-4 rounded shrink-0 border border-border-weaker-base"
                  style={{ "background-color": layerColor(layer), opacity: visibleLayers().has(layer) ? "1" : "0.2" }}
                />
                <span class="font-mono text-text-base">L{layer}</span>
                <Show when={!visibleLayers().has(layer)}>
                  <span class="text-10-regular text-text-weaker ml-auto">hidden</span>
                </Show>
              </button>
            )}
          </For>

          <div class="px-3 py-2 mt-2 text-10-medium text-text-weaker uppercase tracking-wider border-b border-border-weaker-base">
            Cells ({cellCount()})
          </div>
          <For each={[...Array.from(data()?.cells.keys() ?? []).slice(0, 50)]}>
            {(name) => (
              <div class="px-3 py-1 text-11-regular text-text-base font-mono truncate">
                {name}
              </div>
            )}
          </For>
          <Show when={cellCount() > 50}>
            <div class="px-3 py-1 text-10-regular text-text-weaker">+{cellCount() - 50} more...</div>
          </Show>
        </div>
      </Show>
    </div>
  )
}
