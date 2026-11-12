<script setup lang="ts">
/**
 * 规则管理（M5，仅管理员）。
 *
 * 规则是匹配的唯一依据，所以这里有一条硬规矩：**抽取出来的规则默认是
 * needs_review，不参与匹配**。管理员逐条看过原文、确认字段与算子，
 * 点「通过」之后才进入判定。没复核就用，等于拿模型的一次猜测去回答
 * "你符不符合条件"。
 */
import { computed, onMounted, ref } from 'vue'
import {
  ApiError,
  api,
  type DocumentRow,
  type ExtractResult,
  type Paged,
  type RuleRow,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import {
  REVIEW_LABEL,
  RULE_TYPE_LABEL,
  evidenceLabel,
  formatTime,
  truncate,
} from '../utils/format'

const auth = useAuthStore()

const documents = ref<DocumentRow[]>([])
const documentId = ref('')
const reviewFilter = ref('')
const rules = ref<RuleRow[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const notice = ref('')

const extracting = ref(false)
const extractResult = ref<ExtractResult | null>(null)
const elapsed = ref(0)

const busyRule = ref('')

async function loadDocuments() {
  try {
    const data = await api.get<Paged<DocumentRow>>('/documents?page=1&page_size=100')
    documents.value = data.items
    if (!documentId.value && data.items.length) {
      // 优先选已索引的：没索引就没有片段，抽取无从下手
      const indexed = data.items.find((d) => d.parse_status === 'indexed')
      documentId.value = (indexed ?? data.items[0]).id
      await loadRules()
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '文档加载失败'
  }
}

async function loadRules() {
  if (!documentId.value) return
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ page: '1', page_size: '100' })
    if (reviewFilter.value) params.set('review_status', reviewFilter.value)
    const data = await api.get<Paged<RuleRow>>(`/documents/${documentId.value}/rules?${params}`)
    rules.value = data.items
    total.value = data.total
  } catch (err) {
    error.value = err instanceof Error ? err.message : '规则加载失败'
  } finally {
    loading.value = false
  }
}

async function extract() {
  if (!documentId.value) return
  extracting.value = true
  error.value = ''
  notice.value = ''
  extractResult.value = null
  elapsed.value = 0
  const timer = window.setInterval(() => (elapsed.value += 200), 200)
  try {
    const result = await api.post<ExtractResult>(
      `/documents/${documentId.value}/rules/extract`,
      { max_rules_per_chunk: 8 },
    )
    extractResult.value = result
    await loadRules()
  } catch (err) {
    error.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '抽取失败')
  } finally {
    window.clearInterval(timer)
    extracting.value = false
  }
}

async function review(rule: RuleRow, status: 'approved' | 'rejected') {
  busyRule.value = rule.id
  error.value = ''
  try {
    await api.patch<RuleRow>(`/rules/${rule.id}`, { review_status: status })
    await loadRules()
  } catch (err) {
    error.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '复核失败')
  } finally {
    busyRule.value = ''
  }
}

const counts = computed(() => {
  const out = { needs_review: 0, approved: 0, rejected: 0 }
  for (const r of rules.value) out[r.review_status] += 1
  return out
})

const currentDoc = computed(() => documents.value.find((d) => d.id === documentId.value))

onMounted(loadDocuments)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2>规则管理</h2>
        <p class="muted small">
          把政策原文拆成可判定的条件、材料与时间节点。抽取结果默认「待复核」，
          逐条通过后才参与匹配——这是学生看到的每一条结论能追溯到原文的前提。
        </p>
      </div>
    </header>

    <p v-if="!auth.isAdmin" class="card warn-card muted">
      当前账号是学生，只能查看。规则抽取与复核需要管理员权限。
    </p>

    <section class="card">
      <div class="row">
        <label class="field grow">
          <span>政策文件</span>
          <select v-model="documentId" class="select" @change="loadRules">
            <option value="" disabled>选择文件</option>
            <option v-for="d in documents" :key="d.id" :value="d.id">
              {{ d.title }}（{{ d.parse_status }} · {{ d.chunk_count }} 片段）
            </option>
          </select>
        </label>
        <label class="field">
          <span>复核状态</span>
          <select v-model="reviewFilter" class="select" @change="loadRules">
            <option value="">全部</option>
            <option value="needs_review">待复核</option>
            <option value="approved">已通过</option>
            <option value="rejected">已拒绝</option>
          </select>
        </label>
      </div>

      <p v-if="currentDoc" class="muted tiny">
        解析状态 {{ currentDoc.parse_status }} ·
        {{ currentDoc.chunk_count }} 个片段
        <template v-if="currentDoc.parse_status !== 'indexed'">
          —— 该文件没有可用片段，抽取会失败。请先确认它是「已索引」状态。
        </template>
      </p>

      <div v-if="auth.isAdmin" class="actions">
        <button class="btn" :disabled="extracting || !documentId" @click="extract">
          {{ extracting ? `抽取中… ${(elapsed / 1000).toFixed(1)}s` : '运行规则抽取' }}
        </button>
        <span class="muted tiny">
          调用模型，通常 10–40 秒。抽取不会覆盖已复核的规则，只会新增待复核条目。
        </span>
      </div>
    </section>

    <p v-if="error" class="err-line">{{ error }}</p>

    <section v-if="extractResult" class="card ok-card">
      <h3>抽取完成</h3>
      <p class="muted small">
        模型 {{ extractResult.model }} · 处理 {{ extractResult.chunks }} 个片段 ·
        模型给出 {{ extractResult.extracted }} 条 · 通过契约校验并入 {{ extractResult.stored }} 条
      </p>
      <div v-if="extractResult.dropped.length" class="dropped">
        <p class="muted tiny">被丢弃的条目（原文核对不上、算子不认识、字段不在白名单等）：</p>
        <ul>
          <li v-for="(d, i) in extractResult.dropped" :key="i" class="muted tiny">
            · <span class="mono">{{ d.rule_code ?? '—' }}</span>：{{ d.reason }}
          </li>
        </ul>
      </div>
    </section>

    <section class="card">
      <div class="list-head">
        <h3>规则（{{ total }}）</h3>
        <span class="muted tiny">
          待复核 {{ counts.needs_review }} · 已通过 {{ counts.approved }} · 已拒绝 {{ counts.rejected }}
        </span>
      </div>

      <p v-if="loading" class="muted small">加载中…</p>
      <p v-else-if="!rules.length" class="muted small">
        该文件还没有规则。{{ auth.isAdmin ? '点上方「运行规则抽取」生成。' : '请管理员先抽取并复核。' }}
      </p>

      <ul v-else class="rule-list">
        <li v-for="r in rules" :key="r.id">
          <div class="rule-head">
            <span class="mono tiny muted">{{ r.rule_code }}</span>
            <span class="tag plain">{{ RULE_TYPE_LABEL[r.rule_type] ?? r.rule_type }}</span>
            <span
              :class="['tag', r.review_status === 'approved' ? 'met' : r.review_status === 'rejected' ? 'not_met' : 'unknown']"
            >{{ REVIEW_LABEL[r.review_status] ?? r.review_status }}</span>
            <span v-if="!r.quote_verified" class="tag not_met">原文未核对上</span>
            <span v-if="r.confidence" class="muted tiny">置信 {{ r.confidence }}</span>
          </div>

          <p class="desc">{{ r.description }}</p>

          <div class="tuple">
            <span v-if="r.field_key" class="mono tiny">field={{ r.field_key }}</span>
            <span v-if="r.operator" class="mono tiny">op={{ r.operator }}</span>
            <span v-if="r.expected_value" class="mono tiny">expected={{ truncate(r.expected_value, 40) }}</span>
            <span v-if="!r.field_key && !r.operator" class="muted tiny">无判定条件（材料/流程类）</span>
          </div>

          <blockquote class="quote">
            <div class="muted tiny">
              {{ evidenceLabel(null, r.page_or_section) }} ·
              抽取于 {{ formatTime(r.created_at) }}
            </div>
            {{ truncate(r.original_quote, 320) }}
          </blockquote>

          <div v-if="auth.isAdmin" class="rule-actions">
            <button
              class="btn btn-ghost small-btn"
              :disabled="busyRule === r.id || r.review_status === 'approved'"
              @click="review(r, 'approved')"
            >通过</button>
            <button
              class="btn btn-ghost small-btn"
              :disabled="busyRule === r.id || r.review_status === 'rejected'"
              @click="review(r, 'rejected')"
            >拒绝</button>
            <span class="muted tiny">
              通过前必须补齐 field_key 与 operator（条件类），否则后端会打回。
            </span>
          </div>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.small {
  font-size: 12px;
}

.tiny {
  font-size: 11px;
}

.row {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.grow {
  flex: 1;
  min-width: 240px;
}

.select {
  min-height: 40px;
  padding: 0 var(--space-3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
  font-family: inherit;
  font-size: 14px;
  color: var(--c-text);
}

.actions {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  margin-top: var(--space-4);
  flex-wrap: wrap;
}

.list-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: var(--space-3);
  gap: var(--space-3);
}

.rule-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.rule-list li {
  border-bottom: 1px dashed var(--c-border);
  padding-bottom: var(--space-3);
}

.rule-list li:last-child {
  border-bottom: none;
  padding-bottom: 0;
}

.rule-head {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  flex-wrap: wrap;
}

.desc {
  margin: var(--space-2) 0;
  font-size: 13px;
}

.tuple {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
  margin-bottom: var(--space-2);
}

.quote {
  margin: 0 0 var(--space-2);
  padding: var(--space-2) var(--space-3);
  background: var(--c-surface-2);
  border-left: 3px solid var(--c-primary-weak);
  font-size: 12px;
  color: var(--c-text-2);
  line-height: 1.75;
}

.rule-actions {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  flex-wrap: wrap;
}

.small-btn {
  min-height: 30px;
  padding: 0 var(--space-3);
  font-size: 12px;
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

.tag.not_met {
  color: var(--c-not-met);
  border-color: var(--c-not-met-border);
  background: var(--c-not-met-bg);
}

.tag.plain {
  color: var(--c-text-2);
  background: var(--c-bg);
}

.ok-card {
  border-color: var(--c-met-border);
  background: var(--c-met-bg);
}

.warn-card {
  border-color: var(--c-warn-border);
  background: var(--c-warn-bg);
}

.dropped ul {
  margin: var(--space-1) 0 0;
  padding: 0;
  list-style: none;
}

.err-line {
  color: var(--c-not-met);
  font-size: 13px;
  margin: 0;
}
</style>
