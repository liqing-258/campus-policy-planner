<script setup lang="ts">
/**
 * 鼠标气泡：跟随指针的粒子轨迹 + 点击按钮时的破裂效果。
 *
 * ## 三条实现约束
 *
 * **1. 只画在 canvas 上，不碰 DOM。** 每次移动都插一个 `<div>` 的做法在 60Hz 下
 * 会持续触发样式重算，滚动的列表会跟着掉帧。canvas 只有一次 `drawImage` 级别的开销。
 *
 * **2. 指针事件一律 `pointer-events: none`。** 这层铺在整页之上，一旦能接收事件，
 * 所有按钮就点不动了——这类"装饰层挡住交互"的 bug 在真机上极难发现。
 *
 * **3. 会自己关掉。** `prefers-reduced-motion`（眩晕敏感）与触屏（`pointer: coarse`，
 * 手机上没有悬停，跟着手指冒泡只是耗电）都不启用；组件卸载时释放 rAF 与监听。
 *
 * ## 为什么粒子是"环 + 高光"而不是实心圆
 *
 * 实心圆在深色底上看起来像噪点。气泡要有边界感：低透明度填充 + 一圈描边 +
 * 左上角一个更亮的小弧，才像"泡"。这也是它和背景光晕不打架的原因。
 */
import { onBeforeUnmount, onMounted } from 'vue'

interface Bubble {
  x: number
  y: number
  vx: number
  vy: number
  r: number
  /** 0..1 的生命进度，1 表示刚生成 */
  life: number
  decay: number
  tint: [number, number, number]
  /** true = 点击炸开的，扩散更快、消失更快 */
  burst: boolean
}

const TINTS: [number, number, number][] = [
  [47, 199, 180], // 青碧（主色）
  [233, 196, 106], // 暖砂
  [231, 241, 238], // 近白
  [111, 182, 232], // 天青
]

let canvas: HTMLCanvasElement | null = null
let ctx: CanvasRenderingContext2D | null = null
let raf = 0
let bubbles: Bubble[] = []
let lastSpawn = 0
let width = 0
let height = 0
let dpr = 1
let enabled = false

const MAX_BUBBLES = 140
/** 两次生成之间的最小间隔（ms）。不限的话 120Hz 鼠标会瞬间堆满上限 */
const SPAWN_INTERVAL = 38

function resize() {
  if (!canvas || !ctx) return
  dpr = Math.min(window.devicePixelRatio || 1, 2)
  width = window.innerWidth
  height = window.innerHeight
  canvas.width = Math.floor(width * dpr)
  canvas.height = Math.floor(height * dpr)
  canvas.style.width = `${width}px`
  canvas.style.height = `${height}px`
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
}

function pick(): [number, number, number] {
  return TINTS[Math.floor(Math.random() * TINTS.length)]
}

function spawn(x: number, y: number, burst: boolean) {
  if (bubbles.length >= MAX_BUBBLES) bubbles.shift()
  const tint = pick()
  bubbles.push({
    x: x + (Math.random() - 0.5) * (burst ? 4 : 10),
    y: y + (Math.random() - 0.5) * (burst ? 4 : 10),
    vx: burst ? (Math.random() - 0.5) * 5.2 : (Math.random() - 0.5) * 0.35,
    vy: burst ? (Math.random() - 0.5) * 5.2 : -0.12 - Math.random() * 0.28,
    r: burst ? 2.5 + Math.random() * 4.5 : 2 + Math.random() * 4.6,
    life: 1,
    decay: burst ? 0.028 + Math.random() * 0.02 : 0.010 + Math.random() * 0.010,
    tint,
    burst,
  })
}

function draw() {
  raf = 0
  if (!ctx) return
  ctx.clearRect(0, 0, width, height)

  for (let i = bubbles.length - 1; i >= 0; i -= 1) {
    const b = bubbles[i]
    // 非爆裂的气泡几乎不减速：它要"挂"在指针后面慢慢升，不是飞出去
    b.x += b.vx
    b.y += b.vy
    b.vy -= b.burst ? 0.06 : 0.002
    b.vx *= 0.985
    b.vy *= 0.985
    b.life -= b.decay
    b.r += b.burst ? 0.12 : 0.045

    if (b.life <= 0) {
      bubbles.splice(i, 1)
      continue
    }

    const alpha = Math.min(1, b.life) * (b.burst ? 0.85 : 0.6)
    const [r, g, bl] = b.tint

    // 填充：中间几乎透明，边缘略实——气泡是靠"壳"被看到的
    const grad = ctx.createRadialGradient(b.x, b.y, b.r * 0.2, b.x, b.y, b.r)
    grad.addColorStop(0, `rgba(${r},${g},${bl},${alpha * 0.05})`)
    grad.addColorStop(1, `rgba(${r},${g},${bl},${alpha * 0.22})`)
    ctx.beginPath()
    ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2)
    ctx.fillStyle = grad
    ctx.fill()

    // 壳
    ctx.beginPath()
    ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2)
    ctx.strokeStyle = `rgba(${r},${g},${bl},${alpha * 0.55})`
    ctx.lineWidth = 1
    ctx.stroke()

    // 左上高光：一个 70° 的小弧，气泡的"玻璃感"全在这一笔
    if (b.r > 2.4) {
      ctx.beginPath()
      ctx.arc(b.x, b.y, b.r * 0.72, Math.PI * 1.05, Math.PI * 1.62)
      ctx.strokeStyle = `rgba(255,255,255,${alpha * 0.5})`
      ctx.lineWidth = Math.max(0.7, b.r * 0.22)
      ctx.stroke()
    }
  }

  if (bubbles.length) raf = requestAnimationFrame(draw)
}

function ensureLoop() {
  if (!raf && bubbles.length) raf = requestAnimationFrame(draw)
}

function onMove(event: PointerEvent) {
  if (!enabled) return
  const now = performance.now()
  if (now - lastSpawn < SPAWN_INTERVAL) return
  lastSpawn = now
  spawn(event.clientX, event.clientY, false)
  ensureLoop()
}

/** 只有"能点的东西"才炸。整页乱炸会把视觉重心从按钮上抢走 */
const CLICKABLE = 'button, a, [role="button"], input[type="submit"], .btn, .nav-item, .tab, .chip'

function onDown(event: PointerEvent) {
  if (!enabled) return
  const target = event.target as Element | null
  if (!target || !target.closest) return
  if (!target.closest(CLICKABLE)) return
  const count = 14 + Math.floor(Math.random() * 6)
  for (let i = 0; i < count; i += 1) spawn(event.clientX, event.clientY, true)
  ensureLoop()
}

function onLeave() {
  // 指针离开窗口：把非爆裂粒子加速清掉，回来时不留一坨旧泡
  for (const bubble of bubbles) bubble.decay = Math.max(bubble.decay, 0.05)
  ensureLoop()
}

onMounted(() => {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const fine = window.matchMedia('(pointer: fine)').matches
  if (reduce || !fine) return

  canvas = document.createElement('canvas')
  canvas.setAttribute('aria-hidden', 'true')
  canvas.style.cssText =
    'position:fixed;inset:0;pointer-events:none;z-index:60;opacity:.95'
  document.body.appendChild(canvas)
  ctx = canvas.getContext('2d')
  if (!ctx) return

  enabled = true
  resize()
  window.addEventListener('resize', resize)
  window.addEventListener('pointermove', onMove, { passive: true })
  window.addEventListener('pointerdown', onDown, { passive: true })
  document.addEventListener('pointerleave', onLeave)
})

onBeforeUnmount(() => {
  enabled = false
  cancelAnimationFrame(raf)
  raf = 0
  bubbles = []
  window.removeEventListener('resize', resize)
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerdown', onDown)
  document.removeEventListener('pointerleave', onLeave)
  canvas?.remove()
  canvas = null
  ctx = null
})
</script>

<template>
  <!-- 纯副作用组件：不产出可见节点，canvas 由脚本挂到 body -->
  <span hidden aria-hidden="true" />
</template>
