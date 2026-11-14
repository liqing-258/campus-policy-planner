<script setup lang="ts">
/**
 * 系统 logo。
 *
 * ## 图形在说什么
 *
 * 「知校」这两个字里，"校"是场景、"知"是动作。但一个学位帽太泛（满大街都是），
 * 一栋教学楼又太具体（看不出是"政策理解"）。所以取的是**门**：
 *
 * - 外框是校门/门牌，顶部一道弧是门楣——「进校门才谈得上有校规」；
 * - 门内是一本摊开的册子，两页中间有书脊——「规定写在文件里」；
 * - 册子上方一颗小点，是"读明白"的落点，也是整枚图形的视觉焦点。
 *
 * 三者叠起来正好是系统的动作链：**进校门 → 翻开文件 → 读明白**。
 * 全程只用几何形状，不用汉字——汉字版（现在顶栏那个"知"字块）在 16px 下
 * 会糊成一团，而且和旁边"知校"两个字重复。
 *
 * ## 为什么用 SVG 而不是位图
 *
 * 同一枚图形要出现在 22px（顶栏）、34px（落地页）、44px（登录小窗）三个尺寸，
 * 位图要么备三套、要么在高分屏上发虚。SVG 的 `currentColor` 还让它可以
 * 跟着父级文字色变（顶栏深字、登录小窗白字）。
 *
 * ## 渐变 id 必须唯一
 *
 * 页面上同时可能出现多枚（顶栏 + 面板 + 落地页）。SVG 的 `<defs>` 是**全局 id 空间**，
 * 两枚图形写同一个 `id="lg"` 时，后插入的会覆盖前面的引用，表现为"某个尺寸的
 * 渐变突然变成纯色"。这里用 `useId()` 生成唯一 id。
 */
import { computed, useId } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 渲染边长（px），宽高一致 */
    size?: number
    /** 是否带圆角底板。false 时只有图形本身，适合放在已有底色的容器里 */
    plate?: boolean
  }>(),
  { size: 28, plate: true },
)

/** Vue 3.5+ 的 useId；老版本没有就退化成随机串，只是为了让 id 不撞 */
const uid =
  typeof useId === 'function'
    ? useId()
    : `lg${Math.random().toString(36).slice(2, 8)}`

const gradId = computed(() => `logo-grad-${uid}`)
const softId = computed(() => `logo-soft-${uid}`)
</script>

<template>
  <span class="logo-mark" :class="{ plate }" :style="{ width: `${size}px`, height: `${size}px` }">
    <svg viewBox="0 0 48 48" role="img" aria-label="知校" focusable="false">
      <defs>
        <linearGradient :id="gradId" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#16b39b" />
          <stop offset="100%" stop-color="#06635a" />
        </linearGradient>
        <linearGradient :id="softId" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#ffffff" stop-opacity="0.34" />
          <stop offset="100%" stop-color="#ffffff" stop-opacity="0" />
        </linearGradient>
      </defs>

      <template v-if="plate">
        <!-- 底板：圆角方 + 顶部一层极淡高光，让图形在浅底上也立得住 -->
        <rect x="0" y="0" width="48" height="48" rx="13" :fill="`url(#${gradId})`" />
        <rect x="0" y="0" width="48" height="24" rx="13" :fill="`url(#${softId})`" />
      </template>

      <!-- 门楣：一道上凸的弧，代表校门 -->
      <path
        d="M13 17c0-4.4 4.9-8 11-8s11 3.6 11 8"
        fill="none"
        :stroke="plate ? '#ffffff' : 'var(--c-primary)'"
        stroke-width="3"
        stroke-linecap="round"
        :opacity="plate ? 0.95 : 1"
      />
      <!-- 门柱：两根竖线，比画一整圈门框更透气 -->
      <path
        d="M15.4 19.5v13.2M32.6 19.5v13.2"
        fill="none"
        :stroke="plate ? '#ffffff' : 'var(--c-primary)'"
        stroke-width="3"
        stroke-linecap="round"
        :opacity="plate ? 0.95 : 1"
      />
      <!-- 摊开的册子：左右两页 + 中间书脊 -->
      <path
        d="M14.5 22.6c3.2-1.5 6.3-1.5 9.5.4 3.2-1.9 6.3-1.9 9.5-.4v10.9c-3.2-1.5-6.3-1.5-9.5.4-3.2-1.9-6.3-1.9-9.5-.4z"
        :fill="plate ? '#ffffff' : 'var(--c-primary)'"
      />
      <!-- 书脊用底板色挖回来：有底板时挖成品牌深色，无底板时挖成页面底色 -->
      <path
        d="M24 23.2v10.6"
        :stroke="plate ? '#076b5e' : 'var(--c-surface)'"
        stroke-width="1.6"
        stroke-linecap="round"
      />
      <!-- 读明白的落点 -->
      <circle cx="24" cy="11.6" r="2.1" :fill="plate ? '#ffffff' : 'var(--c-primary)'" />
    </svg>
  </span>
</template>

<style scoped>
.logo-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  line-height: 0;
}

/* 无底板模式：给图形留一点内边距，避免贴边被裁 */
.logo-mark:not(.plate) svg {
  padding: 2px;
}

svg {
  width: 100%;
  height: 100%;
  display: block;
}
</style>
