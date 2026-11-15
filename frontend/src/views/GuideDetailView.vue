<script setup lang="ts">
/**
 * 校园事务指南 · 事项详情（R12）。
 *
 * 页面上的四块正文（条件 / 材料 / 步骤 / 常见问题）**逐字来自政策原文**，
 * 后端只做了两件事：按章节取、去掉页眉页脚与 PDF 硬换行。这里不再做二次改写——
 * 一旦在这一层"润色"，用户看到的就不再是政策，而是我们对政策的转述。
 *
 * 三个操作都不是新能力，只是把既有入口接上：
 * - 智能问答：带 `?q=`，预填一条与该事项有关的问题（不自动发送）；
 * - 资格匹配：带 `?document_id=`，跳过去时已选好这份文件；
 * - 事务计划：调既有的 `/plans/draft` 生成草稿，确认仍走 `/plans/{id}/confirm`。
 *   草稿在确认前只存在于响应里（plan 行已写库、任务未写），这与计划页的口径一致。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ApiError,
  api,
  type DraftData,
  type GuideBlock,
  type GuideDetail,
} from '../api/client'
import { EFFECTIVE_LABEL } from '../utils/format'
import CorrectionModal from '../components/CorrectionModal.vue'
import FavoriteButton from '../components/FavoriteButton.vue'
import GuideIcon from '../components/GuideIcon.vue'

const route = useRoute()
const router = useRouter()

const detail = ref<GuideDetail | null>(null)
const loading = ref(true)
const error = ref('')
// R16 纠错反馈：指南事项正文有误时上报（不改内容，只记反馈）
const correctionOpen = ref(false)

const draft = ref<DraftData | null>(null)
const drafting = ref(false)
const confirming = ref(false)
const draftError = ref('')
const confirmedId = ref('')

const itemKey = computed(() => String(route.params.key ?? ''))

const document = computed(() => detail.value?.document ?? null)
const blocks = computed<GuideBlock[]>(() => detail.value?.blocks ?? [])

/** 四块里缺了哪些——缺不是错，但要说清是"原文没写"还是"我们没取到"。 */
const missingSections = computed(() => {
  const found = new Set(blocks.value.map((block) => block.field))
  const missing: string[] = []
  if (!found.has('conditions')) missing.push('申请条件')
  if (!found.has('materials')) missing.push('材料清单')
  if (!found.has('steps')) missing.push('办理步骤')
  return missing
})

const duplicateNote = computed(() => {
  const fields = detail.value?.duplicate_fields ?? []
  if (!fields.includes('steps')) return ''
  return '「办理步骤」与上文的章节同源（原文把条件与流程写在同一章），未重复展示。'
})

const pageLabel = (block: GuideBlock) => {
  const parts = [block.section]
  if (block.article) parts.push(block.article)
  if (block.page) parts.push(`第 ${block.page} 页`)
  return parts.join(' · ')
}

function statusLabel(status: string): string {
  return EFFECTIVE_LABEL[status] ?? status
}

function statusTone(status: string): string {
  if (status === 'active') return 'met'
  if (status === 'superseded' || status === 'expired') return 'unknown'
  return 'plain'
}

async function load() {
  loading.value = true
  error.value = ''
  draft.value = null
  draftError.value = ''
  confirmedId.value = ''
  try {
    detail.value = await api.get<GuideDetail>(`/guides/items/${itemKey.value}`)
  } catch (err) {
    detail.value = null
    error.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '加载失败')
  } finally {
    loading.value = false
  }
}

// ---------- 三个动作 ----------
function goAsk() {
  const question = detail.value?.actions.ask.question
  if (!question) return
  void router.push({ path: '/ask', query: { q: question } })
}

function goMatch() {
  const target = detail.value?.actions.match
  if (!target?.available || !target.document_id) return
  void router.push({ path: '/match', query: { document_id: target.document_id } })
}

function openPolicy() {
  if (!document.value) return
  void router.push({ path: '/library', query: { doc: document.value.id } })
}

async function makeDraft() {
  const target = detail.value?.actions.plan
  if (!target?.available || !target.document_id) return
  drafting.value = true
  draftError.value = ''
  try {
    draft.value = await api.post<DraftData>('/plans/draft', {
      document_id: target.document_id,
    })
  } catch (err) {
    draftError.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '生成计划失败')
  } finally {
    drafting.value = false
  }
}

/** 手动改日期时把来源标成 manual：任务落库后要能说清"这个日期是谁给的"。 */
function onDueChange(index: number, value: string) {
  if (!draft.value) return
  const task = draft.value.tasks[index]
  task.due_at = value || null
  task.due_source = value ? 'manual' : null
}

async function confirmDraft() {
  if (!draft.value) return
  confirming.value = true
  draftError.value = ''
  try {
    const payload = {
      title: draft.value.title,
      tasks: draft.value.tasks.map((task) => ({
        temp_id: task.temp_id,
        title: task.title,
        description: task.description,
        priority: task.priority,
        due_at: task.due_at,
        required_materials: task.required_materials,
        dependency_ids: task.dependency_ids,
        source_rule_code: task.source_rule_code,
        due_source: task.due_source,
        note: task.note,
        needs_human_confirmation: task.needs_human_confirmation,
      })),
    }
    const plan = await api.post<{ plan_id: string; task_count: number }>(
      `/plans/${draft.value.plan_id}/confirm`,
      payload,
    )
    confirmedId.value = plan.plan_id
    draft.value = null
  } catch (err) {
    draftError.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '确认失败')
  } finally {
    confirming.value = false
  }
}

function openRelated(key: string) {
  void router.push(`/guides/${key}`)
}

onMounted(load)
// 从「同类事项」跳到另一条时组件会被复用，params 变了必须重取
watch(itemKey, load)
</script>

<template>
  <div class="page detail-page">
    <div class="crumb">
      <router-link to="/guides">校园事务指南</router-link>
      <span v-if="detail"> / {{ detail.category_label }}</span>
    </div>

    <p v-if="error" class="err-line">{{ error }}</p>
    <div v-if="loading" class="empty">正在读取政策原文…</div>

    <template v-if="detail">
      <!-- 事项头：一眼判断"这是不是我要办的事" -->
      <header class="card item-head">
        <div class="head-main">
          <div class="head-tags">
            <span class="tag plain">
              <GuideIcon :name="detail.category" :size="13" />
              {{ detail.category_label }}
            </span>
            <span
              v-if="detail.availability !== 'ok'"
              class="tag"
              :class="detail.availability === 'no_text' ? 'not_met' : 'unknown'"
            >{{ detail.availability_label }}</span>
            <span
              v-else-if="document"
              class="tag"
              :class="statusTone(document.effective_status)"
            >{{ statusLabel(document.effective_status) }}</span>
          </div>
          <h2>{{ detail.title }}</h2>
          <p class="summary">{{ detail.summary }}</p>
          <p class="audience">
            <span class="dim">适用对象</span>
            {{ detail.audience?.text ?? '原文未单列适用对象，请以政策原文为准' }}
          </p>
        </div>

        <div v-if="document" class="policy-chip">
          <span class="dim tiny">依据文件</span>
          <button class="policy-name" :title="document.title" @click="openPolicy">
            {{ document.title }}
          </button>
          <span class="muted tiny">
            <template v-if="document.academic_year">{{ document.academic_year }}</template>
            <template v-if="document.version_label"> · {{ document.version_label }}</template>
            <template v-if="!document.academic_year && !document.version_label">未标注版本</template>
          </span>
          <p v-if="document.superseded_by" class="warn tiny">
            已被《{{ document.superseded_by.title }}》替代，建议以新版为准。
          </p>
          <button class="link as-btn tiny" @click="openPolicy">查看原文与片段 →</button>
        </div>
        <!-- R16 纠错入口 + R17 收藏入口 -->
        <div class="correction-entry">
          <FavoriteButton target-type="guide" :target-id="itemKey" />
          <button class="link as-btn tiny" @click="correctionOpen = true">报告此处有误</button>
        </div>
      </header>

      <div class="detail-grid">
        <div class="main-col">
          <!-- 分区卡片：条件 / 材料 / 步骤 -->
          <section v-for="block in blocks" :key="block.field" class="card section-card">
            <h3>
              {{ block.label }}
              <span class="section-meta">{{ pageLabel(block) }}</span>
            </h3>
            <div class="policy-text">
              <p v-for="line in block.text.split('\n')" :key="line">{{ line }}</p>
            </div>
          </section>

          <section v-if="missingSections.length" class="card section-card">
            <h3>原文未单列的部分</h3>
            <p class="muted small">
              {{ missingSections.join('、') }}：这份政策文件里没有独立章节，或相关内容写在其它章节中。
              系统不替你补写——请以「政策原文及出处」里的完整文件为准。
            </p>
            <p v-if="duplicateNote" class="muted small">{{ duplicateNote }}</p>
          </section>

          <!-- 常见问题 -->
          <section v-if="detail.faq.length" class="card section-card">
            <h3>
              常见问题
              <span class="section-meta">政策文件「常见问题」章节原文</span>
            </h3>
            <ul class="faq">
              <li v-for="line in detail.faq" :key="line">{{ line }}</li>
            </ul>
          </section>

          <!-- 出处：把"这句话从哪来"落到文件 + 章节 + 页码 -->
          <section class="card section-card source-card">
            <h3>政策原文及出处</h3>
            <p class="muted small">{{ detail.source_note }}</p>
            <div v-if="document" class="source-row">
              <span class="dim tiny">文件</span>
              <button class="link as-btn" @click="openPolicy">{{ document.title }}</button>
              <span class="tag" :class="statusTone(document.effective_status)">
                {{ statusLabel(document.effective_status) }}
              </span>
            </div>
            <ul class="source-list">
              <li v-for="block in blocks" :key="block.field">
                《{{ document?.title }}》{{ pageLabel(block) }}
              </li>
              <li v-if="detail.faq.length && document">
                《{{ document.title }}》常见问题章节
              </li>
            </ul>
            <p v-if="!blocks.length" class="muted small">
              这条事项暂无可引用的原文片段——可能文件缺失，或文件还没有解析出正文。
            </p>
          </section>
        </div>

        <aside class="side-col">
          <section class="card action-card">
            <h3>下一步</h3>

            <button class="btn" @click="goAsk">前往智能问答</button>
            <p class="muted tiny">将带入问题：{{ detail.actions.ask.question }}</p>

            <button
              class="btn btn-ghost"
              :disabled="!detail.actions.match.available"
              @click="goMatch"
            >
              进行资格匹配
            </button>
            <p v-if="!detail.actions.match.available" class="muted tiny">
              {{ detail.actions.match.reason }}
            </p>
            <p v-else class="muted tiny">
              将带上这份文件（{{ detail.actions.match.approved_condition_rules }} 条已复核条件）。
            </p>

            <button
              class="btn btn-ghost"
              :disabled="!detail.actions.plan.available || drafting"
              @click="makeDraft"
            >
              {{ drafting ? '生成中…' : '加入事务计划' }}
            </button>
            <p v-if="!detail.actions.plan.available" class="muted tiny">
              {{ detail.actions.plan.reason }}
            </p>
            <p v-else class="muted tiny">
              先出草稿，确认后才写入任务中心——不会悄悄给你加一堆待办。
            </p>

            <p v-if="confirmedId" class="ok-line">
              计划已确认，任务已写入任务中心。
              <router-link class="link" to="/plans">去查看 →</router-link>
            </p>
          </section>

          <!-- 计划草稿：可改标题 / 优先级 / 截止日期，确认前不落任务表 -->
          <section v-if="draft" class="card action-card draft-card">
            <h3>计划草稿</h3>
            <p class="muted tiny">{{ draft.note }}</p>
            <div class="draft-list">
              <div v-for="(task, index) in draft.tasks" :key="task.temp_id" class="draft-item">
                <input v-model="task.title" type="text" maxlength="100" aria-label="任务标题" />
                <div class="draft-row">
                  <select v-model="task.priority" aria-label="优先级">
                    <option value="P0">P0 紧急</option>
                    <option value="P1">P1 重要</option>
                    <option value="P2">P2 一般</option>
                  </select>
                  <input
                    :value="task.due_at ? task.due_at.slice(0, 10) : ''"
                    type="date"
                    aria-label="截止日期"
                    @change="onDueChange(index, ($event.target as HTMLInputElement).value)"
                  />
                </div>
                <p v-if="task.note" class="muted tiny">{{ task.note }}</p>
              </div>
            </div>
            <p v-if="draftError" class="err-line">{{ draftError }}</p>
            <div class="draft-actions">
              <button class="btn" :disabled="confirming" @click="confirmDraft">
                {{ confirming ? '确认中…' : `确认并写入（${draft.tasks.length} 项）` }}
              </button>
              <button class="btn btn-ghost" :disabled="confirming" @click="draft = null">
                放弃草稿
              </button>
            </div>
          </section>
          <p v-else-if="draftError" class="err-line">{{ draftError }}</p>

          <section v-if="detail.related.length" class="card action-card">
            <h3>同类事项</h3>
            <button
              v-for="item in detail.related"
              :key="item.key"
              class="related-item"
              @click="openRelated(item.key)"
            >
              <span>{{ item.title }}</span>
              <span class="arrow" aria-hidden="true">→</span>
            </button>
          </section>
        </aside>
      </div>
    </template>

    <!-- R16 纠错反馈弹层：校园事务指南事项 -->
    <CorrectionModal
      v-if="detail"
      :open="correctionOpen"
      target-type="guide"
      :target-id="itemKey"
      :document-id="detail.document?.id ?? null"
      :target-title="detail.title"
      @close="correctionOpen = false"
    />
  </div>
</template>

<style scoped>
.correction-entry {
  margin-top: var(--space-2);
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.crumb {
  font-size: 12px;
  color: var(--c-dim);
}

.crumb a {
  color: var(--c-primary);
  text-decoration: none;
}

.crumb a:hover {
  text-decoration: underline;
}

/* ---------- 事项头 ---------- */
.item-head {
  display: flex;
  gap: var(--space-6);
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
}

.head-main {
  flex: 1 1 420px;
  min-width: 0;
}

.head-tags {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-bottom: var(--space-2);
}

.item-head h2 {
  margin: 0 0 var(--space-2);
  font-size: 20px;
  line-height: 1.35;
}

.summary {
  margin: 0 0 var(--space-2);
  font-size: 13px;
  line-height: 1.85;
  color: var(--c-text-2);
}

.audience {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--c-text-2);
}

.audience .dim {
  margin-right: 6px;
}

.policy-chip {
  flex: 0 1 300px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: var(--space-3);
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
}

.policy-name {
  padding: 0;
  border: none;
  background: none;
  font: inherit;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--c-primary);
  text-align: left;
  cursor: pointer;
}

.policy-name:hover {
  text-decoration: underline;
}

.warn {
  color: var(--c-unknown);
  margin: 2px 0 0;
}

/* ---------- 两栏 ---------- */
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: var(--space-4);
  align-items: start;
}

@media (max-width: 1080px) {
  .detail-grid {
    grid-template-columns: 1fr;
  }
}

.main-col,
.side-col {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  min-width: 0;
}

/* ---------- 分区卡片 ---------- */
.section-card h3 {
  align-items: baseline;
  flex-wrap: wrap;
}

.section-meta {
  margin-left: auto;
  color: var(--c-dim);
  font-size: 11px;
  font-weight: 400;
}

.policy-text p {
  margin: 0 0 6px;
  font-size: 13px;
  line-height: 1.95;
  color: var(--c-text);
  white-space: pre-wrap;
}

.policy-text p:last-child {
  margin-bottom: 0;
}

.faq {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.faq li {
  font-size: 12.5px;
  line-height: 1.8;
  color: var(--c-text-2);
}

.source-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-bottom: var(--space-2);
}

.source-list {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--c-text-2);
  font-size: 11.5px;
  line-height: 1.7;
}

/* ---------- 侧栏 ---------- */
.action-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.action-card .btn {
  width: 100%;
}

.action-card p {
  margin: 0;
}

.draft-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.draft-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: var(--space-2);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
}

.draft-item input[type='text'],
.draft-item select,
.draft-item input[type='date'] {
  width: 100%;
  min-height: 30px;
  font-size: 12px;
  padding: 4px 8px;
}

.draft-row {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 6px;
}

.draft-item p {
  margin: 0;
}

.draft-actions {
  display: flex;
  gap: var(--space-2);
}

.draft-actions .btn {
  flex: 1;
  min-width: 0;
}

.related-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  width: 100%;
  padding: 7px 2px;
  border: none;
  border-bottom: 1px dashed var(--c-border);
  background: none;
  font: inherit;
  font-size: 12.5px;
  color: var(--c-text-2);
  text-align: left;
  cursor: pointer;
}

.related-item:last-child {
  border-bottom: none;
}

.related-item:hover {
  color: var(--c-primary);
}

.related-item .arrow {
  color: var(--c-dim);
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
</style>
