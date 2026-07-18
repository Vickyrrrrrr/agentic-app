/**
 * GDS-II binary format parser — Web Worker version.
 * Runs off-thread to avoid freezing the UI on large files (170MB+).
 * Posts progress updates during parsing.
 */

import { parseGds, buildInstancedScene, type GdsFile, type InstancedScene } from "./gds-parser"

export type GdsWorkerResult = {
  cells: [string, { layer: number; points: number[]; bbox: { minX: number; minY: number; maxX: number; maxY: number } }[]][]
  instances: [string, { binCols: number; binRows: number; bins: number[][][] }][]
  cellBboxes: [string, { minX: number; minY: number; maxX: number; maxY: number }][]
  userUnit: number
  polygonCount: number
  cellCount: number
  topCell: string | null
  layers: number[]
  bbox: { minX: number; minY: number; maxX: number; maxY: number }
  perLayerCount: Record<number, number>
  perLayerBbox: Record<number, { minX: number; minY: number; maxX: number; maxY: number }>
}

self.onmessage = (e: MessageEvent<{ buffer: ArrayBuffer }>) => {
  const { buffer } = e.data
  try {
    // Phase 1: Parse
    ;(self as any).postMessage({ type: "progress", phase: "parsing", polygonCount: 0 })
    const gds: GdsFile = parseGds(buffer)

    // Phase 2: Build instanced scene
    ;(self as any).postMessage({
      type: "progress",
      phase: "instancing",
      polygonCount: gds.totalPolygons,
      cellCount: gds.cells.size,
    })
    const scene: InstancedScene = buildInstancedScene(gds)

    // Phase 3: Serialize for transfer (Maps → Arrays for structured clone)
    ;(self as any).postMessage({
      type: "progress",
      phase: "serializing",
      polygonCount: scene.polygonCount,
    })

    const cells: GdsWorkerResult["cells"] = []
    for (const [name, polys] of scene.cells) {
      cells.push([
        name,
        polys.map((p) => ({ layer: p.layer, points: p.points, bbox: bboxFromPoints(p.points) })),
      ])
    }

    // Instances are binned below after layout bbox is calculated

    // Extract layers
    const layerSet = new Set<number>()
    for (const [, polys] of cells) {
      for (const p of polys) layerSet.add(p.layer)
    }

    // Compute local cell bounds once, then global layout bounds from every transformed instance.
    // This matches the instanced viewer architecture: the top cell can be empty or contain
    // marker-only geometry, so its direct bbox is not reliable for fit-to-view.
    const cellBboxMap = new Map<string, { minX: number; minY: number; maxX: number; maxY: number }>()
    for (const [cellName, polys] of scene.cells) {
      const cellBbox = makeEmptyBbox()
      for (const p of polys) expandBboxWithPoints(cellBbox, p.points)
      if (isValidBbox(cellBbox)) cellBboxMap.set(cellName, cellBbox)
    }

    let bbox = makeEmptyBbox()
    for (const [cellName, insts] of scene.instances) {
      const cellBbox = cellBboxMap.get(cellName)
      if (!cellBbox || !insts || insts.length === 0) continue
      for (const t of insts) expandBboxWithTransformedBbox(bbox, cellBbox, t)
    }

    if (!isValidBbox(bbox)) {
      bbox = makeEmptyBbox()
      for (const cellBbox of cellBboxMap.values()) expandBboxWithBbox(bbox, cellBbox)
    }

    if (!isValidBbox(bbox)) {
      bbox = { minX: 0, minY: 0, maxX: 1, maxY: 1 }
    } else {
      bbox = padDegenerateBbox(bbox)
    }

    const binCols = 32
    const binRows = 32
    const binW = Math.max(bbox.maxX - bbox.minX, 1) / binCols
    const binH = Math.max(bbox.maxY - bbox.minY, 1) / binRows

    const instances: GdsWorkerResult["instances"] = []
    for (const [name, insts] of scene.instances) {
      const bins: number[][][] = Array.from({ length: binCols * binRows }, () => [])
      const cellBbox = cellBboxMap.get(name)
      for (const t of insts) {
        const item = [...t] as number[]
        if (!cellBbox) {
          const c = Math.max(0, Math.min(binCols - 1, Math.floor((t[4] - bbox.minX) / binW)))
          const r = Math.max(0, Math.min(binRows - 1, Math.floor((t[5] - bbox.minY) / binH)))
          bins[r * binCols + c].push(item)
          continue
        }
        const worldBox = transformedBbox(cellBbox, t)
        const minCol = Math.max(0, Math.min(binCols - 1, Math.floor((worldBox.minX - bbox.minX) / binW)))
        const maxCol = Math.max(0, Math.min(binCols - 1, Math.floor((worldBox.maxX - bbox.minX) / binW)))
        const minRow = Math.max(0, Math.min(binRows - 1, Math.floor((worldBox.minY - bbox.minY) / binH)))
        const maxRow = Math.max(0, Math.min(binRows - 1, Math.floor((worldBox.maxY - bbox.minY) / binH)))
        for (let r = minRow; r <= maxRow; r++) {
          for (let c = minCol; c <= maxCol; c++) {
            bins[r * binCols + c].push(item)
          }
        }
      }
      instances.push([
        name,
        { binCols, binRows, bins }
      ])
    }

    // Compute per-layer count and bbox for agent programmatic inspection
    type BBox2D = { minX: number; minY: number; maxX: number; maxY: number }
    const perLayerCount: Record<number, number> = {}
    const perLayerBbox: Record<number, BBox2D> = {}

    for (const layer of layerSet) {
      perLayerCount[layer] = 0
      perLayerBbox[layer] = makeEmptyBbox()
    }

    const cellLayerBboxes = new Map<string, Map<number, BBox2D>>()
    const cellLayerCounts = new Map<string, Map<number, number>>()
    for (const [cellName, polys] of scene.cells) {
      const layerMap = new Map<number, BBox2D>()
      const countMap = new Map<number, number>()
      for (const p of polys) {
        let b = layerMap.get(p.layer)
        if (!b) {
          b = makeEmptyBbox()
          layerMap.set(p.layer, b)
        }
        countMap.set(p.layer, (countMap.get(p.layer) || 0) + 1)
        expandBboxWithPoints(b, p.points)
      }
      cellLayerBboxes.set(cellName, layerMap)
      cellLayerCounts.set(cellName, countMap)
    }

    for (const [cellName, insts] of scene.instances) {
      const layerMap = cellLayerBboxes.get(cellName)
      if (!layerMap || !insts) continue
      const countMap = cellLayerCounts.get(cellName) ?? new Map<number, number>()
      
      for (const [layer, cellLayerBbox] of layerMap) {
        const polyCountOnLayer = countMap.get(layer) || 0
        perLayerCount[layer] = (perLayerCount[layer] || 0) + polyCountOnLayer * insts.length
        
        let globalB = perLayerBbox[layer]
        if (!globalB) {
          globalB = makeEmptyBbox()
          perLayerBbox[layer] = globalB
        }
        for (const t of insts) {
          expandBboxWithTransformedBbox(globalB, cellLayerBbox, t)
        }
      }
    }

    for (const layer of layerSet) {
      const b = perLayerBbox[layer]
      if (!isValidBbox(b)) {
        perLayerBbox[layer] = { minX: 0, minY: 0, maxX: 0, maxY: 0 }
      } else {
        perLayerBbox[layer] = padDegenerateBbox(b)
      }
    }

    const result: GdsWorkerResult = {
      cells,
      instances,
      cellBboxes: [...cellBboxMap.entries()].map(([name, b]) => [name, padDegenerateBbox(b)]),
      userUnit: scene.userUnit,
      polygonCount: scene.polygonCount,
      cellCount: gds.cells.size,
      topCell: gds.topCell,
      layers: [...layerSet].sort((a, b) => a - b),
      bbox,
      perLayerCount,
      perLayerBbox,
    }

    ;(self as any).postMessage({ type: "done", result })
  } catch (err) {
    ;(self as any).postMessage({ type: "error", message: String(err) })
  }
}

function makeEmptyBbox() {
  return { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity }
}

function isValidBbox(bbox: { minX: number; minY: number; maxX: number; maxY: number }) {
  return (
    Number.isFinite(bbox.minX) &&
    Number.isFinite(bbox.minY) &&
    Number.isFinite(bbox.maxX) &&
    Number.isFinite(bbox.maxY) &&
    bbox.maxX >= bbox.minX &&
    bbox.maxY >= bbox.minY
  )
}

function expandBboxWithPoints(bbox: { minX: number; minY: number; maxX: number; maxY: number }, points: number[]) {
  for (let i = 0; i + 1 < points.length; i += 2) {
    const x = points[i]
    const y = points[i + 1]
    if (x < bbox.minX) bbox.minX = x
    if (y < bbox.minY) bbox.minY = y
    if (x > bbox.maxX) bbox.maxX = x
    if (y > bbox.maxY) bbox.maxY = y
  }
}

function bboxFromPoints(points: number[]) {
  const bbox = makeEmptyBbox()
  expandBboxWithPoints(bbox, points)
  return isValidBbox(bbox) ? bbox : { minX: 0, minY: 0, maxX: 0, maxY: 0 }
}

function expandBboxWithBbox(
  bbox: { minX: number; minY: number; maxX: number; maxY: number },
  other: { minX: number; minY: number; maxX: number; maxY: number },
) {
  if (!isValidBbox(other)) return
  if (other.minX < bbox.minX) bbox.minX = other.minX
  if (other.minY < bbox.minY) bbox.minY = other.minY
  if (other.maxX > bbox.maxX) bbox.maxX = other.maxX
  if (other.maxY > bbox.maxY) bbox.maxY = other.maxY
}

function expandBboxWithTransformedBbox(
  bbox: { minX: number; minY: number; maxX: number; maxY: number },
  cellBbox: { minX: number; minY: number; maxX: number; maxY: number },
  t: number[],
) {
  const corners = [
    [cellBbox.minX, cellBbox.minY],
    [cellBbox.maxX, cellBbox.minY],
    [cellBbox.maxX, cellBbox.maxY],
    [cellBbox.minX, cellBbox.maxY],
  ]
  for (const [x, y] of corners) {
    const tx = t[0] * x + t[1] * y + t[4]
    const ty = t[2] * x + t[3] * y + t[5]
    if (tx < bbox.minX) bbox.minX = tx
    if (ty < bbox.minY) bbox.minY = ty
    if (tx > bbox.maxX) bbox.maxX = tx
    if (ty > bbox.maxY) bbox.maxY = ty
  }
}

function transformedBbox(
  cellBbox: { minX: number; minY: number; maxX: number; maxY: number },
  t: number[],
) {
  const bbox = makeEmptyBbox()
  expandBboxWithTransformedBbox(bbox, cellBbox, t)
  return bbox
}

function padDegenerateBbox(bbox: { minX: number; minY: number; maxX: number; maxY: number }) {
  const next = { ...bbox }
  const width = next.maxX - next.minX
  const height = next.maxY - next.minY
  const pad = Math.max(width, height, 1) * 0.01
  if (width <= 0) {
    next.minX -= pad
    next.maxX += pad
  }
  if (height <= 0) {
    next.minY -= pad
    next.maxY += pad
  }
  return next
}
