<script setup lang="ts">
/**
 * 自动滚动的列表容器。
 *
 * ## 为什么不是 CSS 动画
 *
 * 常见做法是把列表复制一份、用 `@keyframes` 无限上移（marquee）。它有两个致命问题：
 * ① **鼠标悬停无法真正停下**——只能 `animation-play-state: paused`，但滚到半截
 *    停住的瞬时位置和用户想看清的那一条对不上，读起来眼跳；
 * ② **复制的那一份里每条都是重复 DOM**，点击哪一条？无障碍读屏会把每条念两遍。
 *
 * 这里用 `scrollTop` 手动推进：悬停时直接停住（位置就是眼睛在的那一处），
 * 指针移开再接着走；用户手动滚轮/拖动也会暂停，并且 2 秒内不再抢回控制权。
 *
 * ## 什么时候滚
 *
 * 只有**内容高度超过容器**才滚。三条记录也让它慢慢爬，比不滚更让人烦躁。
 *
 * ## 回到顶部
 *
 * 滚到底后停 1.1 秒，然后`scrollTop` 直接归零。为什么不做"无缝衔接"：
 * 无缝需要复制节点，就退回到上面说的 marquee 问题。这里的列表本来就是
 * "轮播给你看"的性质，跳回顶部不会造成误解——而且跳回的动作被 1.1 秒停顿
 * 和淡出隔开，视觉上像"换了一屏"。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 每秒滚动多少像素。16 约等于"广告屏"速度，再快就读不了 */
    speed?: number
    /** 容器最大高度；不传就按 CSS 里给的高度算 */
    maxHeight?: number
  }>(),
  { speed: 16, maxHeight: 0 },
)

const viewport = ref<HTMLElement | null>(null)
const content = ref<HTMLElement | null>(null)
const canScroll = ref(false)
const paused = ref(false)

let raf = 0
let last = 0
let holdUntil = 0
/** 滚到底之后的停顿计时（ms），用来代替"立刻跳回顶部"带来的突兀感 */
let bottomHold = 0
/** 不足 1px 的位移余量，见 tick() 里的说明 */
let carry = 0

const BOTTOM_HOLD_MS = 1100

function measure() {
  const view = viewport.value
  const inner = content.value
  if (!view || !inner) {
    canScroll.value = false
    return
  }
  const overflow = inner.scrollHeight - view.clientHeight
  canScroll.value = overflow > 6
  if (!canScroll.value) view.scrollTop = 0
}

function tick(now: number) {
  raf = requestAnimationFrame(tick)
  const view = viewport.value
  if (!view || !canScroll.value || paused.value) return
  if (now < holdUntil) return
  const delta = last ? Math.min(now - last, 64) : 16
  last = now

  const max = view.scrollHeight - view.clientHeight
  if (max <= 0) return

  if (view.scrollTop >= max - 0.5) {
    // 停在底部，等一会儿再回顶
    if (!bottomHold) bottomHold = now
    if (now - bottomHold > BOTTOM_HOLD_MS) {
      view.scrollTop = 0
      bottomHold = 0
    }
    return
  }
  bottomHold = 0

  /**
   * 必须自己累积小数余量。
   *
   * 一帧的位移是 `speed * delta / 1000` ≈ 0.27px（16px/s @60fps），而 `scrollTop`
   * 在某些元素上**只接受整数**（Chromium 的布局单位是 1/64px，写 0.27 读回来是 0）。
   * 直接写的话每帧都被舍回 0，永远不动 —— 第一次实现就踩了这个坑，
   * 表现是"60fps 一直在写 scrollTop，但值恒为 0"。
   * 攒够 1px 再写，读数才是连续的。
   */
  carry += (props.speed * delta) / 1000
  if (carry < 1) return
  const step = Math.floor(carry)
  carry -= step
  view.scrollTop = Math.min(max, view.scrollTop + step)
}

function pause() {
  paused.value = true
}

function resume() {
  paused.value = false
  last = 0
  carry = 0
}

/** 用户自己动了滚动条：给他 2 秒做主导权，别一边滚一边跟他抢 */
function onUserScroll() {
  holdUntil = performance.now() + 2000
}

onMounted(() => {
  measure()
  // 数据是异步来的：DOM 高度会变，但**不能**在 ResizeObserver 里同步改 DOM——
  // 观察回调里改布局会触发 "ResizeObserver loop completed with undelivered notifications"，
  // 浏览器控制台每帧刷一条。这里是"改 scrollTop"（不触发尺寸变化），所以安全；
  // 真正会改尺寸的是 measure() 里的 scrollTop = 0，只在超出→不超出时走一次。
  if (typeof ResizeObserver !== 'undefined' && content.value) {
    const observer = new ResizeObserver(() => {
      const view = viewport.value
      const inner = content.value
      if (!view || !inner) return
      const overflow = inner.scrollHeight - view.clientHeight
      const next = overflow > 6
      if (next !== canScroll.value) {
        canScroll.value = next
        if (!next) view.scrollTop = 0
      }
    })
    observer.observe(content.value)
    cleanup.push(() => observer.disconnect())
  }
  setTimeout(measure, 260)
  raf = requestAnimationFrame(tick)
})

const cleanup: (() => void)[] = []

onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  cleanup.forEach((fn) => fn())
})

watch(
  () => props.maxHeight,
  (value) => {
    if (viewport.value && value > 0) viewport.value.style.maxHeight = `${value}px`
    measure()
  },
)

defineExpose({ measure })
</script>

<template>
  <div
    ref="viewport"
    class="scroll-box"
    :class="{ live: canScroll, held: paused }"
    @pointerenter="pause"
    @pointerleave="resume"
    @wheel.passive="onUserScroll"
    @touchstart.passive="onUserScroll"
  >
    <div ref="content" class="scroll-inner">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.scroll-box {
  max-height: 268px;
  overflow-y: auto;
  /* 遮住上下两端：滚动的列表贴着卡片边缘会显得"漏了一块"，
     这两道渐隐让出入场变软。用 mask 而不是叠加渐变块，
     因为叠加块在深色底上会盖住悬停高亮 */
  mask-image: linear-gradient(to bottom, transparent 0, #000 14px, #000 calc(100% - 14px), transparent 100%);
  -webkit-mask-image: linear-gradient(
    to bottom,
    transparent 0,
    #000 14px,
    #000 calc(100% - 14px),
    transparent 100%
  );
  padding-block: 2px;
}

/* 滚动时隐藏滚动条：它是自动滚的，用户不需要知道自己滚到哪了；
   但悬停时露出来，因为那时用户可能想手动拖 */
.scroll-box.live::-webkit-scrollbar {
  width: 6px;
}

.scroll-box.live::-webkit-scrollbar-thumb {
  background: var(--c-border-strong);
  background-clip: content-box;
  border-radius: 999px;
}

.scroll-box.live {
  scrollbar-width: thin;
  scrollbar-color: transparent transparent;
}

.scroll-box.live:hover {
  scrollbar-color: var(--c-border-strong) transparent;
}

.scroll-inner {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* 悬停时压一下速度感：不改 scrollTop，只让光标变成"这里能点" */
.scroll-box.held {
  cursor: default;
}
</style>
