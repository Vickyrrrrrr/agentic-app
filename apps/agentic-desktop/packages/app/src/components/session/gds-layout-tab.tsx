import { createResource, createSignal, Show, For, onMount, onCleanup, createMemo, createEffect } from "solid-js"
import { Icon } from "@opencode-ai/ui/icon"
import { useSessionLayout } from "@/pages/session/session-layout"
import { getAgenticBase } from "@/utils/agentic"
import { decode64 } from "@/utils/base64"
import { showToast } from "@/utils/toast"
import type { GdsWorkerResult } from "@/utils/gds-worker"

type BBox = { minX: number; minY: number; maxX: number; maxY: number }

// PDK layer color presets (Sky130)
const LAYER_COLORS: Record<number, string> = {
  0: "#8b8b8b", 1: "#a9d18e", 2: "#ffd966", 3: "#f4b183", 4: "#9dc3e6",
  5: "#b4a7d6", 10: "#e06666", 11: "#6d9eeb", 20: "#76a5af", 21: "#76a5af",
  22: "#76a5af", 23: "#76a5af", 30: "#cccccc", 31: "#cccccc", 36: "#d5a6bd",
  37: "#d5a6bd", 40: "#ffe599", 41: "#ffe599", 42: "#b6d7a8", 43: "#b6d7a8",
  49: "#e06666", 50: "#a4c2f4", 59: "#d9ead3", 65: "#b4a7d6", 66: "#b4a7d6",
  67: "#b4a7d6", 68: "#b4a7d6", 69: "#b4a7d6", 70: "#4a86e8", 71: "#4a86e8",
  72: "#4a86e8",
}

function layerColor(layer: number): string {
  return LAYER_COLORS[layer] || `hsl(${(layer * 37) % 360}, 60%, 65%)`
}

// Transform a polygon's points by an affine transform
function transformPoints(points: number[], t: number[]): number[] {
  const [a, b, c, d, tx, ty] = t
  const result: number[] = []
  for (let i = 0; i + 1 < points.length; i += 2) {
    result.push(a * points[i] + b * points[i + 1] + tx)
    result.push(c * points[i] + d * points[i + 1] + ty)
  }
  return result
}

function transformBbox(bbox: BBox, t: number[]): BBox {
  const out = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity }
  const corners = [
    [bbox.minX, bbox.minY],
    [bbox.maxX, bbox.minY],
    [bbox.maxX, bbox.maxY],
    [bbox.minX, bbox.maxY],
  ]
  for (const [x, y] of corners) {
    const tx = t[0] * x + t[1] * y + t[4]
    const ty = t[2] * x + t[3] * y + t[5]
    if (tx < out.minX) out.minX = tx
    if (ty < out.minY) out.minY = ty
    if (tx > out.maxX) out.maxX = tx
    if (ty > out.maxY) out.maxY = ty
  }
  return out
}

function isValidBbox(bbox: BBox | undefined): bbox is BBox {
  return !!bbox &&
    Number.isFinite(bbox.minX) &&
    Number.isFinite(bbox.minY) &&
    Number.isFinite(bbox.maxX) &&
    Number.isFinite(bbox.maxY) &&
    bbox.maxX >= bbox.minX &&
    bbox.maxY >= bbox.minY
}

function bboxIntersects(a: BBox, b: BBox) {
  return a.maxX >= b.minX && a.minX <= b.maxX && a.maxY >= b.minY && a.minY <= b.maxY
}

function isPointInPolygon(px: number, py: number, pts: number[]) {
  let inside = false
  for (let i = 0, j = pts.length - 2; i < pts.length; i += 2) {
    const xi = pts[i], yi = pts[i + 1]
    const xj = pts[j], yj = pts[j + 1]
    const intersect = ((yi > py) !== (yj > py)) &&
                      (px < (xj - xi) * (py - yi) / (yj - yi) + xi)
    if (intersect) inside = !inside
    j = i
  }
  return inside
}

function screenBbox(bbox: BBox, v: { offsetX: number; offsetY: number; scale: number }, cx: number, cy: number, w: number, h: number) {
  const x0 = (bbox.minX - cx) * v.scale + w / 2
  const x1 = (bbox.maxX - cx) * v.scale + w / 2
  const y0 = -(bbox.maxY - cy) * v.scale + h / 2
  const y1 = -(bbox.minY - cy) * v.scale + h / 2
  return {
    x: Math.min(x0, x1),
    y: Math.min(y0, y1),
    width: Math.abs(x1 - x0),
    height: Math.abs(y1 - y0),
  }
}

type BinnedInstances = { binCols: number; binRows: number; bins: number[][][] }

function getVisibleInstances(
  binst: BinnedInstances | undefined,
  bbox: BBox,
  viewport: BBox
): number[][] {
  if (!binst) return []
  const { binCols, binRows, bins } = binst
  const binW = Math.max(bbox.maxX - bbox.minX, 1) / binCols
  const binH = Math.max(bbox.maxY - bbox.minY, 1) / binRows

  const minCol = Math.max(0, Math.min(binCols - 1, Math.floor((viewport.minX - bbox.minX) / binW)))
  const maxCol = Math.max(0, Math.min(binCols - 1, Math.floor((viewport.maxX - bbox.minX) / binW)))
  const minRow = Math.max(0, Math.min(binRows - 1, Math.floor((viewport.minY - bbox.minY) / binH)))
  const maxRow = Math.max(0, Math.min(binRows - 1, Math.floor((viewport.maxY - bbox.minY) / binH)))

  const result: number[][] = []
  for (let r = minRow; r <= maxRow; r++) {
    for (let c = minCol; c <= maxCol; c++) {
      const idx = r * binCols + c
      const bin = bins[idx]
      if (bin) {
        for (let i = 0; i < bin.length; i++) {
          result.push(bin[i])
        }
      }
    }
  }
  return result
}

export function GdsLayoutTab(props: { path: string }) {
  const { params } = useSessionLayout()
  // View: offsetX, offsetY (screen px), scale (world→screen). Y is FLIPPED (GDS Y up → screen Y down)
  const [view, setView] = createSignal({ offsetX: 0, offsetY: 0, scale: 1 })
  const [visibleLayers, setVisibleLayers] = createSignal<Set<number>>(new Set())
  const [showLayers, setShowLayers] = createSignal(false)
  const [progress, setProgress] = createSignal<string | null>(null)
  const [renderedCount, setRenderedCount] = createSignal(0)
  const [hoverCoords, setHoverCoords] = createSignal<{ x: number; y: number } | null>(null)
  const [interacting, setInteracting] = createSignal(false)
  let wheelTimeout: any = null
  let canvasRef: HTMLCanvasElement | undefined
  let dragging = false
  let lastX = 0
  let lastY = 0
  let clickStartX = 0
  let clickStartY = 0
  let renderPending = false
  let didFit = false

  const [selectedObject, setSelectedObject] = createSignal<{ cellName: string; layer: number; points: number[]; pointsCount: number } | null>(null)
  const [selectedDRC, setSelectedDRC] = createSignal<{ rule: string; message: string; bbox: number[]; logLine?: number } | null>(null)
  const [layerOpacities, setLayerOpacities] = createSignal<Map<number, number>>(new Map())
  const [showDRC, setShowDRC] = createSignal(true)

  const [signoffData] = createResource(
    () => params.id,
    async (sessId) => {
      try {
        const response = await fetch(`${getAgenticBase()}/build/signoff/session/${encodeURIComponent(sessId)}`)
        if (!response.ok) return null
        return await response.json()
      } catch {
        return null
      }
    }
  )

  const layerOpacity = (layer: number) => {
    return layerOpacities().get(layer) ?? (layer >= 60 ? 0.72 : 0.52)
  }

  const updateLayerOpacity = (layer: number, val: number) => {
    const next = new Map(layerOpacities())
    next.set(layer, val)
    setLayerOpacities(next)
  }

  const [data] = createResource(
    () => props.path,
    async (gdsPath) => {
      const base = getAgenticBase()
      const dir = decode64(params.dir) ?? ""
      const response = await fetch(
        `${base}/opencode/binary-file?directory=${encodeURIComponent(dir)}&path=${encodeURIComponent(gdsPath)}`,
      )
      if (!response.ok) return null
      const buffer = await response.arrayBuffer()

      return new Promise<GdsWorkerResult>((resolve, reject) => {
        const worker = new Worker(new URL("../../utils/gds-worker.ts", import.meta.url), { type: "module" })
        worker.onmessage = (e: MessageEvent) => {
          const msg = e.data
          if (msg.type === "progress") {
            setProgress(`${msg.phase}... ${msg.polygonCount ? msg.polygonCount.toLocaleString() + " polygons" : ""}`)
          } else if (msg.type === "done") {
            worker.terminate()
            setProgress(null)
            resolve(msg.result as GdsWorkerResult)
          } else if (msg.type === "error") {
            worker.terminate()
            setProgress(null)
            reject(new Error(msg.message))
          }
        }
        worker.onerror = (e) => {
          worker.terminate()
          setProgress(null)
          reject(new Error(e.message))
        }
        worker.postMessage({ buffer }, [buffer])
      }).catch(() => null)
    },
  )

  const layers = createMemo(() => data()?.layers ?? [])
  const cellCount = createMemo(() => data()?.cellCount ?? 0)
  const polyCount = createMemo(() => data()?.polygonCount ?? 0)
  const cellsByName = createMemo(() => new Map(data()?.cells ?? []))
  const instancesByName = createMemo(() => new Map(data()?.instances ?? []))
  const cellBboxesByName = createMemo(() => new Map(data()?.cellBboxes ?? []))

  createEffect(() => {
    props.path
    didFit = false
  })

  const fitBbox = (bbox: BBox | undefined) => {
    const canvas = canvasRef
    if (!canvas || !isValidBbox(bbox)) return
    const w = Math.max(bbox.maxX - bbox.minX, 1)
    const h = Math.max(bbox.maxY - bbox.minY, 1)
    const pad = 0.10
    const availW = canvas.clientWidth * (1 - pad * 2)
    const availH = canvas.clientHeight * (1 - pad * 2)
    const scale = Math.min(availW / w, availH / h)
    const cx = (bbox.minX + bbox.maxX) / 2
    const cy = (bbox.minY + bbox.maxY) / 2
    setView({
      scale,
      offsetX: canvas.clientWidth / 2 - cx * scale,
      offsetY: canvas.clientHeight / 2 + cy * scale,
    })
  }

  // Initialize visible layers + fit view when data loads
  createEffect(() => {
    const d = data()
    if (!d || didFit) return
    didFit = true
    setSelectedObject(null)
    setSelectedDRC(null)
    setLayerOpacities(new Map())
    if (d.layers.length > 0) {
      setVisibleLayers(new Set(d.layers))
    }
    // Fit view after data loads — center the layout in the viewport
    fitBbox(d.bbox)
  })

  const fitView = () => fitBbox(data()?.bbox)

  const render = () => {
    if (renderPending) return
    renderPending = true
    requestAnimationFrame(() => {
      renderPending = false
      doRender()
    })
  }

  const doRender = () => {
    const canvas = canvasRef
    if (!canvas) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    const d = data()
    if (!d) return
    const v = view()
    const vis = visibleLayers()
    const dpr = window.devicePixelRatio || 1

    const cssW = canvas.clientWidth
    const cssH = canvas.clientHeight
    const targetW = Math.round(cssW * dpr)
    const targetH = Math.round(cssH * dpr)
    if (canvas.width !== targetW || canvas.height !== targetH) {
      canvas.width = targetW
      canvas.height = targetH
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    // Clear
    ctx.fillStyle = "#0d0d0d"
    ctx.fillRect(0, 0, cssW, cssH)

    // Center-relative offsets for GPU double-precision projection
    const scale = v.scale
    const cx_world = (cssW / 2 - v.offsetX) / scale
    const cy_world = -(cssH / 2 - v.offsetY) / scale

    // World coordinates of viewport (Y flipped: worldTop = +offsetY/scale, worldBottom = (offsetY - height)/scale)
    const worldLeft = -v.offsetX / v.scale
    const worldRight = (cssW - v.offsetX) / v.scale
    const worldTop = v.offsetY / v.scale
    const worldBottom = (v.offsetY - cssH) / v.scale
    const worldWidth = worldRight - worldLeft

    // Grid
    drawGrid(ctx, v, cssW, cssH, cx_world, cy_world)

    const viewport: BBox = {
      minX: Math.min(worldLeft, worldRight),
      minY: Math.min(worldBottom, worldTop),
      maxX: Math.max(worldLeft, worldRight),
      maxY: Math.max(worldBottom, worldTop),
    }
    const cellMap = cellsByName()
    const instanceMap = instancesByName()
    const cellBboxMap = cellBboxesByName()

    // LOD: when zoomed out, draw instance footprints rather than millions of tiny polygons.
    const dieW = d.bbox.maxX - d.bbox.minX
    const dieH = d.bbox.maxY - d.bbox.minY
    const zoomedOut = worldWidth > dieW / 4 || (worldTop - worldBottom) > dieH / 4
    const useLOD = zoomedOut && polyCount() > 5000

    const isInteracting = interacting()
    const maxPolys = isInteracting ? 20000 : 200000
    const maxLODInsts = isInteracting ? 50000 : 500000

    let drawn = 0
    const BATCH_FLUSH = 5000

    if (useLOD) {
      const b = d.bbox
      const dieScreen = screenBbox(b, v, cx_world, cy_world, cssW, cssH)
      ctx.strokeStyle = "rgba(120, 168, 224, 0.72)"
      ctx.lineWidth = 1.5
      ctx.strokeRect(dieScreen.x, dieScreen.y, dieScreen.width, dieScreen.height)

      // Sub-pixel grid culling to prevent redundant drawing calls on the same screen pixels
      const cols = Math.ceil(cssW / 2)
      const rows = Math.ceil(cssH / 2)
      const subpixelGrid = new Uint8Array(cols * rows)

      for (const [cellName, binst] of instanceMap) {
        const polys = cellMap.get(cellName)
        const localBbox = cellBboxMap.get(cellName)
        if (!polys || !localBbox) continue
        const insts = getVisibleInstances(binst as any, d.bbox, viewport)
        if (insts.length === 0) continue
        const visiblePoly = polys.find((poly) => vis.has(poly.layer))
        if (!visiblePoly) continue

        const color = layerColor(visiblePoly.layer)
        ctx.fillStyle = color
        ctx.strokeStyle = color
        ctx.globalAlpha = layerOpacity(visiblePoly.layer)

        for (const t of insts) {
          const worldBox = transformBbox(localBbox, t)
          if (!bboxIntersects(worldBox, viewport)) continue
          const s = screenBbox(worldBox, v, cx_world, cy_world, cssW, cssH)
          if (s.width < 2 && s.height < 2) {
            // Check grid culling
            const gx = Math.floor(s.x / 2)
            const gy = Math.floor(s.y / 2)
            if (gx >= 0 && gx < cols && gy >= 0 && gy < rows) {
              const idx = gy * cols + gx
              if (subpixelGrid[idx] === 1) continue // Skip duplicate sub-pixel draw
              subpixelGrid[idx] = 1
            }
            ctx.fillRect(s.x, s.y, 1.5, 1.5)
          } else if (s.width < 8 || s.height < 8) {
            ctx.fillRect(s.x, s.y, Math.max(1, s.width), Math.max(1, s.height))
          } else {
            ctx.globalAlpha = Math.min(layerOpacity(visiblePoly.layer), 0.22)
            ctx.fillRect(s.x, s.y, s.width, s.height)
            ctx.globalAlpha = layerOpacity(visiblePoly.layer)
            ctx.strokeRect(s.x, s.y, s.width, s.height)
          }
          drawn++
          if (drawn >= maxLODInsts) break
        }
        if (drawn >= maxLODInsts) break
      }
      ctx.globalAlpha = 1

      ctx.fillStyle = "rgba(255, 255, 255, 0.6)"
      ctx.font = "12px monospace"
      if (d.topCell) ctx.fillText(d.topCell, dieScreen.x + 8, dieScreen.y + 16)
    } else {
      // Pre-query visible cell instances once per frame
      const visibleCellInstances = new Map<string, number[][]>()
      for (const [cellName, binst] of instanceMap) {
        const insts = getVisibleInstances(binst as any, d.bbox, viewport)
        if (insts.length > 0) {
          visibleCellInstances.set(cellName, insts)
        }
      }

      const sortedLayers = [...vis].sort((a, b) => a - b)
      for (const layer of sortedLayers) {
        const color = layerColor(layer)
        ctx.fillStyle = color
        ctx.strokeStyle = color
        ctx.lineWidth = 0.5
        ctx.globalAlpha = layerOpacity(layer)
        ctx.beginPath()
        let batch = 0

        for (const [cellName, insts] of visibleCellInstances) {
          const polys = cellMap.get(cellName)
          if (!polys) continue
          
          for (const poly of polys) {
            if (poly.layer !== layer) continue
            
            for (const t of insts) {
              if (drawn >= maxPolys) break
              const pts = poly.points
              if (pts.length < 4) continue

              const [a, b, c, d, tx, ty] = t
              ctx.moveTo(
                (a * pts[0] + b * pts[1] + tx - cx_world) * scale + cssW / 2,
                -((c * pts[0] + d * pts[1] + ty - cy_world) * scale) + cssH / 2
              )
              for (let i = 2; i < pts.length; i += 2) {
                ctx.lineTo(
                  (a * pts[i] + b * pts[i + 1] + tx - cx_world) * scale + cssW / 2,
                  -((c * pts[i] + d * pts[i + 1] + ty - cy_world) * scale) + cssH / 2
                )
              }
              ctx.closePath()
              drawn++
              batch++

              if (batch >= BATCH_FLUSH) {
                ctx.fill()
                ctx.beginPath()
                batch = 0
              }
            }
            if (drawn >= maxPolys) break
          }
          if (drawn >= maxPolys) break
        }
        ctx.fill()
        if (drawn >= maxPolys) break
      }
      ctx.globalAlpha = 1.0
    }

    // Highlight selected object
    const sel = selectedObject()
    if (sel && sel.points) {
      ctx.strokeStyle = "#ff3b30"
      ctx.lineWidth = 2.5
      ctx.globalAlpha = 1.0
      ctx.beginPath()
      const pts = sel.points
      ctx.moveTo(
        (pts[0] - cx_world) * scale + cssW / 2,
        -((pts[1] - cy_world) * scale) + cssH / 2
      )
      for (let i = 2; i < pts.length; i += 2) {
        ctx.lineTo(
          (pts[i] - cx_world) * scale + cssW / 2,
          -((pts[i + 1] - cy_world) * scale) + cssH / 2
        )
      }
      ctx.closePath()
      ctx.stroke()
      ctx.fillStyle = "rgba(255, 59, 48, 0.25)"
      ctx.fill()
    }

    // Draw DRC violations overlay
    const userUnit = d.userUnit ?? 1
    if (showDRC() && signoffData()?.drc?.diagnostics) {
      for (const diag of signoffData().drc.diagnostics) {
        if (!diag.bbox) continue
        const [x0, y0, x1, y1] = diag.bbox
        const dbX0 = x0 / userUnit
        const dbY0 = y0 / userUnit
        const dbX1 = x1 / userUnit
        const dbY1 = y1 / userUnit
        
        const s = screenBbox(
          { minX: dbX0, minY: dbY0, maxX: dbX1, maxY: dbY1 },
          v,
          cx_world,
          cy_world,
          cssW,
          cssH
        )
        
        // Draw red dashed bounding box
        ctx.strokeStyle = "#ff453a"
        ctx.lineWidth = 2.0
        ctx.setLineDash([4, 4])
        ctx.strokeRect(s.x - 2, s.y - 2, Math.max(s.width + 4, 8), Math.max(s.height + 4, 8))
        ctx.setLineDash([]) // Reset
        
        ctx.fillStyle = "rgba(255, 69, 58, 0.2)"
        ctx.fillRect(s.x - 2, s.y - 2, Math.max(s.width + 4, 8), Math.max(s.height + 4, 8))

        // Draw warning marker circle
        const mx = s.x + s.width / 2
        const my = s.y + s.height / 2
        ctx.fillStyle = "#ff453a"
        ctx.beginPath()
        ctx.arc(mx, my, 5, 0, 2 * Math.PI)
        ctx.fill()
        ctx.strokeStyle = "#ffffff"
        ctx.lineWidth = 1.0
        ctx.stroke()
      }
    }

    // Highlight selected DRC violation
    const selDrc = selectedDRC()
    if (selDrc && selDrc.bbox) {
      const [x0, y0, x1, y1] = selDrc.bbox
      const dbX0 = x0 / userUnit
      const dbY0 = y0 / userUnit
      const dbX1 = x1 / userUnit
      const dbY1 = y1 / userUnit
      const s = screenBbox(
        { minX: dbX0, minY: dbY0, maxX: dbX1, maxY: dbY1 },
        v,
        cx_world,
        cy_world,
        cssW,
        cssH
      )
      
      // Draw outer target rings
      ctx.strokeStyle = "#ff9500" // Orange selection
      ctx.lineWidth = 3.0
      ctx.beginPath()
      ctx.arc(s.x + s.width / 2, s.y + s.height / 2, 10, 0, 2 * Math.PI)
      ctx.stroke()

      ctx.strokeStyle = "#ffffff"
      ctx.lineWidth = 1.0
      ctx.beginPath()
      ctx.arc(s.x + s.width / 2, s.y + s.height / 2, 14, 0, 2 * Math.PI)
      ctx.stroke()
    }

    setRenderedCount(drawn)
  }

  function drawGrid(
    ctx: CanvasRenderingContext2D,
    v: { offsetX: number; offsetY: number; scale: number },
    width: number,
    height: number,
    cx_world: number,
    cy_world: number
  ) {
    const targetPx = 80
    const worldSpacing = targetPx / v.scale
    const magnitude = Math.pow(10, Math.floor(Math.log10(worldSpacing)))
    const residual = worldSpacing / magnitude
    let gridStep: number
    if (residual < 2) gridStep = magnitude
    else if (residual < 5) gridStep = 2 * magnitude
    else gridStep = 5 * magnitude

    ctx.strokeStyle = "rgba(255, 255, 255, 0.05)"
    ctx.lineWidth = 1
    ctx.beginPath()

    const worldLeft = -v.offsetX / v.scale
    const worldRight = (width - v.offsetX) / v.scale
    const startX = Math.floor(worldLeft / gridStep) * gridStep
    for (let wx = startX; wx <= worldRight; wx += gridStep) {
      const sx = (wx - cx_world) * v.scale + width / 2
      ctx.moveTo(sx, 0)
      ctx.lineTo(sx, height)
    }

    const worldTop = v.offsetY / v.scale
    const worldBottom = (v.offsetY - height) / v.scale
    const startY = Math.floor(worldBottom / gridStep) * gridStep
    for (let wy = startY; wy <= worldTop; wy += gridStep) {
      const sy = -((wy - cy_world) * v.scale) + height / 2
      ctx.moveTo(0, sy)
      ctx.lineTo(width, sy)
    }
    ctx.stroke()
  }

  // Re-render on changes
  createEffect(() => {
    view()
    data()
    visibleLayers()
    interacting()
    showDRC()
    signoffData()
    render()
  })

  onMount(() => {
    const canvas = canvasRef
    if (!canvas) return

    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      
      setInteracting(true)
      if (wheelTimeout) clearTimeout(wheelTimeout)
      wheelTimeout = setTimeout(() => {
        setInteracting(false)
      }, 150)

      const cur = view()
      const delta = -e.deltaY * 0.0015
      const next = Math.min(1000, Math.max(0.0001, cur.scale * (1 + delta)))
      const rect = canvas.getBoundingClientRect()
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      // World coords (Y flipped): worldX = (sx - offsetX) / scale, worldY = -(sy - offsetY) / scale
      const wx = (px - cur.offsetX) / cur.scale
      const wy = -(py - cur.offsetY) / cur.scale
      setView({
        scale: next,
        offsetX: px - wx * next,
        offsetY: py + wy * next,
      })
    }
    const onPointerDown = (e: PointerEvent) => {
      dragging = true
      setInteracting(true)
      lastX = e.clientX
      lastY = e.clientY
      clickStartX = e.clientX
      clickStartY = e.clientY
      canvas.setPointerCapture(e.pointerId)
    }
    const onPointerMove = (e: PointerEvent) => {
      const rect = canvas.getBoundingClientRect()
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      const cur = view()
      const wx = (px - cur.offsetX) / cur.scale
      const wy = -(py - cur.offsetY) / cur.scale
      setHoverCoords({ x: wx, y: wy })

      if (!dragging) return
      setView({ ...cur, offsetX: cur.offsetX + (e.clientX - lastX), offsetY: cur.offsetY + (e.clientY - lastY) })
      lastX = e.clientX
      lastY = e.clientY
    }
    const onPointerUp = (e: PointerEvent) => {
      dragging = false
      setInteracting(false)
      canvas.releasePointerCapture(e.pointerId)

      const dx = Math.abs(e.clientX - clickStartX)
      const dy = Math.abs(e.clientY - clickStartY)
      if (dx < 3 && dy < 3) {
        handleCanvasClick(e)
      }
    }
    const onPointerLeave = () => {
      setHoverCoords(null)
      setInteracting(false)
    }

    const handleCanvasClick = (e: PointerEvent) => {
      const rect = canvas.getBoundingClientRect()
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      const cur = view()
      const wx = (px - cur.offsetX) / cur.scale
      const wy = -(py - cur.offsetY) / cur.scale
      
      const cellMap = cellsByName()
      const instanceMap = instancesByName()
      const cellBboxMap = cellBboxesByName()
      const vis = visibleLayers()
      const d = data()
      if (!d) return

      // Check if user clicked close to a DRC violation marker
      const userUnit = d.userUnit ?? 1
      if (showDRC() && signoffData()?.drc?.diagnostics) {
        for (const diag of signoffData().drc.diagnostics) {
          if (!diag.bbox) continue
          const [x0, y0, x1, y1] = diag.bbox
          const dbX0 = x0 / userUnit
          const dbY0 = y0 / userUnit
          const dbX1 = x1 / userUnit
          const dbY1 = y1 / userUnit
          
          const px0 = Math.min(dbX0, dbX1)
          const py0 = Math.min(dbY0, dbY1)
          const px1 = Math.max(dbX0, dbX1)
          const py1 = Math.max(dbY0, dbY1)
          
          const tolerance = 15 / cur.scale
          if (
            wx >= px0 - tolerance &&
            wx <= px1 + tolerance &&
            wy >= py0 - tolerance &&
            wy <= py1 + tolerance
          ) {
            setSelectedDRC({
              rule: diag.rule,
              message: diag.message,
              bbox: diag.bbox,
              logLine: diag.log_line
            })
            setSelectedObject(null)
            return
          }
        }
      }
      setSelectedDRC(null)

      const worldLeft = -cur.offsetX / cur.scale
      const worldRight = (canvas.clientWidth - cur.offsetX) / cur.scale
      const worldTop = cur.offsetY / cur.scale
      const worldBottom = (cur.offsetY - canvas.clientHeight) / cur.scale
      const viewport: BBox = {
        minX: Math.min(worldLeft, worldRight),
        minY: Math.min(worldBottom, worldTop),
        maxX: Math.max(worldLeft, worldRight),
        maxY: Math.max(worldBottom, worldTop),
      }

      for (const [cellName, binst] of instanceMap) {
        const polys = cellMap.get(cellName)
        const localBbox = cellBboxMap.get(cellName)
        if (!polys || !localBbox) continue
        const insts = getVisibleInstances(binst as any, d.bbox, viewport)
        
        for (const t of insts) {
          const worldBox = transformBbox(localBbox, t)
          if (wx < worldBox.minX || wx > worldBox.maxX || wy < worldBox.minY || wy > worldBox.maxY) continue
          
          for (const poly of polys) {
            if (!vis.has(poly.layer)) continue
            const tp = transformPoints(poly.points, t)
            if (isPointInPolygon(wx, wy, tp)) {
              setSelectedObject({
                cellName,
                layer: poly.layer,
                points: tp,
                pointsCount: tp.length / 2
              })
              return
            }
          }
        }
      }
      setSelectedObject(null)
    }


    canvas.addEventListener("wheel", onWheel, { passive: false })
    canvas.addEventListener("pointerdown", onPointerDown)
    canvas.addEventListener("pointermove", onPointerMove)
    canvas.addEventListener("pointerup", onPointerUp)
    canvas.addEventListener("pointerleave", onPointerLeave)
    window.addEventListener("resize", render)

    onCleanup(() => {
      if (wheelTimeout) clearTimeout(wheelTimeout)
      canvas.removeEventListener("wheel", onWheel)
      canvas.removeEventListener("pointerdown", onPointerDown)
      canvas.removeEventListener("pointermove", onPointerMove)
      canvas.removeEventListener("pointerup", onPointerUp)
      canvas.removeEventListener("pointerleave", onPointerLeave)
      window.removeEventListener("resize", render)
    })
  })

  const handleSendToAgent = async () => {
    const d = data()
    if (!d) return
    const summary = [
      `GDS layout: ${props.path}`,
      `${cellCount()} cells, ${polyCount().toLocaleString()} polygons`,
      `Top cell: ${d.topCell}`,
      `Layers: ${layers().join(", ")}`,
    ].join("\n")
    try {
      await navigator.clipboard.writeText(summary)
      showToast({ title: "Copied to clipboard", description: "Paste in chat to send to the agent." })
    } catch {
      // Fallback for Electron
      try {
        const textarea = document.createElement("textarea")
        textarea.value = summary
        textarea.style.position = "fixed"
        textarea.style.opacity = "0"
        document.body.appendChild(textarea)
        textarea.select()
        document.execCommand("copy")
        document.body.removeChild(textarea)
        showToast({ title: "Copied to clipboard", description: "Paste in chat to send to the agent." })
      } catch {
        showToast({ title: "Copy failed", description: summary.slice(0, 200) })
      }
    }
  }

  const toggleLayer = (layer: number) => {
    const vis = new Set(visibleLayers())
    if (vis.has(layer)) vis.delete(layer)
    else vis.add(layer)
    setVisibleLayers(vis)
  }

  const showAllLayers = () => {
    setVisibleLayers(new Set(layers()))
  }

  const hideAllLayers = () => {
    setVisibleLayers(new Set<number>())
  }

  return (
    <div class="h-full flex bg-background-stronger overflow-hidden">
      <div class="flex-1 relative min-w-0">
        <Show when={data.loading || progress()}>
          <div class="absolute inset-0 flex items-center justify-center text-14-regular text-text-weak font-mono z-10">
            {progress() || "Loading GDS..."}
          </div>
        </Show>

        <Show when={!data.loading && !data() && !progress()}>
          <div class="absolute inset-0 flex items-center justify-center text-14-regular text-text-weak">
            Failed to load GDS file.
          </div>
        </Show>

        <canvas
          ref={(el) => (canvasRef = el)}
          class="w-full h-full cursor-grab active:cursor-grabbing"
          style={{ "touch-action": "none" }}
        />

        <Show when={data()}>
          <div class="absolute top-0 left-0 right-0 flex items-center justify-between px-4 py-2 bg-gradient-to-b from-black/60 to-transparent pointer-events-none">
            <div class="flex items-center gap-3">
              <Icon name="file-tree" class="text-text-strong shrink-0" />
              <div>
                <div class="text-13-medium text-text-strong font-mono">{props.path.split("/").pop()}</div>
                <div class="text-11-regular text-text-weaker">
                  {cellCount()} cells · {polyCount().toLocaleString()} polygons · {layers().length} layers
                </div>
              </div>
            </div>
            <div class="flex items-center gap-2 pointer-events-auto">
              <button
                class="px-2 py-1 text-11-medium text-text-base rounded border border-border-weaker-base hover:bg-surface-base transition-colors"
                onClick={fitView}
              >
                Fit
              </button>
              <Show when={signoffData()?.drc?.diagnostics?.length > 0}>
                <button
                  class="px-2 py-1 text-11-medium rounded border transition-colors"
                  classList={{
                    "bg-red-500/20 text-red-400 border-red-500/40 hover:bg-red-500/30": showDRC(),
                    "text-text-base border-border-weaker-base hover:bg-surface-base": !showDRC()
                  }}
                  onClick={() => setShowDRC(!showDRC())}
                >
                  DRC ({signoffData()?.drc?.diagnostics?.length})
                </button>
              </Show>
              <button
                class="px-2 py-1 text-11-medium text-text-base rounded border border-border-weaker-base hover:bg-surface-base transition-colors"
                onClick={() => setShowLayers(!showLayers())}
              >
                {showLayers() ? "Hide" : "Layers"}
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

          <div class="absolute bottom-3 left-3 flex items-center gap-2 px-2 py-1 rounded bg-black/60 backdrop-blur-sm border border-white/5 text-10-regular text-text-weaker font-mono pointer-events-none select-none">
            <Show when={hoverCoords()}>
              <span class="border-r border-white/10 pr-2 mr-1">
                X: {Math.round(hoverCoords()!.x).toLocaleString()} · Y: {Math.round(hoverCoords()!.y).toLocaleString()}
              </span>
            </Show>
            <span>{Math.round(view().scale * 1000) / 10}%</span>
            <span class="text-white/20">·</span>
            <span>{renderedCount().toLocaleString()} rendered</span>
          </div>

          <Show when={selectedObject()}>
            <div class="absolute bottom-12 right-3 p-3 rounded bg-black/80 backdrop-blur-md border border-white/10 text-11-regular text-text-strong font-mono flex flex-col gap-1.5 shadow-lg min-w-[200px] pointer-events-auto select-none z-20">
              <div class="flex items-center justify-between border-b border-white/10 pb-1 mb-1">
                <span class="text-text-weaker text-9-medium uppercase tracking-wider">Inspector</span>
                <button class="text-text-weaker hover:text-text-strong font-sans text-12-medium ml-2" onClick={() => setSelectedObject(null)}>✕</button>
              </div>
              <div><span class="text-text-weaker font-sans">Cell:</span> {selectedObject()!.cellName}</div>
              <div><span class="text-text-weaker font-sans">Layer:</span> L{selectedObject()!.layer}</div>
              <div><span class="text-text-weaker font-sans">Vertices:</span> {selectedObject()!.pointsCount}</div>
              <button
                class="mt-1 px-2 py-1 bg-blue-600 hover:bg-blue-700 text-white text-10-medium rounded transition-colors text-center font-sans font-medium"
                onClick={() => {
                  const sel = selectedObject()
                  if (!sel) return
                  const msg = `Selected layout object:\nCell: ${sel.cellName}\nLayer: L${sel.layer}\nVertices: ${sel.pointsCount}\nCoordinates: ${JSON.stringify(sel.points)}`
                  navigator.clipboard.writeText(msg)
                  showToast({ title: "Copied to clipboard", description: "Paste in chat to send layout object details to agent." })
                }}
              >
                Send object to agent
              </button>
            </div>
          </Show>

          <Show when={selectedDRC()}>
            <div class="absolute bottom-12 right-3 p-3 rounded bg-black/80 backdrop-blur-md border border-red-500/30 text-11-regular text-text-strong font-mono flex flex-col gap-1.5 shadow-lg min-w-[240px] pointer-events-auto select-none z-20">
              <div class="flex items-center justify-between border-b border-red-500/20 pb-1 mb-1">
                <span class="text-red-400 text-9-medium uppercase tracking-wider">DRC Inspector</span>
                <button class="text-text-weaker hover:text-text-strong font-sans text-12-medium ml-2" onClick={() => setSelectedDRC(null)}>✕</button>
              </div>
              <div><span class="text-text-weaker font-sans">Rule check:</span> <span class="text-red-400 font-bold">{selectedDRC()!.rule}</span></div>
              <div class="max-h-24 overflow-y-auto text-10-regular text-text-base border border-white/5 p-1 bg-black/40 rounded whitespace-pre-wrap font-sans">
                {selectedDRC()!.message}
              </div>
              <Show when={selectedDRC()!.bbox}>
                <div><span class="text-text-weaker font-sans">BBox (um):</span> [{selectedDRC()!.bbox.map(n => n.toFixed(3)).join(", ")}]</div>
              </Show>
              <Show when={selectedDRC()!.logLine}>
                <div><span class="text-text-weaker font-sans">Report line:</span> {selectedDRC()!.logLine}</div>
              </Show>
              <button
                class="mt-1 px-2 py-1 bg-red-600 hover:bg-red-700 text-white text-10-medium rounded transition-colors text-center font-sans font-medium"
                onClick={() => {
                  const sel = selectedDRC()
                  if (!sel) return
                  const msg = `DRC Violation:\nRule: ${sel.rule}\nCoordinates: ${JSON.stringify(sel.bbox)}\nDescription: ${sel.message}`
                  navigator.clipboard.writeText(msg)
                  showToast({ title: "Copied to clipboard", description: "Paste in chat to ask the agent to fix this DRC violation." })
                }}
              >
                Send violation to agent
              </button>
            </div>
          </Show>
        </Show>
      </div>

      <Show when={data() && showLayers()}>
        <div class="w-56 shrink-0 border-l border-border-weaker-base bg-background-base overflow-y-auto">
          <div class="px-3 py-2 text-10-medium text-text-weaker uppercase tracking-wider border-b border-border-weaker-base">
            Layers ({layers().length})
          </div>
          <div class="flex items-center gap-2 px-3 py-2 border-b border-border-weaker-base bg-background-stronger">
            <button
              class="flex-1 py-1 text-10-medium text-text-base rounded border border-border-weaker-base hover:bg-surface-base transition-colors text-center"
              onClick={showAllLayers}
            >
              All
            </button>
            <button
              class="flex-1 py-1 text-10-medium text-text-base rounded border border-border-weaker-base hover:bg-surface-base transition-colors text-center"
              onClick={hideAllLayers}
            >
              None
            </button>
          </div>
          <For each={layers()}>
            {(layer) => (
              <div class="w-full flex items-center gap-2 px-3 py-1.5 text-12-regular hover:bg-surface-base transition-colors">
                <button
                  class="flex items-center gap-2 flex-1 min-w-0"
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
                <Show when={visibleLayers().has(layer)}>
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.1"
                    value={layerOpacity(layer)}
                    onInput={(e) => updateLayerOpacity(layer, parseFloat(e.currentTarget.value))}
                    onClick={(e) => e.stopPropagation()}
                    class="w-10 h-1 accent-blue-500 bg-white/10 rounded-lg appearance-none cursor-pointer shrink-0"
                  />
                </Show>
              </div>
            )}
          </For>
        </div>
      </Show>
    </div>
  )
}
