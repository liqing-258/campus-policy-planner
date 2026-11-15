<script setup lang="ts">
/**
 * 我的收藏（R17）。
 *
 * 收藏状态由 favorites store 的镜像渲染「已收藏/未收藏」，但列表内容（名称、
 * 有效状态、问答正文）全部 `GET /favorites` 现取——「政策更新后同步有效状态」
 * 与「问答保留原始回答来源」都靠后端实时 JOIN，这一层不做副本。
 *
 * 取消收藏后要同步刷新镜像与列表，否则「我的收藏」页和详情页的按钮会各说各话。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type FavoriteItem, type FavoriteList, type FavoriteTarget } from '../api/client'
import { useFavoriteStore } from '../stores/favorites'
import { EFFECTIVE_LABEL, FAVORITE_TARGET_LABEL, formatTime, truncate } from '../utils/format'

const router = useRouter()
const fav = useFavoriteStore()

const items = ref<FavoriteItem[]>([])
const total = ref(0)
const loading = ref(true)
const error = ref('')
const activeType = ref<'' | FavoriteTarget>('')
const keyword = ref('')

const TYPE_ICONS: Record<string, string> = {
  document: '▣',
  guide: '▦',
  qa: '?',
  plan: '☑',
}

const typeChips = computed(() => [
  { id: '' as const, label: '全部' },
  { id: 'document' as const, label: '政策文件' },
  { id: 'guide' as const, label: '校园事务指南' },
  { id: 'qa' as const, label: '重要问答' },
  { id: 'plan' as const, label: '常用办理事项' },
])

function statusTone(status: string | null): string {
  if (status === 'active') return 'met'
  if (status === 'superseded' || status === 'expired') return 'unknown'
  return 'plain'
}

function statusLabel(status: string | null): string {
  if (!status) return '未关联政策'
  return EFFECTIVE_LABEL[status] ?? status
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ page: '1', page_size: '100' })
    if (activeType.value) params.set('type', activeType.value)
    if (keyword.value.trim()) params.set('q', keyword.value.trim())
    const data = await api.get<FavoriteList>(`/favorites?${params.toString()}`)
    items.value = data.items
    total.value = data.total
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function selectType(id: '' | FavoriteTarget) {
  activeType.value = activeType.value === id ? '' : id
  void load()
}

function onSearch() {
  void load()
}

function clearSearch() {
  keyword.value = ''
  void load()
}

/** 查看详情：按类型跳转到目标所在页面，并带深链定位到那一条。 */
function openDetail(item: FavoriteItem) {
  if (item.target_type === 'document') {
    void router.push({ path: '/library', query: { doc: item.target_id } })
  } else if (item.target_type === 'guide') {
    void router.push(`/guides/${item.target_id}`)
  } else if (item.target_type === 'qa') {
    if (item.conversation_id) {
      void router.push({ path: '/ask', query: { conversation_id: item.conversation_id } })
    } else {
      void router.push('/ask')
    }
  } else if (item.target_type === 'plan') {
    void router.push({ path: '/plans', query: { plan: item.target_id } })
  }
}

/** 问答收藏的摘要：优先问题，其次回答正文。 */
function summaryOf(item: FavoriteItem): string {
  if (item.target_type === 'qa') {
    return item.answer ?? item.question ?? ''
  }
  return ''
}

async function removeFav(item: FavoriteItem) {
  if (!window.confirm(`取消收藏「${item.title}」？`)) return
  try {
    await api.remove(`/favorites/${item.target_type}/${item.target_id}`)
    // 同步镜像（让详情页/列表页按钮一致）与列表
    fav.keys.delete(`${item.target_type}:${item.target_id}`)
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '取消失败'
  }
}

onMounted(async () => {
  // 先补镜像（可能还没拉过），再拉列表
  try {
    await fav.load()
  } catch {
    /* 镜像拉不到不阻断列表：列表自己会报错 */
  }
  await load()
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2>我的收藏</h2>
        <p class="muted small">
          把常用的政策、指南、问答与办理事项收进来，形成一个随取随用的个人常用信息库。
          政策有效状态与问答原文都实时取自知识库，收藏不会变成过期快照。
        </p>
      </div>
    </header>

    <!-- 搜索 + 分类筛选 -->
    <section class="card filter">
      <div class="search-row">
        <span class="search-ico" aria-hidden="true">⌕</span>
        <input
          v-model="keyword"
          type="search"
          placeholder="搜收藏的名称或问答内容"
          aria-label="搜索收藏"
          @keyup.enter="onSearch"
        />
        <button v-if="keyword" class="btn btn-ghost btn-sm" @click="clearSearch">清空</button>
        <button class="btn btn-sm" @click="onSearch">搜索</button>
      </div>
      <div class="chips">
        <button
          v-for="c in typeChips"
          :key="c.id"
          class="chip"
          :class="{ on: activeType === c.id }"
          @click="selectType(c.id)"
        >
          {{ c.label }}
        </button>
      </div>
    </section>

    <p v-if="error" class="err-line">{{ error }}</p>

    <section class="block">
      <h3 class="block-title">
        收藏（{{ total }}）
        <span v-if="loading" class="muted tiny">加载中…</span>
      </h3>

      <div v-if="loading" class="empty">正在读取收藏…</div>
      <div v-else-if="!items.length" class="empty">
        <strong>还没有收藏。</strong>
        <br />
        到「政策知识库」「校园事务指南」或「智能问答」里，把常用的内容点一下收藏，
        就会出现在这里。
      </div>

      <div v-else class="fav-list">
        <article v-for="item in items" :key="item.id" class="fav-card">
          <div class="fav-icon" :class="`t-${item.target_type}`">
            {{ TYPE_ICONS[item.target_type] ?? '·' }}
          </div>

          <div class="fav-body">
            <div class="fav-head">
              <strong class="fav-title">{{ item.title }}</strong>
              <span class="tag plain">{{ item.target_type_label }}</span>
            </div>

            <p v-if="summaryOf(item)" class="fav-summary">{{ truncate(summaryOf(item), 140) }}</p>

            <div class="fav-meta">
              <span class="meta-item">
                <span class="meta-label">收藏时间</span>
                {{ formatTime(item.created_at) }}
              </span>
              <span class="meta-item">
                <span class="meta-label">关联政策</span>
                <template v-if="item.document">{{ item.document.title }}</template>
                <template v-else>未关联政策文件</template>
              </span>
              <span class="meta-item">
                <span class="meta-label">有效状态</span>
                <span :class="['tag', statusTone(item.effective_status)]">
                  {{ statusLabel(item.effective_status) }}
                </span>
              </span>
            </div>
          </div>

          <div class="fav-actions">
            <button class="btn btn-ghost btn-sm" @click="openDetail(item)">查看详情</button>
            <button class="btn btn-sm" @click="removeFav(item)">取消收藏</button>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.filter {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.search-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.search-ico {
  position: absolute;
  left: 12px;
  color: var(--c-dim);
  font-size: 15px;
}

.search-row input {
  width: 100%;
  padding-left: 32px;
  min-height: 40px;
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
  font-family: inherit;
  font-size: 14px;
  color: var(--c-text);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 11px;
  border-radius: 999px;
  border: 1px solid var(--c-border);
  background: var(--c-surface);
  color: var(--c-text-2);
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
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
  font-weight: 500;
}

.block {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.block-title {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin: 0;
  font-size: 14px;
  color: var(--c-text);
}

.fav-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.fav-card {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
}

.fav-icon {
  flex: none;
  width: 40px;
  height: 40px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 12px;
  font-size: 16px;
  font-weight: 600;
  background: var(--c-primary-weak);
  color: var(--c-primary);
}

.fav-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.fav-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.fav-title {
  font-size: 14.5px;
  line-height: 1.4;
  color: var(--c-text);
}

.fav-summary {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--c-text-2);
}

.fav-meta {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--c-text-2);
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.meta-label {
  color: var(--c-dim);
}

.fav-actions {
  flex: none;
  display: flex;
  gap: var(--space-2);
  align-items: center;
}

.tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid var(--c-border);
  white-space: nowrap;
}

.tag.met {
  color: var(--c-met);
  border-color: var(--c-met-border);
  background: var(--c-met-bg);
}

.tag.unknown {
  color: var(--c-unknown);
  border-color: var(--c-warn-border);
  background: var(--c-warn-bg);
}

.tag.plain {
  color: var(--c-text-2);
  background: var(--c-bg);
}

.empty {
  padding: var(--space-8) 0;
  text-align: center;
  color: var(--c-text-2);
  font-size: 13px;
  line-height: 1.8;
}

.err-line {
  color: var(--c-not-met);
  font-size: 13px;
  margin: 0;
}

.small {
  font-size: 12px;
}

.tiny {
  font-size: 11px;
}

@media (max-width: 720px) {
  .fav-card {
    flex-direction: column;
  }
  .fav-actions {
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
