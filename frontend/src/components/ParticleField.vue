<script setup lang="ts">
/**
 * 落地页背景：漂浮微粒 + 邻近连线。
 *
 * ## 为什么用 canvas 而不是 DOM 粒子
 *
 * 60 个会动的 DOM 节点，每个都要让浏览器做一次布局 + 合成，在低端机上是稳定掉帧的
 * 来源；canvas 只有一个节点，粒子只改两个浮点数。代价是要自己算命中与连线，
 * 但那点算术比 60 次 style 写入便宜得多。
 *
 * ## 三个必须处理的细节
 *
 * 1. **devicePixelRatio**：canvas 的 CSS 尺寸与位图尺寸是两回事。只设 `width/height`
 *    属性不设 `style.width/height`，在高分屏上会糊成一片；反过来只设样式，
 *    图形会被拉大两倍。两个都设，再 `ctx.scale(dpr, dpr)`。
 * 2. **页面不可见时停掉**：用户切到别的标签页，`requestAnimationFrame` 会被浏览器
 *    降到 1fps 但不会停。用 `visibilitychange` 主动停，回来再续——
 *    这是"关掉一个标签页风扇就安静"和"一直在响"的区别。
 * 3. **prefers-reduced-motion**：动效偏好为"减少"时**不建 canvas**（返回后组件
 *    什么都不渲染），而不是画一个不动的。静态粒子云看起来像脏点，不如没有。
 *
 * ## 鼠标排斥但不需要专门的事件优化
 *
 * 指针位置只记在变量里，每帧读一次；不 `preventDefault`、不设 `passive: false`，
 * 更不挂在 canvas 上（canvas 是 `pointer-events: none`，挂在 window）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 粒子数量。1080p 下 62 个连线密度刚好，再多会变蜘蛛网 */
    count?: number
    /** 连线的最大距离（px），超过就不画 */
    linkDistance?: number
    /** 鼠标影响半径（px） */
    repelRadius?: number
  }>(),
  { count: 62, linkDistance: 132, repelRadius: 110 },
)

const canvas = ref<HTMLCanvasElement | null>(null)
/** 动效偏好为"减少"时不渲染 canvas */
const enabled = ref(true)

interface P {
  x: number
  y: number
  vx: number
  vy: number
  r: number
}

let ctx: CanvasRenderingContext2D | null = null
let particles: P[] = []
let frame = 0
let running = false
let width = 0
let height = 0
let dpr = 1
const pointer = { x: -9999, y: -9999, active: false }

const reduceQuery =
  typeof window !== 'undefined' && window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : null

function resize() {
  const el = canvas.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  dpr = Math.min(window.devicePixelRatio || 1, 2)
  width = Math.max(1, rect.width)
  height = Math.max(1, rect.height)
  el.width = Math.round(width * dpr)
  el.height = Math.round(height * dpr)
  ctx = el.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

  // 粒子按面积取数量：窗口变小后密度不变，不会挤成一坨
  const target = Math.round((props.count * width * height) / (1440 * 900))
  const n = Math.max(18, Math.min(props.count * 2, target))

  const next: P[] = []
  for (let i = 0; i < n; i++) {
    next.push({
      x: Math.random() * width,
      y: Math.random() * height,
      // 速度区间刻意很窄：快一点就变成"下雪"，慢到 0.2 以下又像卡住了
      vx: (Math.random() - 0.5) * 0.26,
      vy: (Math.random() - 0.5) * 0.26,
      r: 0.9 + Math.random() * 1.5,
    })
  }
  particles = next
}

function step() {
  if (!ctx) return
  ctx.clearRect(0, 0, width, height)

  const linkDist = props.linkDistance
  const linkSq = linkDist * linkDist
  const repel = props.repelRadius

  for (const p of particles) {
    // 鼠标附近轻微推开：位移上限 0.9px/帧，够看出来但不会把粒子甩飞
    if (pointer.active) {
      const dx = p.x - pointer.x
      const dy = p.y - pointer.y
      const d2 = dx * dx + dy * dy
      if (d2 < repel * repel && d2 > 0.01) {
        const d = Math.sqrt(d2)
        const force = (1 - d / repel) * 0.9
        p.vx += (dx / d) * force * 0.06
        p.vy += (dy / d) * force * 0.06
      }
    }

    // 阻尼：被推开的粒子会慢慢回到原速，不然越积越快
    p.vx *= 0.996
    p.vy *= 0.996
    p.x += p.vx
    p.y += p.vy

    // 环绕而不是反弹：反弹会在边缘堆一条线，很扎眼
    if (p.x < -8) p.x = width + 8
    if (p.x > width + 8) p.x = -8
    if (p.y < -8) p.y = height + 8
    if (p.y > height + 8) p.y = -8
  }

  // 先画线后画点：线压在点下面，点的边缘才干净
  for (let i = 0; i < particles.length; i++) {
    const a = particles[i]
    for (let j = i + 1; j < particles.length; j++) {
      const b = particles[j]
      const dx = a.x - b.x
      const dy = a.y - b.y
      const d2 = dx * dx + dy * dy
      if (d2 > linkSq) continue
      const alpha = (1 - d2 / linkSq) * 0.28
      ctx.strokeStyle = `rgba(10, 125, 111, ${alpha.toFixed(3)})`
      ctx.lineWidth = 0.9
      ctx.beginPath()
      ctx.moveTo(a.x, a.y)
      ctx.lineTo(b.x, b.y)
      ctx.stroke()
    }
  }

  for (const p of particles) {
    ctx.fillStyle = 'rgba(10, 125, 111, 0.42)'
    ctx.beginPath()
    ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2)
    ctx.fill()
  }

  frame = requestAnimationFrame(step)
}

function start() {
  if (running || !ctx) return
  running = true
  frame = requestAnimationFrame(step)
}

function stop() {
  running = false
  cancelAnimationFrame(frame)
}

function onPointerMove(e: PointerEvent) {
  const el = canvas.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  pointer.x = e.clientX - rect.left
  pointer.y = e.clientY - rect.top
  pointer.active = true
}

function onPointerLeave() {
  pointer.active = false
}

function onVisibility() {
  if (document.hidden) stop()
  else start()
}

function onResize() {
  resize()
}

onMounted(() => {
  if (reduceQuery?.matches) {
    enabled.value = false
    return
  }
  resize()
  start()
  window.addEventListener('resize', onResize)
  window.addEventListener('pointermove', onPointerMove, { passive: true })
  window.addEventListener('pointerleave', onPointerLeave)
  document.addEventListener('visibilitychange', onVisibility)
  reduceQuery?.addEventListener?.('change', onReduceChange)
})

function onReduceChange(e: MediaQueryListEvent) {
  enabled.value = !e.matches
  if (e.matches) {
    stop()
  } else {
    resize()
    start()
  }
}

onBeforeUnmount(() => {
  stop()
  window.removeEventListener('resize', onResize)
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerleave', onPointerLeave)
  document.removeEventListener('visibilitychange', onVisibility)
  reduceQuery?.removeEventListener?.('change', onReduceChange)
})
</script>

<template>
  <canvas v-if="enabled" ref="canvas" class="particles" aria-hidden="true" />
</template>

<style scoped>
.particles {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  /* 必须让指针穿透：否则它盖在 hero 上，按钮就点不到了 */
  pointer-events: none;
}
</style>
