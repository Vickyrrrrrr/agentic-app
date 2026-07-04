/**
 * GDS-II binary format parser.
 * Reads .gds files → cell hierarchy + polygons.
 * Runs in main thread (Web Worker upgrade later for large files).
 *
 * GDS-II format: each record = 2-byte length (big-endian) + 1-byte type + 1-byte data type.
 * Data types: 0=none, 1=2-byte int, 2=4-byte int, 4=8-byte float, 5=ASCII.
 */

export type GdsLayer = {
  layer: number
  dataType: number
}

export type GdsPolygon = {
  layer: number
  dataType: number
  points: number[]  // [x1,y1, x2,y2, ...] in user units
}

export type GdsPath = {
  layer: number
  dataType: number
  width: number
  points: number[]
}

export type GdsCellRef = {
  name: string
  x: number
  y: number
  mag: number
  angle: number
}

export type GdsCell = {
  name: string
  polygons: GdsPolygon[]
  paths: GdsPath[]
  refs: GdsCellRef[]
  bbox: { minX: number; minY: number; maxX: number; maxY: number }
}

export type GdsFile = {
  cells: Map<string, GdsCell>
  topCell: string | null
  units: { userUnits: number; metersUnits: number }
  totalPolygons: number
}

// GDS-II record types
const RT = {
  HEADER: 0x00, BGNLIB: 0x01, LIBNAME: 0x02, UNITS: 0x03, ENDLIB: 0x04,
  BGNSTR: 0x05, STRNAME: 0x06, ENDSTR: 0x07, BOUNDARY: 0x08, PATH: 0x09,
  SREF: 0x0a, AREF: 0x0b, TEXT: 0x0c, LAYER: 0x0d, DATATYPE: 0x0e,
  PATHTYPE: 0x21, WIDTH: 0x0f, XY: 0x10, ENDEL: 0x11, SNAME: 0x12,
  COLROW: 0x13, STRANS: 0x16, MAG: 0x17, ANGLE: 0x18,
} as const

export function parseGds(data: ArrayBuffer): GdsFile {
  const view = new DataView(data)
  const cells = new Map<string, GdsCell>()
  let topCell: string | null = null
  let units = { userUnits: 1e-9, metersUnits: 1e-3 }
  let totalPolygons = 0

  let offset = 0
  let currentCell: GdsCell | null = null
  let currentLayer = 0
  let currentDataType = 0
  let currentWidth = 0
  let currentXY: number[] = []
  let currentSname = ""
  let currentMag = 1.0
  let currentAngle = 0.0
  let inBoundary = false
  let inPath = false
  let inSref = false
  let inAref = false

  while (offset < view.byteLength - 3) {
    const recordLen = view.getUint16(offset, false) // big-endian
    const recordType = view.getUint8(offset + 2)
    const dataType = view.getUint8(offset + 3)

    if (recordLen < 4 || offset + recordLen > view.byteLength) break

    const dataStart = offset + 4
    const dataLen = recordLen - 4

    switch (recordType) {
      case RT.HEADER:
        // version (2-byte int) — skip
        break

      case RT.UNITS: {
        // Two 8-byte floats: user units per meter, meters per user unit
        if (dataLen >= 16) {
          const userPerMeter = view.getFloat64(dataStart, false)
          const meterPerUser = view.getFloat64(dataStart + 8, false)
          units = { userUnits: meterPerUser, metersUnits: userPerMeter }
        }
        break
      }

      case RT.BGNSTR:
        currentCell = { name: "", polygons: [], paths: [], refs: [], bbox: { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity } }
        break

      case RT.STRNAME: {
        const name = readString(view, dataStart, dataLen)
        if (currentCell) {
          currentCell.name = name
          cells.set(name, currentCell)
          if (!topCell) topCell = name
        }
        break
      }

      case RT.ENDSTR:
        currentCell = null
        break

      case RT.BOUNDARY:
        inBoundary = true
        inPath = false
        inSref = false
        inAref = false
        currentXY = []
        break

      case RT.PATH:
        inPath = true
        inBoundary = false
        inSref = false
        inAref = false
        currentXY = []
        currentWidth = 0
        break

      case RT.SREF:
        inSref = true
        inBoundary = false
        inPath = false
        currentSname = ""
        currentMag = 1.0
        currentAngle = 0.0
        currentXY = []
        break

      case RT.LAYER:
        currentLayer = readInt2(view, dataStart, dataType)
        break

      case RT.DATATYPE:
        currentDataType = readInt2(view, dataStart, dataType)
        break

      case RT.WIDTH:
        currentWidth = readInt(view, dataStart, dataType)
        break

      case RT.SNAME:
        currentSname = readString(view, dataStart, dataLen)
        break

      case RT.MAG:
        currentMag = readFloat8(view, dataStart, dataType)
        break

      case RT.ANGLE:
        currentAngle = readFloat8(view, dataStart, dataType)
        break

      case RT.XY:
        currentXY = readXYArray(view, dataStart, dataLen, dataType)
        break

      case RT.ENDEL:
        if (inBoundary && currentCell && currentXY.length >= 4) {
          const poly: GdsPolygon = { layer: currentLayer, dataType: currentDataType, points: currentXY.slice() }
          currentCell.polygons.push(poly)
          totalPolygons++
          updateBbox(currentCell.bbox, currentXY)
        } else if (inPath && currentCell && currentXY.length >= 4) {
          const path: GdsPath = { layer: currentLayer, dataType: currentDataType, width: currentWidth, points: currentXY.slice() }
          currentCell.paths.push(path)
          updateBbox(currentCell.bbox, currentXY)
        } else if (inSref && currentCell && currentXY.length >= 2) {
          currentCell.refs.push({
            name: currentSname,
            x: currentXY[0],
            y: currentXY[1],
            mag: currentMag,
            angle: currentAngle,
          })
        }
        inBoundary = false
        inPath = false
        inSref = false
        inAref = false
        break

      case RT.ENDLIB:
        offset = view.byteLength // done
        continue
    }

    offset += recordLen
  }

  // Determine top cell: the one not referenced by any other cell
  if (cells.size > 0) {
    const referenced = new Set<string>()
    for (const cell of cells.values()) {
      for (const ref of cell.refs) referenced.add(ref.name)
    }
    const unreferenced = [...cells.keys()].filter((name) => !referenced.has(name))
    if (unreferenced.length > 0) topCell = unreferenced[0]
  }

  return { cells, topCell, units, totalPolygons }
}

function readInt2(view: DataView, offset: number, dataType: number): number {
  if (dataType === 1 && offset + 2 <= view.byteLength) return view.getInt16(offset, false)
  if (dataType === 2 && offset + 4 <= view.byteLength) return view.getInt32(offset, false)
  return 0
}

function readInt(view: DataView, offset: number, dataType: number): number {
  if (dataType === 1 && offset + 2 <= view.byteLength) return view.getInt16(offset, false)
  if (dataType === 2 && offset + 4 <= view.byteLength) return view.getInt32(offset, false)
  return 0
}

function readFloat8(view: DataView, offset: number, dataType: number): number {
  if (dataType === 4 && offset + 8 <= view.byteLength) return view.getFloat64(offset, false)
  if (dataType === 3 && offset + 4 <= view.byteLength) return view.getFloat32(offset, false)
  return 0
}

function readString(view: DataView, offset: number, len: number): string {
  let str = ""
  for (let i = 0; i < len && offset + i < view.byteLength; i++) {
    const ch = view.getUint8(offset + i)
    if (ch === 0) break
    str += String.fromCharCode(ch)
  }
  return str.trim()
}

function readXYArray(view: DataView, offset: number, len: number, dataType: number): number[] {
  const points: number[] = []
  const bytesPerVal = dataType === 2 ? 4 : 2
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

/**
 * Flatten a cell hierarchy into a single polygon list for rendering.
 * Resolves SREF references recursively.
 */
export function flattenCell(
  gdsFile: GdsFile,
  cellName: string,
  dx = 0,
  dy = 0,
  depth = 0,
  maxDepth = 10,
): GdsPolygon[] {
  const cell = gdsFile.cells.get(cellName)
  if (!cell || depth > maxDepth) return []

  const result: GdsPolygon[] = []

  // Add this cell's polygons (offset by dx, dy)
  for (const poly of cell.polygons) {
    result.push({
      layer: poly.layer,
      dataType: poly.dataType,
      points: poly.points.map((v, i) => (i % 2 === 0 ? v + dx : v + dy)),
    })
  }

  // Recursively resolve references
  for (const ref of cell.refs) {
    const refPolygons = flattenCell(gdsFile, ref.name, dx + ref.x, dy + ref.y, depth + 1, maxDepth)
    result.push(...refPolygons)
  }

  return result
}
