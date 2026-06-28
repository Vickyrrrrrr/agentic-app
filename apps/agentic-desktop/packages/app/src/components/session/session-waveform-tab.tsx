import { createResource, createSignal, onMount, Show } from "solid-js"
import { useSessionLayout } from "@/pages/session/session-layout"
import { callAgenticTool } from "@/utils/agentic"

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

    if (line.startsWith("$enddefinitions")) { i++; break }
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

    if (line.startsWith("r") || line.startsWith("R")) { i++; continue }

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

export function SessionWaveformTab(props: { path: string }) {
  const { params } = useSessionLayout()
  const [fetchError, setFetchError] = createSignal<string | null>(null)

  const [vcdData] = createResource(
    () => params.id,
    async (sessionId) => {
      setFetchError(null)
      try {
        const res = await callAgenticTool("workspace", { session_id: sessionId }, { action: "read", path: props.path })
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
      <div class="flex items-center gap-2 px-3 py-1.5 border-b border-border-weaker-base text-12-regular text-text-strong">
        <span class="truncate font-mono">{props.path}</span>
        <Show when={vcdData()}>
          <span class="text-text-weaker text-11-regular ml-auto shrink-0">
            {vcdData()!.signals.length} sigs &middot; {vcdData()!.duration} {vcdData()!.timescale}
          </span>
        </Show>
      </div>

      <Show when={vcdData.loading && !vcdData()}>
        <div class="flex-1 flex items-center justify-center text-12-regular text-text-weak">
          Parsing VCD transitions...
        </div>
      </Show>

      <Show when={fetchError()}>
        <div class="flex-1 flex items-center justify-center text-12-regular text-text-weaker px-4 text-center">
          {fetchError()}
        </div>
      </Show>

      <Show when={vcdData() && !vcdData.loading}>
        <WaveformCanvas data={vcdData()!} />
      </Show>
    </div>
  )
}

function WaveformCanvas(props: { data: VCDData }) {
  const { signals, changes, duration } = props.data
  const signalHeight = 28
  const nameWidth = 200
  const pad = { top: 12, left: 16, right: 24 }
  const displaySignals = signals.slice(0, 64)
  const timeRange = duration || 1
  const timeScale = 1

  let canvasRef!: HTMLCanvasElement
  let containerRef!: HTMLDivElement

  const write = (ctx: CanvasRenderingContext2D, text: string, x: number, y: number, color = "rgba(255,255,255,0.618)", size = 10) => {
    ctx.fillStyle = color
    ctx.font = `${size}px "JetBrainsMono Nerd Font Mono", monospace`
    ctx.textBaseline = "middle"
    ctx.fillText(text, x, y)
  }

  const drawGrid = (ctx: CanvasRenderingContext2D, w: number, h: number, n: number) => {
    ctx.strokeStyle = "rgba(255,255,255,0.06)"
    ctx.lineWidth = 1
    for (let r = 0; r <= n; r++) {
      const y = pad.top + r * signalHeight
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke()
    }
    const step = Math.max(Math.floor(timeRange / 20), 1)
    for (let t = 0; t <= timeRange; t += step) {
      const x = nameWidth + pad.left + t * timeScale
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke()
      if (t % (step * 5) === 0) write(ctx, `${t}`, x + 2, h - 4, "rgba(255,255,255,0.284)", 9)
    }
  }

  const drawWave = (ctx: CanvasRenderingContext2D, sig: VCDSignal, y: number, w: number) => {
    const ch = changes.get(sig.id) || []
    const cy = y + signalHeight / 2
    const hy = cy - 7
    const ly = cy + 7
    const sx = nameWidth + pad.left
    const ex = sx + timeRange * timeScale

    ctx.save()
    ctx.beginPath()
    ctx.rect(sx, y + 1, w - sx, signalHeight - 2)
    ctx.clip()

    write(ctx, sig.name, pad.left, cy, "rgba(255,255,255,0.618)", 10)

    if (ch.length === 0) {
      ctx.fillStyle = "rgba(255,255,255,0.195)"
      ctx.fillRect(sx, cy - 1, ex - sx, 2)
      ctx.restore()
      return
    }

    if (sig.width === 1) {
      for (let i = 0; i < ch.length; i++) {
        const c = ch[i]
        const x = sx + c.time * timeScale
        const hi = c.value === "1" || c.value === "h"
        const curY = hi ? hy : ly

        if (i > 0) {
          const px = sx + ch[i - 1].time * timeScale
          const wasHi = ch[i - 1].value === "1" || ch[i - 1].value === "h"
          const prevY = wasHi ? hy : ly
          ctx.strokeStyle = wasHi ? "rgba(67,181,129,0.8)" : "rgba(255,255,255,0.422)"
          ctx.lineWidth = 1.5
          ctx.beginPath(); ctx.moveTo(px, prevY); ctx.lineTo(x, prevY); ctx.stroke()
          if (wasHi !== hi) {
            ctx.strokeStyle = "rgba(255,255,255,0.618)"
            ctx.lineWidth = 1
            ctx.beginPath(); ctx.moveTo(x, prevY); ctx.lineTo(x, curY); ctx.stroke()
          }
        }

        const nx = i < ch.length - 1 ? sx + ch[i + 1].time * timeScale : ex
        ctx.strokeStyle = hi ? "rgba(67,181,129,0.8)" : "rgba(255,255,255,0.422)"
        ctx.lineWidth = 1.5
        ctx.beginPath(); ctx.moveTo(x, curY); ctx.lineTo(nx, curY); ctx.stroke()
      }
    } else {
      for (let i = 0; i < ch.length; i++) {
        const c = ch[i]
        const x = sx + c.time * timeScale
        const nx = i < ch.length - 1 ? sx + ch[i + 1].time * timeScale : ex
        const mx = (x + nx) / 2
        ctx.fillStyle = "rgba(67,181,129,0.8)"
        ctx.font = '9px "JetBrainsMono Nerd Font Mono", monospace'
        ctx.textBaseline = "middle"
        ctx.textAlign = "center"
        ctx.fillText(c.value.length > 8 ? c.value.slice(0, 8) + ".." : c.value, mx, cy)
      }
    }

    ctx.restore()
  }

  onMount(() => {
    const container = containerRef
    if (!container) return
    const w = container.clientWidth
    const h = container.clientHeight
    const canvas = canvasRef
    if (!canvas) return
    canvas.width = w * window.devicePixelRatio
    canvas.height = h * window.devicePixelRatio
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio)

    ctx.fillStyle = "rgba(18,18,18,1)"
    ctx.fillRect(0, 0, w, h)

    drawGrid(ctx, w, h, displaySignals.length)
    for (let i = 0; i < displaySignals.length; i++) {
      drawWave(ctx, displaySignals[i], pad.top + i * signalHeight, w)
    }
  })

  return (
    <div ref={containerRef!} class="flex-1 overflow-auto relative">
      <canvas ref={canvasRef!} class="block w-full h-full" />
    </div>
  )
}
