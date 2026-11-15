<script setup lang="ts">
/**
 * 收藏按钮（R17）。
 *
 * 五处入口（知识库列表/详情、指南卡片/详情、问答回答区、计划卡片）共用这一个按钮：
 * 收藏状态与切换都走 favorites store，避免各处各写一套 `POST /favorites`。
 * 挂载时若镜像还没拉过就拉一次，好让「已收藏 / 未收藏」一开始就画对。
 */
import { computed, onMounted, ref } from 'vue'
import type { FavoriteTarget } from '../api/client'
import { useFavoriteStore } from '../stores/favorites'

const props = defineProps<{
  targetType: FavoriteTarget
  targetId: string
}>()

const fav = useFavoriteStore()
const busy = ref(false)
const error = ref('')

const isFav = computed(() => fav.isFav(props.targetType, props.targetId))

onMounted(async () => {
  if (!fav.loaded) {
    try {
      await fav.load()
    } catch {
      /* 镜像拉不到不阻断按钮；点击时 toggle 仍会真实调用后端 */
    }
  }
})

async function toggle() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await fav.toggle(props.targetType, props.targetId)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '操作失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <span class="fav-btn">
    <button
      type="button"
      class="chip"
      :class="{ on: isFav }"
      :disabled="busy"
      :title="isFav ? '取消收藏' : '加入收藏'"
      @click.stop="toggle"
    >
      <span aria-hidden="true">{{ isFav ? '★' : '☆' }}</span>
      {{ isFav ? '已收藏' : '收藏' }}
    </button>
    <span v-if="error" class="fav-err">{{ error }}</span>
  </span>
</template>

<style scoped>
.fav-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--c-border);
  background: var(--c-surface);
  color: var(--c-text-2);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  white-space: nowrap;
  transition: border-color 0.16s ease, background 0.16s ease, color 0.16s ease;
}

.chip:hover {
  border-color: var(--c-primary-border);
  color: var(--c-primary);
}

.chip.on {
  border-color: var(--c-primary-strong);
  background: var(--c-primary-weak);
  color: var(--c-primary);
}

.chip:disabled {
  opacity: 0.55;
  cursor: default;
}

.fav-err {
  color: var(--c-not-met);
  font-size: 11px;
}
</style>
