<template>
  <canvas ref="cvs" class="dag-canvas" @mousemove="onMouseMove"></canvas>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, nextTick } from 'vue'
import { useDAGStore } from '../store/dag'
const store = useDAGStore()
const cvs = ref<HTMLCanvasElement>()

const STATUS_COLORS: Record<string, string> = {
  PENDING: '#4a5568', RUNNING: '#3182ce', SUCCESS: '#38a169', FAILED: '#e53e3e',
  TIMEOUT: '#d69e2e', INTERRUPTED: '#c9a86a'
}

function draw() {
  const c = cvs.value!; c.width = c.clientWidth; c.height = c.clientHeight
  const ctx = c.getContext('2d')!; const W = c.width, H = c.height
  ctx.fillStyle = '#0f0f23'; ctx.fillRect(0, 0, W, H)

  const wf = store.execution?.workflow || store.workflow
  if (!wf) return

  const nodes = wf.nodes
  const nodePos: Record<string, {x:number, y:number}> = {}
  nodes.forEach(n => { nodePos[n.id] = { x: 80 + n.x * 80, y: 60 + n.y * 80 } })

  // Draw edges
  wf.edges.forEach(([u, v]) => {
    const a = nodePos[u], b = nodePos[v]
    if (!a || !b) return
    ctx.strokeStyle = '#2a2a4a'; ctx.lineWidth = 2
    ctx.beginPath(); ctx.moveTo(a.x, a.y)
    // Draw bezier curve
    const mx = (a.x + b.x) / 2
    ctx.bezierCurveTo(mx, a.y, mx, b.y, b.x, b.y)
    ctx.stroke()

    // Arrow head
    const angle = Math.atan2(b.y - Math.max(a.y, b.y - 20), b.x - a.x)
    const arrowSize = 8
    ctx.fillStyle = '#2a2a4a'
    ctx.beginPath()
    ctx.moveTo(b.x, b.y)
    ctx.lineTo(b.x - arrowSize * Math.cos(angle - 0.5), b.y - arrowSize * Math.sin(angle - 0.5))
    ctx.lineTo(b.x - arrowSize * Math.cos(angle + 0.5), b.y - arrowSize * Math.sin(angle + 0.5))
    ctx.fill()
  })

  // Draw nodes
  nodes.forEach(n => {
    const {x, y} = nodePos[n.id]
    const color = STATUS_COLORS[n.status] || '#4a5568'

    // Glow for running
    if (n.status === 'RUNNING') {
      ctx.shadowColor = color; ctx.shadowBlur = 15
    }

    // Node box
    const rw = 120, rh = 44, rx = x - rw/2, ry = y - rh/2
    ctx.fillStyle = '#1a1a2e'; ctx.strokeStyle = color; ctx.lineWidth = 2
    ctx.beginPath(); roundRect(ctx, rx, ry, rw, rh, 6); ctx.fill(); ctx.stroke()
    ctx.shadowBlur = 0

    // Status bar at top
    ctx.fillStyle = color
    ctx.beginPath(); ctx.moveTo(rx+6, ry); ctx.lineTo(rx+rw-6, ry); ctx.lineTo(rx+rw-6, ry+4); ctx.lineTo(rx+6, ry+4); ctx.fill()

    // Text
    ctx.fillStyle = '#e0e0e0'; ctx.font = 'bold 11px system-ui'; ctx.textAlign = 'center'
    ctx.fillText(n.name, x, y - 2)
    ctx.fillStyle = '#888'; ctx.font = '9px monospace'
    ctx.fillText(`${n.status} | 重试${n.retries}`, x, y + 14)
    ctx.textAlign = 'start'

    // Duration
    if (n.startTime && n.endTime) {
      ctx.font = '8px monospace'; ctx.fillStyle = '#666'
      ctx.fillText(`${(n.endTime - n.startTime).toFixed(1)}s`, rx + 4, ry + rh - 4)
    }
  })
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.moveTo(x+r, y); ctx.lineTo(x+w-r, y); ctx.arcTo(x+w, y, x+w, y+r, r)
  ctx.lineTo(x+w, y+h-r); ctx.arcTo(x+w, y+h, x+w-r, y+h, r)
  ctx.lineTo(x+r, y+h); ctx.arcTo(x, y+h, x, y+h-r, r)
  ctx.lineTo(x, y+r); ctx.arcTo(x, y, x+r, y, r)
}

function onMouseMove(e: MouseEvent) {}

onMounted(() => { nextTick(draw) })
watch(() => [store.workflow, store.execution], draw, { deep: true })
</script>

<style scoped>
.dag-canvas { width: 100%; height: 100%; display: block; }
</style>