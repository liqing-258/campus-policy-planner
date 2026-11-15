<script setup lang="ts">
/**
 * 指南分类图标（R12）。
 *
 * 八个分类各一枚 24×24 线性图标，`stroke="currentColor"`，颜色跟随所在卡片。
 * 不用 emoji 有两个原因：一是 emoji 在 Windows / macOS / Android 上字形差异很大，
 * 同一排卡片会高低不齐；二是 emoji 自带颜色，与浅底青碧主题的克制感冲突。
 *
 * 路径只画"能一眼认出是什么"的最少笔画——这些图标在 20px 下显示，
 * 细节画多了只剩一团墨点。
 */
defineProps<{ name: string; size?: number }>()
</script>

<template>
  <svg
    class="guide-icon"
    :width="size ?? 22"
    :height="size ?? 22"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <!-- 奖学金与助学金：奖章 + 绶带 -->
    <template v-if="name === 'scholarship'">
      <circle cx="12" cy="9" r="5" />
      <path d="M8.5 13.2 7 21l5-2.6L17 21l-1.5-7.8" />
      <path d="m10 9 1.4 1.4L14 7.8" />
    </template>

    <!-- 评奖评优：星 -->
    <template v-else-if="name === 'honors'">
      <path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.7l5.9-.9z" />
    </template>

    <!-- 考试与等级考试：试卷 + 笔 -->
    <template v-else-if="name === 'exams'">
      <rect x="4" y="3.5" width="12" height="17" rx="2" />
      <path d="M7 8h6M7 12h6M7 16h3" />
      <path d="m18.5 8.5 2 2-6.5 6.5-2.6.6.6-2.6z" />
    </template>

    <!-- 学籍与成绩：翻开的本子 -->
    <template v-else-if="name === 'academic'">
      <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5z" />
      <path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5A1.5 1.5 0 0 0 20 18.5z" />
      <path d="M6.5 8h2M15.5 8h2" />
    </template>

    <!-- 宿舍与校园生活：楼 + 门 -->
    <template v-else-if="name === 'campus-life'">
      <path d="M4 20V6.5L12 3v17" />
      <path d="M12 20h8V10l-8-3.5" />
      <path d="M7 9h2M7 13h2M15.5 13h2M15.5 16.5h2" />
    </template>

    <!-- 图书馆服务：书架 -->
    <template v-else-if="name === 'library'">
      <rect x="4" y="4" width="4" height="16" rx="1" />
      <rect x="10" y="4" width="4" height="16" rx="1" />
      <path d="m16.5 5.2 3.6 1 3.1 13.6" />
    </template>

    <!-- 就业与实习：公文包 -->
    <template v-else-if="name === 'career'">
      <rect x="3" y="7.5" width="18" height="12" rx="2" />
      <path d="M9 7.5V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5v2" />
      <path d="M3 12.5h18" />
      <path d="M10.5 12.5h3" />
    </template>

    <!-- 请假与证明办理：文件 + 勾 -->
    <template v-else-if="name === 'service'">
      <path d="M14 3.5H6.5A1.5 1.5 0 0 0 5 5v14a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 19V8.5z" />
      <path d="M14 3.5V8a.5.5 0 0 0 .5.5H19" />
      <path d="m8.5 14 2 2 4-4" />
    </template>

    <!-- 兜底：不加图标也好过显示一个破图 -->
    <template v-else>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 8v4l3 2" />
    </template>
  </svg>
</template>

<style scoped>
.guide-icon {
  flex: none;
  display: block;
}
</style>
