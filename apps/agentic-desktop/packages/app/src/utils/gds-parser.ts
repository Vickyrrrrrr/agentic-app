/**
 * GDS-II binary format parser.
 * Reads .gds files → cell hierarchy + polygons + instanced references.
 *
 * Fixed based on study of the GDS-II spec and open-source reference:
 * - GDS 8-byte floats use excess-64 base-16 format (NOT IEEE 754)
 * - AREF (array references) expanded as col×row grid
 * - BOX records treated as polygons
 * - PATH records expanded to polygon rectangles
 * - Instanced rendering: stores transforms, doesn't flatten
 * - Handles trailing null bytes after ENDLIB
 */

export type Polygon = { x: number[]; y: number[] }

export type GdsPolygon = {
  layer: number
  dataType: number
  points: number[]  // [x1,y1, x2,y2, ...] in user units
}

export type GdsCellRef = {
  name: string
  x: number
  y: number
  mag: number
  angle: number
  reflect: boolean
}

export type GdsArrayRef = {
  name: string
  x: number
  y: number
  colDx: number
  colDy: number
  rowDx: number
  rowDy: number
  columns: number
  rows: number
  mag: number
  angle: number
  reflect: boolean
}

export type GdsCell = {
  name: string
  polygons: GdsPolygon[]
  srefs: GdsCellRef[]
  arefs: GdsArrayRef[]
  bbox: { minX: number; minY: number; maxX: number; maxY: number }
}

export type GdsFile = {
  cells: Map<string, GdsCell>
  topCell: string | null
  units: { userUnit: number; metersPerUnit: number }
  totalPolygons: number
}

/**
 * Decode a GDS-II 8-byte real number.
 * Format: excess-64 exponent (7 bits), base-16 mantissa (7 bytes, 56 bits).
 * This is NOT IEEE 754 — using getFloat64() gives wrong values.
 */
function parseGdsReal8(view: DataView, offset: number): number {
  const firstByte = view.getUint8(offset)
  if (firstByte === 0 && view.getUint32(offset) === 0 && view.getUint32(offset + 4) === 0) return 0

  const sign = (firstByte & 0x80) ? -1 : 1
  const exponent = (firstByte & 0x7f) - 64

  let mantissa = 0
  for (let i = 1; i < 8; i++) {
    const byte = view.getUint8(offset + i)
    const hi = (byte >> 4) & 0x0f
    const lo = byte & 0x0f
    mantissa += hi * Math.pow(16, -(2 * i - 1))
    mantissa += lo * Math.pow(16, -(2 * i))
  }

  return sign * mantissa * Math.pow(16, exponent)
}

export function parseGds(data: ArrayBuffer): GdsFile {
  const view = new DataView(data)
  const cells = new Map<string, GdsCell>()
  let topCell: string | null = null
  let units = { userUnit: 1e-9, metersPerUnit: 1e-3 }
  let totalPolygons = 0

  let offset = 0
  let currentCell: GdsCell | null = null
  let currentLayer = 0
  let currentDataType = 0
  let currentWidth = 0
  let currentPathType = 0
  let currentXY: number[] = []
  let currentSname = ""
  let currentMag = 1.0
  let currentAngle = 0.0
  let currentStrans = 0
  let currentCols = 0
  let currentRows = 0
  let elementType: "boundary" | "path" | "box" | "sref" | "aref" | null = null

  while (offset + 4 <= view.byteLength) {
    const recordLen = view.getUint16(offset, false) // big-endian
    const tag = view.getUint16(offset + 2, false)   // 2-byte tag (type + dataType combined)

    if (recordLen === 0) break  // trailing padding
    if (recordLen < 4 || recordLen % 2 !== 0) break  // malformed
    if (offset + recordLen > view.byteLength) break  // truncated

    const recordType = (tag >> 8) & 0xff
    const dataType = tag & 0xff
    const dataStart = offset + 4
    const dataLen = recordLen - 4

    switch (recordType) {
      case 0x00: // HEADER
        break

      case 0x03: { // UNITS — two GDS 8-byte reals
        if (dataLen >= 16) {
          const userPerMeter = parseGdsReal8(view, dataStart)
          const meterPerUser = parseGdsReal8(view, dataStart + 8)
          units = { userUnit: meterPerUser, metersPerUnit: userPerMeter }
        }
        break
      }

      case 0x05: // BGNSTR
        currentCell = { name: "", polygons: [], srefs: [], arefs: [], bbox: { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity } }
        break

      case 0x06: { // STRNAME
        const name = readGdsString(view, dataStart, dataLen)
        if (currentCell) {
          currentCell.name = name
          cells.set(name, currentCell)
          if (!topCell) topCell = name
        }
        break
      }

      case 0x07: // ENDSTR
        currentCell = null
        break

      case 0x08: // BOUNDARY
        elementType = "boundary"
        resetElement()
        break

      case 0x09: // PATH
        elementType = "path"
        resetElement()
        break

      case 0x0a: // SREF
        elementType = "sref"
        resetElement()
        break

      case 0x0b: // AREF
        elementType = "aref"
        resetElement()
        break

      case 0x0c: // BOX
        elementType = "box"
        resetElement()
        break

      case 0x0d: // LAYER
        currentLayer = readGdsInt2(view, dataStart, dataType)
        break

      case 0x0e: // DATATYPE
        currentDataType = readGdsInt2(view, dataStart, dataType)
        break

      case 0x0f: // WIDTH
        currentWidth = readGdsInt4(view, dataStart, dataType)
        break

      case 0x21: // PATHTYPE
        currentPathType = readGdsInt2(view, dataStart, dataType)
        break

      case 0x12: // SNAME
        currentSname = readGdsString(view, dataStart, dataLen)
        break

      case 0x16: // STRANS
        currentStrans = readGdsInt2(view, dataStart, dataType)
        break

      case 0x17: // MAG — GDS 8-byte real
        currentMag = parseGdsReal8(view, dataStart)
        break

      case 0x18: // ANGLE — GDS 8-byte real
        currentAngle = parseGdsReal8(view, dataStart)
        break

      case 0x13: // COLROW
        currentCols = view.getUint16(dataStart, false)
        currentRows = view.getUint16(dataStart + 2, false)
        break

      case 0x10: // XY
        currentXY = readGdsXYArray(view, dataStart, dataLen, dataType)
        break

      case 0x11: // ENDEL
        if (currentCell) {
          if (elementType === "boundary" || elementType === "box") {
            if (currentXY.length >= 6) { // at least 3 points
              currentCell.polygons.push({
                layer: currentLayer,
                dataType: currentDataType,
                points: currentXY.slice(),
              })
              totalPolygons++
              updateBbox(currentCell.bbox, currentXY)
            }
          } else if (elementType === "path") {
            // Expand path into polygon rectangles
            const polys = expandPath(currentXY, currentWidth, currentPathType)
            for (const poly of polys) {
              currentCell.polygons.push({
                layer: currentLayer,
                dataType: currentDataType,
                points: poly,
              })
              totalPolygons++
              updateBbox(currentCell.bbox, poly)
            }
          } else if (elementType === "sref") {
            if (currentXY.length >= 2) {
              currentCell.srefs.push({
                name: currentSname,
                x: currentXY[0],
                y: currentXY[1],
                mag: currentMag,
                angle: currentAngle,
                reflect: !!(currentStrans & 0x8000),
              })
            }
          } else if (elementType === "aref") {
            // AREF has 3 XY points: origin, col-step, row-step
            if (currentXY.length >= 6) {
              const ox = currentXY[0], oy = currentXY[1]
              currentCell.arefs.push({
                name: currentSname,
                x: ox,
                y: oy,
                colDx: currentCols > 0 ? (currentXY[2] - ox) / currentCols : 0,
                colDy: currentCols > 0 ? (currentXY[3] - oy) / currentCols : 0,
                rowDx: currentRows > 0 ? (currentXY[4] - ox) / currentRows : 0,
                rowDy: currentRows > 0 ? (currentXY[5] - oy) / currentRows : 0,
                columns: currentCols,
                rows: currentRows,
                mag: currentMag,
                angle: currentAngle,
                reflect: !!(currentStrans & 0x8000),
              })
            }
          }
        }
        elementType = null
        break

      case 0x04: // ENDLIB
        offset = view.byteLength // done
        continue
    }

    offset += recordLen
  }

  // Determine top cell: the one not referenced by any other cell
  if (cells.size > 0) {
    const referenced = new Set<string>()
    for (const cell of cells.values()) {
      for (const ref of cell.srefs) referenced.add(ref.name)
      for (const ref of cell.arefs) referenced.add(ref.name)
    }
    const unreferenced = [...cells.keys()].filter((name) => !referenced.has(name))
    if (unreferenced.length > 0) topCell = unreferenced[unreferenced.length - 1]
  }

  return { cells, topCell, units, totalPolygons }

  function resetElement() {
    currentLayer = 0
    currentDataType = 0
    currentWidth = 0
    currentPathType = 0
    currentXY = []
    currentSname = ""
    currentMag = 1.0
    currentAngle = 0.0
    currentStrans = 0
    currentCols = 0
    currentRows = 0
  }
}

function readGdsInt2(view: DataView, offset: number, dataType: number): number {
  if (dataType === 2 && offset + 2 <= view.byteLength) return view.getInt16(offset, false)
  if (dataType === 3 && offset + 4 <= view.byteLength) return view.getInt32(offset, false)
  return 0
}

function readGdsInt4(view: DataView, offset: number, dataType: number): number {
  if (dataType === 3 && offset + 4 <= view.byteLength) return view.getInt32(offset, false)
  if (dataType === 2 && offset + 2 <= view.byteLength) return view.getInt16(offset, false)
  return 0
}

function readGdsString(view: DataView, offset: number, len: number): string {
  // Strip trailing null byte
  let actualLen = len
  if (actualLen > 0 && view.getUint8(offset + actualLen - 1) === 0) actualLen--
  let str = ""
  for (let i = 0; i < actualLen && offset + i < view.byteLength; i++) {
    str += String.fromCharCode(view.getUint8(offset + i))
  }
  return str.trim()
}

function readGdsXYArray(view: DataView, offset: number, len: number, dataType: number): number[] {
  const points: number[] = []
  // GDS coordinates are 4-byte integers (dataType=3) or 2-byte integers (dataType=2)
  const bytesPerVal = dataType === 3 ? 4 : 2
  const vals = Math.floor(len / bytesPerVal)
  for (let i = 0; i < vals && offset + (i + 1) * bytesPerVal <= view.byteLength; i++) {
    if (bytesPerVal === 4) points.push(view.getInt32(offset + i * 4, false))
    else points.push(view.getInt16(offset + i * 2, false))
  }
  return points
}

function updateBbox(bbox: { minX: number; minY: number; maxX: number; maxY: number }, points: number[]) {
  for (let i = 0; i + 1 < points.length; i += 2) {
    const x = points[i]
    const y = points[i + 1]
    if (x < bbox.minX) bbox.minX = x
    if (y < bbox.minY) bbox.minY = y
    if (x > bbox.maxX) bbox.maxX = x
    if (y > bbox.maxY) bbox.maxY = y
  }
}

/** Expand a GDS PATH into polygon rectangles (one per segment). */
function expandPath(xy: number[], width: number, pathType: number): number[][] {
  if (xy.length < 4 || width <= 0) return []
  const hw = Math.abs(width) / 2
  const polys: number[][] = []

  for (let i = 0; i + 3 < xy.length; i += 2) {
    const x0 = xy[i], y0 = xy[i + 1]
    const x1 = xy[i + 2], y1 = xy[i + 3]
    const dx = x1 - x0
    const dy = y1 - y0
    const len = Math.sqrt(dx * dx + dy * dy)
    if (len === 0) continue

    const nx = -dy / len * hw
    const ny = dx / len * hw

    // Path type 2 = half-width extension at ends
    let ex = 0, ey = 0
    if (pathType === 2) {
      ex = dx / len * hw
      ey = dy / len * hw
    }

    polys.push([
      x0 - nx - ex, y0 - ny - ey,
      x1 - nx + ex, y1 - ny + ey,
      x1 + nx + ex, y1 + ny + ey,
      x0 + nx - ex, y0 + ny - ey,
    ])
  }
  return polys
}

/**
 * Instanced scene: stores each cell's polygons once + list of transforms
 * where the cell is instantiated. Avoids flattening millions of polygons.
 */
export type Affine2D = [number, number, number, number, number, number]

export type InstancedScene = {
  cells: Map<string, GdsPolygon[]>
  instances: Map<string, Affine2D[]>
  userUnit: number
  polygonCount: number
}

function affineIdentity(): Affine2D { return [1, 0, 0, 1, 0, 0] }

function affineCompose(parent: Affine2D, ox: number, oy: number, mag: number, angleDeg: number, reflect: boolean): Affine2D {
  const rad = angleDeg * Math.PI / 180
  const cosA = Math.cos(rad) * mag
  const sinA = Math.sin(rad) * mag
  let a = cosA, b = -sinA, c = sinA, d = cosA
  if (reflect) { b = sinA; d = -cosA }
  const [pa, pb, pc, pd, ptx, pty] = parent
  return [
    pa * a + pb * c,
    pa * b + pb * d,
    pc * a + pd * c,
    pc * b + pd * d,
    pa * ox + pb * oy + ptx,
    pc * ox + pd * oy + pty,
  ]
}

/**
 * Build an instanced scene from parsed GDS data.
 * Walks the cell hierarchy, collecting composed transforms without duplicating polygons.
 */
export function buildInstancedScene(gdsFile: GdsFile): InstancedScene {
  const cells = new Map<string, GdsPolygon[]>()
  const instances = new Map<string, Affine2D[]>()
  let polygonCount = 0

  // Register all cells that have polygons
  for (const [name, cell] of gdsFile.cells) {
    if (cell.polygons.length > 0) {
      cells.set(name, cell.polygons)
      instances.set(name, [])
    }
  }

  if (!gdsFile.topCell) return { cells, instances, userUnit: gdsFile.units.userUnit, polygonCount: 0 }

  const visited = new Set<string>()

  function walk(cellName: string, transform: Affine2D, depth: number) {
    if (depth > 15) return // prevent infinite recursion
    const cell = gdsFile.cells.get(cellName)
    if (!cell) return

    // Record this transform for the cell's own polygons
    if (cell.polygons.length > 0) {
      instances.get(cellName)?.push(transform)
      polygonCount += cell.polygons.length
    }

    // SREFs (single references)
    for (const sr of cell.srefs) {
      const key = `${sr.name}@${transform[4] + sr.x},${transform[5] + sr.y}`
      if (visited.has(key)) continue
      visited.add(key)
      const childTransform = affineCompose(transform, sr.x, sr.y, sr.mag, sr.angle, sr.reflect)
      walk(sr.name, childTransform, depth + 1)
      visited.delete(key)
    }

    // AREFs (array references — col×row grid)
    for (const ar of cell.arefs) {
      for (let col = 0; col < ar.columns; col++) {
        for (let row = 0; row < ar.rows; row++) {
          const ex = ar.x + col * ar.colDx + row * ar.rowDx
          const ey = ar.y + col * ar.colDy + row * ar.rowDy
          const key = `${ar.name}@${transform[4] + ex},${transform[5] + ey}`
          if (visited.has(key)) continue
          visited.add(key)
          const childTransform = affineCompose(transform, ex, ey, ar.mag, ar.angle, ar.reflect)
          walk(ar.name, childTransform, depth + 1)
          visited.delete(key)
        }
      }
    }
  }

  walk(gdsFile.topCell, affineIdentity(), 0)

  return { cells, instances, userUnit: gdsFile.units.userUnit, polygonCount }
}

/**
 * Apply an affine transform to a polygon's points.
 */
export function transformPolygon(points: number[], t: Affine2D): number[] {
  const [a, b, c, d, tx, ty] = t
  const result: number[] = []
  for (let i = 0; i + 1 < points.length; i += 2) {
    result.push(a * points[i] + b * points[i + 1] + tx)
    result.push(c * points[i] + d * points[i + 1] + ty)
  }
  return result
}
