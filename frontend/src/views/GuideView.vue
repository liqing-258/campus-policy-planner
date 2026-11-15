<script setup lang="ts">
/**
 * 校园事务指南 · 首页（R12）。
 *
 * 这一页只做三件事：**分类浏览 / 搜索 / 进入详情**。它不生成任何内容——
 * 卡片上的简介、条件预览、政策文件全部来自 `/api/v1/guides`，
 * 而那个接口的正文又是从 `document_chunks` 现取的（见 app/guides.py 的模块注释）。
 *
 * 两个数字必须一起显示：`available_count / item_count`。
 * 只显示"已收录 3 项"会让人以为这个分类只有 3 件事；只显示"5 项"又会把
 * 没有政策依据的编目也当成可办事项。两个一起给，空状态才有解释。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type GuideCard, type GuideCategory, type GuideOverview } from '../api/client'
import { EFFECTIVE_LABEL } from '../utils/format'
import GuideIcon from '../components/GuideIcon.vue'
import FavoriteButton from '../components/FavoriteButton.vue'

const router = useRouter()

const overview = ref<GuideOverview | null>(null)
const items = ref<GuideCard[]>([])
const loading = ref(true)
const error = ref('')

const activeCategory = ref('')
const keyword = ref('')

let searchTimer: number | undefined

const categories = computed<GuideCategory[]>(() => overview.value?.categories ?? [])
const recommended = computed(() => overview.value?.recommended ?? [])
const stats = computed(() => overview.value?.stats)

const activeCategoryData = computed(
  () => categories.value.find((item) => item.id === activeCategory.value) ?? null,
)

const filtering = computed(() => Boolean(activeCategory.value || keyword.value.trim()))

const listTitle = computed(() => {
  if (keyword.value.trim()) return `搜索「${keyword.value.trim()}」`
  if (activeCategoryData.value) return activeCategoryData.value.label
  return '全部事项'
})

/** 状态 → 语义色。active 绿、superseded 黄、其余灰：与知识库页保持一致。 */
function statusTone(status: string): string {
  if (status === 'active') return 'met'
  if (status === 'superseded' || status === 'expired') return 'unknown'
  return 'plain'
}

function statusLabel(status: string): string {
  return EFFECTIVE_LABEL[status] ?? status
}

async function loadOverview() {
  overview.value = await api.get<GuideOverview>('/guides')
}

async function loadItems() {
  const params = new URLSearchParams({ page: '1', page_size: '60' })
  if (activeCategory.value) params.set('category', activeCategory.value)
  const query = keyword.value.trim()
  if (query) params.set('q', query)
  const data = await api.get<{ items: GuideCard[]; total: number }>(
    `/guides/items?${params.toString()}`,
  )
  items.value = data.items
}

async function bootstrap() {
  loading.value = true
  error.value = ''
  try {
    await Promise.all([loadOverview(), loadItems()])
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function refreshList() {
  loading.value = true
  error.value = ''
  try {
    await loadItems()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function selectCategory(id: string) {
  activeCategory.value = activeCategory.value === id ? '' : id
  void refreshList()
}

function onSearch() {
  // 输入就发请求会把每个汉字都打成一次请求；250ms 足够让一句话打完
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => void refreshList(), 250)
}

function clearSearch() {
  keyword.value = ''
  void refreshList()
}

function resetFilters() {
  activeCategory.value = ''
  keyword.value = ''
  void refreshList()
}

function openItem(key: string) {
  void router.push(`/guides/${key}`)
}

onMounted(bootstrap)
onBeforeUnmount(() => window.clearTimeout(searchTimer))
</script>

<template>
  <div class="page guide-page">
    <div class="page-head">
      <div>
        <h2>校园事务指南</h2>
        <p class="sub">
          把政策文件拆成能照着办的步骤：先看条件与材料，再决定要不要问 AI 或跑一次资格匹配。
        </p>
      </div>
      <div class="tail">
        <span v-if="stats" class="tag">
          {{ stats.available_count }} 项已收录 · 覆盖 {{ stats.category_count }} 类
        </span>
        <router-link class="btn btn-ghost btn-sm" to="/library">政策知识库</router-link>
      </div>
    </div>

    <!-- 搜索 + 分类筛选：两个控件放同一张卡里，因为它们的结果永远一起生效 -->
    <section class="card filter">
      <div class="search-row">
        <span class="search-ico" aria-hidden="true">⌕</span>
        <input
          v-model="keyword"
          type="search"
          placeholder="搜事项、材料或政策文件名，如：四六级 / 在读证明 / 奖学金"
          aria-label="搜索校园事务"
          @input="onSearch"
        />
        <button v-if="keyword" class="btn btn-ghost btn-sm" @click="clearSearch">清空</button>
      </div>

      <div class="chips">
        <button class="chip" :class="{ on: !activeCategory }" @click="selectCategory('')">
          全部
          <span v-if="stats" class="count">{{ stats.item_count }}</span>
        </button>
        <button
          v-for="category in categories"
          :key="category.id"
          class="chip"
          :class="{ on: activeCategory === category.id, dim: category.empty }"
          :title="category.note ?? category.description"
          @click="selectCategory(category.id)"
        >
          <GuideIcon :name="category.id" :size="15" />
          {{ category.label }}
          <span class="count">{{ category.available_count }}/{{ category.item_count }}</span>
        </button>
      </div>
    </section>

    <p v-if="error" class="err-line">{{ error }}</p>

    <!-- 分类总览：只在没有筛选时出现，筛选后这一段就是噪声 -->
    <section v-if="!filtering" class="category-grid">
      <button
        v-for="category in categories"
        :key="category.id"
        class="category-card"
        :class="{ empty: category.empty }"
        @click="selectCategory(category.id)"
      >
        <span class="category-icon"><GuideIcon :name="category.id" :size="24" /></span>
        <span class="category-body">
          <strong>{{ category.label }}</strong>
          <small>{{ category.description }}</small>
        </span>
        <span class="category-foot">
          <span v-if="category.empty" class="tag unknown">暂无政策</span>
          <span v-else class="tag plain">
            {{ category.available_count }} 项
            <template v-if="category.item_count > category.available_count">
              / 编目 {{ category.item_count }}
            </template>
          </span>
          <span class="arrow" aria-hidden="true">→</span>
        </span>
        <small v-if="category.note" class="category-note">{{ category.note }}</small>
      </button>
    </section>

    <!-- 常用事项：新用户不知道从哪开始，给八个高频入口 -->
    <section v-if="!filtering && recommended.length" class="block">
      <h3 class="block-title">
        常用事项
        <span class="muted tiny">按办理频次挑选，不是"推荐算法"</span>
      </h3>
      <div class="item-grid compact">
        <button
          v-for="item in recommended"
          :key="item.key"
          class="item-card compact"
          @click="openItem(item.key)"
        >
          <span class="item-title">{{ item.title }}</span>
          <span class="tag plain">{{ item.category_label }}</span>
        </button>
      </div>
    </section>

    <section class="block">
      <h3 class="block-title">
        {{ listTitle }}
        <span class="muted tiny">{{ items.length }} 项</span>
      </h3>
      <p v-if="activeCategoryData?.note" class="muted tiny note-line">
        {{ activeCategoryData.note }}
      </p>

      <div v-if="loading" class="empty">正在读取政策原文…</div>

      <!-- 空状态三种，必须分开说，否则用户不知道是"没有"还是"坏了" -->
      <div v-else-if="!items.length && activeCategoryData?.empty" class="empty">
        <strong>这一类暂时没有可用的政策原文。</strong>
        <br />
        知识库里还没有收录{{ activeCategoryData.label }}相关的政策文件。为避免给你一份编出来的指南，
        这里先留空——可以到「政策知识库」看看已有哪些文件，或用「智能问答」直接问一句。
        <br />
        <router-link class="link" to="/library">去政策知识库</router-link>
      </div>
      <div v-else-if="!items.length" class="empty">
        没有匹配「{{ keyword }}」的事项。换一个说法，或者
        <button class="link as-btn" @click="resetFilters">看全部事项</button>。
      </div>

      <div v-else class="item-grid">
        <article
          v-for="item in items"
          :key="item.key"
          class="item-card"
          tabindex="0"
          role="link"
          @click="openItem(item.key)"
          @keydown.enter="openItem(item.key)"
          @keydown.space.prevent="openItem(item.key)"
        >
          <div class="item-head">
            <strong>{{ item.title }}</strong>
            <span
              v-if="item.availability !== 'ok'"
              class="tag"
              :class="item.availability === 'no_text' ? 'not_met' : 'unknown'"
            >{{ item.availability_label }}</span>
            <span
              v-else-if="item.document"
              class="tag"
              :class="statusTone(item.document.effective_status)"
            >{{ statusLabel(item.document.effective_status) }}</span>
          </div>

          <p class="item-summary">{{ item.summary }}</p>

          <p class="item-meta">
            <span class="meta-label">适用对象</span>
            <span>{{ item.audience?.text ?? '原文未单列适用对象，以政策原文为准' }}</span>
          </p>

          <dl
            v-if="item.conditions_preview || item.materials_preview || item.steps_preview"
            class="item-preview"
          >
            <div v-if="item.conditions_preview">
              <dt>条件</dt>
              <dd>{{ item.conditions_preview }}</dd>
            </div>
            <div v-if="item.materials_preview">
              <dt>材料</dt>
              <dd>{{ item.materials_preview }}</dd>
            </div>
            <div v-if="item.steps_preview">
              <dt>流程</dt>
              <dd>{{ item.steps_preview }}</dd>
            </div>
          </dl>

          <div class="item-foot">
            <span class="doc-name" :title="item.document?.title">
              {{ item.document?.title ?? '暂未收录相关政策' }}
            </span>
            <span class="tags">
              <span v-if="item.match_available" class="tag info">可匹配</span>
              <span v-if="item.plan_available" class="tag info">可生成计划</span>
            </span>
            <FavoriteButton target-type="guide" :target-id="item.key" />
          </div>
        </article>
      </div>
    </section>

    <p v-if="overview" class="muted tiny foot-note">{{ overview.notice }}</p>
  </div>
</template>

<style scoped>
.guide-page {
  gap: var(--space-4);
}

/* ---------- 搜索与分类 ---------- */
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
  pointer-events: none;
}

.search-row input {
  width: 100%;
  padding-left: 32px;
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

.chip.dim {
  opacity: 0.72;
}

.chip .count {
  font-size: 11px;
  color: var(--c-dim);
  font-variant-numeric: tabular-nums;
}

.chip.on .count {
  color: var(--c-primary);
}

/* ---------- 分类卡片 ---------- */
.category-grid {
  display: grid;
  gap: var(--space-3);
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

@media (max-width: 1180px) {
  .category-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.category-card {
  display: grid;
  grid-template-columns: auto 1fr;
  grid-template-areas:
    'icon body'
    'foot foot'
    'note note';
  gap: var(--space-2) var(--space-3);
  align-items: center;
  padding: var(--space-3) var(--space-4);
  text-align: left;
  font-family: inherit;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
  cursor: pointer;
  transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
}

.category-card:hover {
  transform: translateY(-2px);
  border-color: var(--c-primary-border);
  box-shadow: var(--shadow-pop);
}

.category-card.empty {
  background: var(--c-surface-2);
  box-shadow: none;
}

.category-icon {
  grid-area: icon;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 42px;
  height: 42px;
  border-radius: 12px;
  background: var(--c-primary-weak);
  color: var(--c-primary);
}

.category-card.empty .category-icon {
  background: var(--c-elev-1);
  color: var(--c-dim);
}

.category-body {
  grid-area: body;
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.category-body strong {
  font-size: 14px;
  color: var(--c-text);
}

.category-body small {
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--c-text-2);
}

.category-foot {
  grid-area: foot;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: 2px;
}

.category-foot .arrow {
  margin-left: auto;
  color: var(--c-dim);
  font-size: 13px;
  transition: transform 0.16s ease, color 0.16s ease;
}

.category-card:hover .arrow {
  transform: translateX(2px);
  color: var(--c-primary);
}

.category-note {
  grid-area: note;
  color: var(--c-unknown);
  font-size: 11px;
  line-height: 1.5;
}

/* ---------- 区块 ---------- */
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

.note-line {
  margin: -4px 0 0;
}

.item-grid {
  display: grid;
  gap: var(--space-3);
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.item-grid.compact {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

@media (max-width: 1180px) {
  .item-grid.compact {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 860px) {
  .item-grid,
  .item-grid.compact {
    grid-template-columns: 1fr;
  }
}

/* ---------- 事项卡片 ---------- */
.item-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-4);
  text-align: left;
  font-family: inherit;
  color: inherit;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
  cursor: pointer;
  transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
}

.item-card:hover,
.item-card:focus-visible {
  transform: translateY(-2px);
  border-color: var(--c-primary-border);
  box-shadow: var(--shadow-pop);
}

.item-card.compact {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-3);
  font-size: 12.5px;
}

.item-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-2);
}

.item-head strong {
  font-size: 14.5px;
  line-height: 1.4;
  color: var(--c-text);
}

.item-summary {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--c-text-2);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.item-meta {
  display: flex;
  gap: 6px;
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--c-text-2);
}

.meta-label {
  flex: none;
  color: var(--c-dim);
}

.item-preview {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin: 0;
  padding: 8px 10px;
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
  font-size: 11.5px;
  line-height: 1.7;
}

.item-preview > div {
  display: flex;
  gap: 6px;
  min-width: 0;
}

.item-preview dt {
  flex: none;
  color: var(--c-dim);
}

.item-preview dd {
  margin: 0;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--c-text-2);
}

.item-foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: auto;
  padding-top: var(--space-2);
  border-top: 1px dashed var(--c-border);
}

.doc-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11.5px;
  color: var(--c-dim);
}

.item-foot .tags {
  margin-left: auto;
  display: flex;
  gap: 4px;
}

.link {
  color: var(--c-primary);
  text-decoration: none;
}

.link:hover {
  text-decoration: underline;
}

.as-btn {
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  cursor: pointer;
}

.foot-note {
  margin: 0;
}
</style>
