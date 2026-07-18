import { createResource, createSignal, createMemo, createEffect, onMount, onCleanup, untrack, Show, For } from "solid-js"
import { useSessionLayout } from "@/pages/session/session-layout"
import { callAgenticTool } from "@/utils/agentic"
import { decode64 } from "@/utils/base64"
import { useFile } from "@/context/file"
import { showToast } from "@/utils/toast"

interface VCDSignal {
  id: string
  name: string
  width: number
  type: string
}

interface VCDChange {
  time: number
  value: string
}

interface VCDData {
  timescale: string
  signals: VCDSignal[]
  changes: Map<string, VCDChange[]>
  duration: number
}

// Global cache to persist signal filter query across tab unmounts
const waveformFilterCache = new Map<string, string>()

function parseVCD(content: string): VCDData {
  const signals: VCDSignal[] = []
  const changes = new Map<string, VCDChange[]>()
  let timescale = "1 ns"
  let currentTime = 0
  const lines = content.split("\n")
  let i = 0

  while (i < lines.length) {
    const line = lines[i].trim()
    if (!line) { i++; continue }

    if (line.startsWith("$timescale")) {
      const endIdx = lines.findIndex((l, idx) => idx > i && l.trim() === "$end")
      if (endIdx === -1) { i++; continue }
      const block = lines.slice(i, endIdx + 1).join(" ")
      timescale = block.replace("$timescale", "").replace("$end", "").trim()
      i = endIdx + 1
      continue
    }

    if (line.startsWith("$var")) {
      const parts = line.split(/\s+/)
      if (parts.length >= 4) {
        const sigType = parts[1]
        const width = parseInt(parts[2]) || 1
        const id = parts[3]
        const name = parts.slice(4).join(" ").replace("$end", "").trim()
        signals.push({ id, name, width, type: sigType })
        changes.set(id, [])
      }
      i++
      continue
    }

    if (line.startsWith("$enddefinitions")) { i++; continue }
    if (line.startsWith("$")) { i++; continue }

    if (line.startsWith("#")) {
      currentTime = parseInt(line.slice(1)) || 0
      i++
      continue
    }

    if (line.startsWith("b") || line.startsWith("B")) {
      const spaceIdx = line.indexOf(" ")
      if (spaceIdx > 0) {
        const value = line.slice(1, spaceIdx)
        const id = line.slice(spaceIdx + 1).trim()
        changes.get(id)?.push({ time: currentTime, value })
      }
      i++
      continue
    }

    if (line.startsWith("r") || line.startsWith("R")) {
      // real-type value: "r<float> <id>" — capture as string for step rendering
      const spaceIdx = line.indexOf(" ")
      if (spaceIdx > 0) {
        const value = line.slice(1, spaceIdx)
        const id = line.slice(spaceIdx + 1).trim()
        changes.get(id)?.push({ time: currentTime, value })
      }
      i++
      continue
    }

    if (line.length >= 2) {
      const value = line[0]
      const id = line.slice(1).trim()
      if (id && !id.includes(" ") && /^[!-~]+$/.test(id)) {
        changes.get(id)?.push({ time: currentTime, value })
      }
    }
    i++
  }

  const duration = signals.reduce((max, sig) => {
    const ch = changes.get(sig.id)
    return ch && ch.length > 0 ? Math.max(max, ch[ch.length - 1].time) : max
  }, 0)

  return { timescale, signals, changes, duration }
}

const resolveSignalSource = async (sessionId: string, workspaceRoot: string, sigName: string) => {
  const parts = sigName.split(".")
  const baseName = parts[parts.length - 1]

  try {
    const res = await callAgenticTool(
      "workspace",
      { session_id: sessionId, workspace_root: workspaceRoot },
      { action: "search", pattern: baseName }
    )

    if (!res.success || !res.result) return null

    const matches = res.result.split("\n").filter(Boolean)
    for (const match of matches) {
      const tokens = match.split(":")
      if (tokens.length < 3) continue
      const relativePath = tokens[0].trim()
      const lineNum = parseInt(tokens[1].trim(), 10)
      const content = tokens.slice(2).join(":").trim()

      if (!relativePath.endsWith(".v") && !relativePath.endsWith(".sv")) continue

      const isDecl = new RegExp(`\\b(wire|reg|logic|input|output|inout)\\b.*\\b${baseName}\\b`).test(content)
      if (isDecl) {
        return { path: relativePath, line: lineNum }
      }
    }
  } catch (e) {
    console.error("Error resolving signal source:", e)
  }
  return null
}

export function SessionWaveformTab(props: { path: string }) {
  const { params, tabs } = useSessionLayout()
  const file = useFile()
  const [fetchError, setFetchError] = createSignal<string | null>(null)

  // Read initial filter from global cache
  const initialFilter = waveformFilterCache.get(props.path) || ""
  const [filterText, setFilterText] = createSignal(initialFilter)

  // Persist filter query reactively
  createEffect(() => {
    waveformFilterCache.set(props.path, filterText())
  })

  const openTab = (tabPath: string) => {
    // 1. Fetch file content from backend to load it into cache
    file.load(tabPath)

    // 2. Open and activate tab in editor
    const tab = file.tab(tabPath)
    tabs().open(tab)
    tabs().setActive(tab)
  }

  // Track both session ID and file path reactively
  const [vcdData] = createResource(
    () => ({ sessionId: params.id, path: props.path }),
    async ({ sessionId, path }) => {
      setFetchError(null)
      try {
        if (!sessionId) throw new Error("Session is not ready")
        const workspaceRoot = decode64(params.dir) ?? ""

        let filePath = path
        // Slice path dynamically based on standard design subfolders
        const subfolders = ["simulation/", "verification/", "rtl/", "schematic/"]
        for (const sub of subfolders) {
          const idx = filePath.indexOf(sub)
          if (idx !== -1) {
            filePath = filePath.slice(idx)
            break
          }
        }

        const res = await callAgenticTool(
          "workspace",
          { session_id: sessionId, workspace_root: workspaceRoot },
          { action: "read", path: filePath }
        )
        if (!res.success || !res.result) throw new Error(res.result || "Could not read file")
        const parsed = parseVCD(res.result)
        if (parsed.signals.length === 0) throw new Error("No signals found in VCD")
        return parsed
      } catch (e) {
        setFetchError(e instanceof Error ? e.message : String(e))
        return null
      }
    },
  )

  return (
    <div class="flex flex-col w-full h-full bg-background-stronger font-sans">
      <div class="flex items-center gap-2 px-3 py-1.5 border-b border-border-weaker-base text-12-regular text-text-strong shrink-0">
        <span class="truncate font-mono">{props.path}</span>

        <div class="relative ml-4 w-48">
          <input
            type="text"
            placeholder="Filter signals..."
            value={filterText()}
            onInput={(e) => setFilterText(e.currentTarget.value)}
            class="w-full h-6 px-2 text-11-regular bg-background-base text-text-strong rounded border border-border-weaker-base focus:outline-none focus:border-border-strong transition-colors"
          />
          <Show when={filterText()}>
            <button
              onClick={() => setFilterText("")}
              class="absolute right-1.5 top-1/2 -translate-y-1/2 text-text-weaker hover:text-text-strong text-12-bold"
            >
              ×
            </button>
          </Show>
        </div>

        <Show when={vcdData()}>
          <span class="text-text-weaker text-11-regular ml-auto shrink-0">
            {vcdData()!.signals.length} sigs &middot; {vcdData()!.duration} {vcdData()!.timescale}
          </span>
        </Show>
      </div>

      <Show when={vcdData.loading && !vcdData()}>
        <div class="flex-1 flex items-center justify-center text-12-regular text-text-weaker px-4 text-center">
          Parsing VCD transitions...
        </div>
      </Show>

      <Show when={fetchError()}>
        <div class="flex-1 flex items-center justify-center text-12-regular text-text-weaker px-4 text-center">
          {fetchError()}
        </div>
      </Show>

      <Show when={vcdData() && !vcdData.loading}>
        <WaveformCanvas
          data={vcdData()!}
          filterText={filterText()}
          openTab={openTab}
          file={file}
          tabPath={props.path}
        />
      </Show>
    </div>
  )
}

function WaveformCanvas(props: { data: VCDData; filterText: string; openTab: (path: string) => void; file: any; tabPath: string }) {
  const signals = () => props.data?.signals || []
  const changes = () => props.data?.changes || new Map()
  const duration = () => props.data?.duration || 0
  const timeRange = () => props.data?.duration || 1

  const signalHeight = 28
  const nameWidth = 200
  const pad = { top: 12, left: 16, right: 24, bottom: 20 }

  let canvasRef!: HTMLCanvasElement
  let containerRef!: HTMLDivElement

  // State
  const [zoom, setZoom] = createSignal(1)
  const [offsetX, setOffsetX] = createSignal(0)
  const [hoveredRow, setHoveredRow] = createSignal<number | null>(null)
  const [canvasWidth, setCanvasWidth] = createSignal(800)
  const [cursorTime, setCursorTime] = createSignal<number | null>(null)  // click cursor position in VCD time units

  const { params } = useSessionLayout()

  // Filter signals reactively
  const filteredSignals = createMemo(() => {
    const query = props.filterText.toLowerCase().trim()
    const sigs = signals()
    if (!query) return sigs
    return sigs.filter(sig => sig.name.toLowerCase().includes(query))
  })

  const canvasHeight = () => pad.top + filteredSignals().length * signalHeight + pad.bottom

  // Helper to clamp horizontal offset
  const clampOffset = (z: number, ox: number, width: number) => {
    const waveWidth = width - nameWidth - pad.left - pad.right
    const range = timeRange()
    if (range * z <= waveWidth) {
      return 0
    }
    const minOffset = waveWidth - range * z
    return Math.max(Math.min(0, ox), minOffset)
  }

  // Draw function
  const redraw = () => {
    const canvas = canvasRef
    if (!canvas) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return

    const w = canvasWidth()
    const h = canvasHeight()

    // Clear background
    ctx.fillStyle = "rgba(18,18,18,1)"
    ctx.fillRect(0, 0, w, h)

    const activeSignals = filteredSignals()

    // Highlight hovered row
    const hr = hoveredRow()
    if (hr !== null && hr >= 0 && hr < activeSignals.length) {
      ctx.fillStyle = "rgba(255,255,255,0.03)"
      ctx.fillRect(0, pad.top + hr * signalHeight, w, signalHeight)
    }

    const currentZoom = zoom()
    const currentOffset = offsetX()

    // Draw Grid Rows
    ctx.strokeStyle = "rgba(255,255,255,0.06)"
    ctx.lineWidth = 1
    for (let r = 0; r <= activeSignals.length; r++) {
      const y = pad.top + r * signalHeight
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke()
    }

    // Determine grid step
    const range = timeRange()
    const step = Math.max(Math.floor(range / 20), 1)
    const sx = nameWidth + pad.left
    const tsLabel = props.data.timescale || ""

    ctx.save()
    // Clip grid lines to the wave area
    ctx.beginPath()
    ctx.rect(sx, 0, w - sx, h)
    ctx.clip()

    for (let t = 0; t <= range; t += step) {
      const x = sx + t * currentZoom + currentOffset
      if (x < sx || x > w) continue
      ctx.strokeStyle = "rgba(255,255,255,0.06)"
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke()

      if (t % (step * 2) === 0) {
        ctx.fillStyle = "rgba(255,255,255,0.2)"
        ctx.font = '9px "JetBrainsMono Nerd Font Mono", monospace'
        ctx.textBaseline = "middle"
        ctx.fillText(`${t} ${tsLabel}`, x + 2, h - 10)
      }
    }

    // Draw cursor line
    const ct = cursorTime()
    if (ct !== null) {
      const cx = sx + ct * currentZoom + currentOffset
      if (cx >= sx && cx <= w) {
        ctx.strokeStyle = "rgba(255,80,80,0.85)"
        ctx.lineWidth = 1.5
        ctx.setLineDash([4, 3])
        ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, h); ctx.stroke()
        ctx.setLineDash([])
        // Time label at top of cursor
        ctx.fillStyle = "rgba(255,80,80,0.95)"
        ctx.font = 'bold 9px "JetBrainsMono Nerd Font Mono", monospace'
        ctx.textBaseline = "top"
        ctx.textAlign = "left"
        ctx.fillText(`▶ ${ct} ${tsLabel}`, cx + 3, 2)
        ctx.textAlign = "left"
      }
    }

    ctx.restore()

    const currentChanges = changes()

    // Draw Waves
    for (let i = 0; i < activeSignals.length; i++) {
      const sig = activeSignals[i]
      const y = pad.top + i * signalHeight
      const cy = y + signalHeight / 2
      const hy = cy - 7
      const ly = cy + 7

      ctx.save()
      // Draw signal name
      ctx.fillStyle = "rgba(255,255,255,0.7)"
      ctx.font = '10px "JetBrainsMono Nerd Font Mono", monospace'
      ctx.textBaseline = "middle"
      ctx.textAlign = "left"
      ctx.fillText(sig.name, pad.left, cy)

      // Clip waves to the wave area
      ctx.beginPath()
      ctx.rect(sx, y + 1, w - sx, signalHeight - 2)
      ctx.clip()

      const ch = currentChanges.get(sig.id) || []
      const exWave = sx + range * currentZoom + currentOffset

      if (ch.length === 0) {
        ctx.fillStyle = "rgba(255,255,255,0.15)"
        ctx.fillRect(sx, cy - 1, exWave - sx, 2)
      } else {
        if (sig.width === 1) {
          for (let idx = 0; idx < ch.length; idx++) {
            const c = ch[idx]
            const x = sx + c.time * currentZoom + currentOffset
            // VLSI standard: 1=green HIGH, 0=grey LOW, x=orange UNKNOWN, z=purple HI-Z
            const vLow = c.value.toLowerCase()
            const isX = vLow === "x"
            const isZ = vLow === "z"
            const hi = c.value === "1" || vLow === "h"
            const curY = (isX || isZ) ? cy : (hi ? hy : ly)

            const sigColor = isX ? "rgba(255,160,50,0.9)"      // X=orange (unknown)
                           : isZ ? "rgba(180,100,255,0.9)"     // Z=purple (hi-z)
                           : hi  ? "rgba(67,181,129,0.8)"      // 1=green
                           :       "rgba(255,255,255,0.35)"    // 0=grey

            if (idx > 0) {
              const px = sx + ch[idx - 1].time * currentZoom + currentOffset
              const prevVLow = ch[idx - 1].value.toLowerCase()
              const wasX = prevVLow === "x"
              const wasZ = prevVLow === "z"
              const wasHi = ch[idx - 1].value === "1" || prevVLow === "h"
              const prevY = (wasX || wasZ) ? cy : (wasHi ? hy : ly)
              const prevColor = wasX ? "rgba(255,160,50,0.9)" : wasZ ? "rgba(180,100,255,0.9)" : wasHi ? "rgba(67,181,129,0.8)" : "rgba(255,255,255,0.35)"
              ctx.strokeStyle = prevColor
              ctx.lineWidth = 1.5
              ctx.beginPath(); ctx.moveTo(px, prevY); ctx.lineTo(x, prevY); ctx.stroke()
              // Draw vertical transition edge
              ctx.strokeStyle = "rgba(255,255,255,0.4)"
              ctx.lineWidth = 1
              ctx.beginPath(); ctx.moveTo(x, prevY); ctx.lineTo(x, curY); ctx.stroke()
            }

            const nx = idx < ch.length - 1 ? sx + ch[idx + 1].time * currentZoom + currentOffset : exWave
            ctx.strokeStyle = sigColor
            ctx.lineWidth = 1.5
            ctx.beginPath(); ctx.moveTo(x, curY); ctx.lineTo(nx, curY); ctx.stroke()
          }
        } else {
          // Bus rendering
          for (let idx = 0; idx < ch.length; idx++) {
            const c = ch[idx]
            const x = sx + c.time * currentZoom + currentOffset
            const nx = idx < ch.length - 1 ? sx + ch[idx + 1].time * currentZoom + currentOffset : exWave
            const mx = (x + nx) / 2

            // VLSI bus colors: X=orange (any bit unknown), Z=purple (hi-z), else teal
            const valLow = c.value.toLowerCase()
            const busHasX = valLow.includes("x")
            const busHasZ = !busHasX && valLow.includes("z")
            const busColor = busHasX ? "rgba(255,160,50,0.85)"
                           : busHasZ ? "rgba(180,100,255,0.85)"
                           :           "rgba(100,200,255,0.8)"

            // Draw bus outline (hexagonal ends)
            const diagW = Math.min(6, (nx - x) / 4)
            ctx.strokeStyle = busColor
            ctx.lineWidth = 1
            ctx.beginPath()
            ctx.moveTo(x + diagW, hy)
            ctx.lineTo(nx - diagW, hy)
            ctx.lineTo(nx, cy)
            ctx.lineTo(nx - diagW, ly)
            ctx.lineTo(x + diagW, ly)
            ctx.lineTo(x, cy)
            ctx.closePath()
            ctx.stroke()

            // Draw bus value text
            if (nx - x > 20) {
              ctx.fillStyle = busHasX ? "rgba(255,200,100,0.9)" : busHasZ ? "rgba(200,150,255,0.9)" : "rgba(255,255,255,0.85)"
              ctx.font = '9px "JetBrainsMono Nerd Font Mono", monospace'
              ctx.textBaseline = "middle"
              ctx.textAlign = "center"
              const textVal = c.value.length > 8 ? c.value.slice(0, 8) + ".." : c.value
              ctx.fillText(textVal, mx, cy)
            }
          }
        }
      }
      ctx.restore()
    }
  }

  // Effect to handle canvas resizing (only runs when width or height changes)
  createEffect(() => {
    const w = canvasWidth()
    const h = canvasHeight()
    const canvas = canvasRef
    if (canvas) {
      const dpr = window.devicePixelRatio || 1
      canvas.width = w * dpr
      canvas.height = h * dpr
      const ctx = canvas.getContext("2d")
      if (ctx) {
        ctx.resetTransform()
        ctx.scale(dpr, dpr)
      }
    }
    // Redraw without registering active zoom/offset dependencies on resize effect
    untrack(() => redraw())
  })

  // Effect to redraw on zoom, offset, hover, or cursor changes
  createEffect(() => {
    zoom()
    offsetX()
    hoveredRow()
    canvasWidth()
    canvasHeight()
    cursorTime()
    redraw()
  })


  // Handle Resize
  const updateSize = () => {
    const container = containerRef
    if (!container) return
    const w = container.clientWidth

    // Auto-fit on first load or resize
    if (w !== canvasWidth()) {
      const waveWidth = w - nameWidth - pad.left - pad.right
      const range = timeRange()
      const idealZoom = waveWidth / range
      setZoom(idealZoom)
      setOffsetX(0)
      setCanvasWidth(w)
    }
  }

  onMount(() => {
    updateSize()
    window.addEventListener("resize", updateSize)

    // Resize Observer for container
    const ro = new ResizeObserver(() => updateSize())
    ro.observe(containerRef)

    onCleanup(() => {
      window.removeEventListener("resize", updateSize)
      ro.disconnect()
    })
  })

  // Drag Panning State
  let isDragging = false
  let startX = 0
  let startOffset = 0
  let dragMovedPx = 0  // track drag distance to distinguish click from pan

  const handleMouseDown = (e: MouseEvent) => {
    const rect = canvasRef.getBoundingClientRect()
    const x = e.clientX - rect.left
    if (x < nameWidth + pad.left) return

    isDragging = true
    dragMovedPx = 0
    startX = e.clientX
    startOffset = offsetX()
  }

  const handleMouseMove = (e: MouseEvent) => {
    const rect = canvasRef.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top

    const activeSignals = filteredSignals()
    const row = Math.floor((y - pad.top) / signalHeight)
    if (row >= 0 && row < activeSignals.length) {
      setHoveredRow(row)
    } else {
      setHoveredRow(null)
    }

    if (isDragging) {
      const dx = e.clientX - startX
      dragMovedPx = Math.abs(dx)
      const nextOffset = startOffset + dx
      setOffsetX(clampOffset(zoom(), nextOffset, canvasWidth()))
    }
  }

  const handleMouseUp = (e: MouseEvent) => {
    if (isDragging && dragMovedPx < 4) {
      // Treat as click: place cursor at this time
      const rect = canvasRef.getBoundingClientRect()
      const x = e.clientX - rect.left
      const sx = nameWidth + pad.left
      if (x >= sx) {
        const timeVal = Math.round((x - sx - offsetX()) / zoom())
        setCursorTime(Math.max(0, Math.min(timeVal, timeRange())))
      }
    }
    isDragging = false
  }

  const handleMouseLeave = () => {
    isDragging = false
    setHoveredRow(null)
  }

  const handleWheel = (e: WheelEvent) => {
    const rect = canvasRef.getBoundingClientRect()
    const mouseX = e.clientX - rect.left

    // Zooming with Ctrl + Wheel
    if (e.ctrlKey) {
      e.preventDefault()
      const waveX = mouseX - nameWidth - pad.left
      const timeAtMouse = (waveX - offsetX()) / zoom()

      const factor = e.deltaY < 0 ? 1.25 : 0.8
      let nextZoom = zoom() * factor
      const waveWidth = canvasWidth() - nameWidth - pad.left - pad.right
      const range = timeRange()
      const minZoom = waveWidth / range
      const maxZoom = 10
      nextZoom = Math.max(minZoom, Math.min(maxZoom, nextZoom))

      const nextOffset = waveX - timeAtMouse * nextZoom
      setZoom(nextZoom)
      setOffsetX(clampOffset(nextZoom, nextOffset, canvasWidth()))
    } else if (e.shiftKey) {
      // Horizontal Pan with Shift + Wheel
      e.preventDefault()
      const nextOffset = offsetX() - e.deltaY
      setOffsetX(clampOffset(zoom(), nextOffset, canvasWidth()))
    }
  }

  // Double Click RTL Cross-Probing
  const handleDblClick = async (e: MouseEvent) => {
    const rect = canvasRef.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top

    const activeSignals = filteredSignals()
    const row = Math.floor((y - pad.top) / signalHeight)
    if (row >= 0 && row < activeSignals.length) {
      const sig = activeSignals[row]

      showToast({
        title: `Searching driver for signal: "${sig.name}"...`,
        description: "Scanning Verilog source files...",
      })

      const match = await resolveSignalSource(
        params.id!,
        decode64(params.dir) ?? "",
        sig.name
      )

      if (match) {
        let resolvedPath = match.path

        let prefix = ""
        const subfolders = ["simulation/", "verification/", "rtl/", "schematic/"]
        for (const sub of subfolders) {
          const idx = props.tabPath.indexOf(sub)
          if (idx > 0) {
            prefix = props.tabPath.slice(0, idx)
            break
          }
        }

        if (prefix && !resolvedPath.startsWith(prefix)) {
          resolvedPath = prefix + resolvedPath
        }

        showToast({
          title: "Signal Driver Located!",
          description: `Opened ${resolvedPath.split("/").pop()} at line ${match.line}`,
        })
        props.openTab(resolvedPath)
        props.file.setSelectedLines(resolvedPath, { start: match.line, end: match.line })
      } else {
        showToast({
          title: "Search Complete",
          description: `Could not find Verilog declaration for: "${sig.name}"`,
        })
      }
    }
  }

  return (
    <div ref={containerRef!} class="flex-1 overflow-auto relative h-full bg-background-stronger">
      <div
        class="absolute top-0 right-4 bg-background-base/80 backdrop-blur px-2 py-1 text-10-regular text-text-weaker rounded-md border border-border-weaker-base pointer-events-none z-10"
        style="margin-top: 6px;"
      >
        Click to Place Cursor &middot; Drag to Pan &middot; Ctrl+Scroll to Zoom &middot; Dbl-Click to Cross-Probe RTL
      </div>
      <canvas
        ref={canvasRef!}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseLeave}
        onDblClick={handleDblClick}
        onWheel={handleWheel}
        class="block cursor-grab active:cursor-grabbing"
        style={{
          width: `${canvasWidth()}px`,
          height: `${canvasHeight()}px`
        }}
      />
    </div>
  )
}
