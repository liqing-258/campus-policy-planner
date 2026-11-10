<script setup lang="ts">
/**
 * 资格匹配（M5）。
 *
 * 三种结果是三件事，不能合并成"符合/不符合"两态：
 * - 符合 / 不符合：字段齐全、算子可求值
 * - 无法判定：还要说清为什么（缺字段 / 条款主观 / 缺依据）
 *
 * 匹配完全不调模型（设计 §7.6）：同一个人填同样的画像，结果必须每次都一样，
 * 而且能追溯到政策原文的那一条。所以这一页不会出现"AI 判断"字样。
 */
import { computed, onMounted, ref } from 'vue'
import {
  ApiError,
  api,
  type DraftData,
  type DocumentRow,
  type MatchData,
  type MatchItem,
  type Paged,
  type RuleRow,
} from '../api/client'
import {
  MATCH_LABEL,
  MATCH_TONE,
  RULE_TYPE_LABEL,
  UNKNOWN_REASON_LABEL,
  evidenceLabel,
  formatBytes,
} from '../utils/format'

const documents = ref<DocumentRow[]>([])
/** 每份文件"已复核的条件类规则"条数——匹配能不能跑，取决于这个数不是 0。 */
const conditionsByDoc = ref<Record<string, number>>({})
const documentId = ref('')
const matching = ref(false)
const result = ref<MatchData | null>(null)
const error = ref('')
const notice = ref('')

const draft = ref<DraftData | null>(null)
const drafting = ref(false)
const confirming = ref(false)
const confirmedId = ref('')

/** 画像字段：全部可选。空值不发，后端会判成 unknown/missing_info，
 *  这正是真实场景——逼用户填齐才给结果等于把"信息不足"藏起来。 */
const profile = ref({
  student_type: '',
  grade: '',
  gpa: '',
  rank_percent: '',
  poverty_certified: '',
  disciplinary_record: '',
  major: '',
  credits_earned: '',
  enrollment_status: '',
})

const fields: { key: keyof typeof profile.value; label: string; hint: string; type: string }[] = [
  { key: 'student_type', label: '学生类型', hint: '如 full_time_undergraduate', type: 'text' },
  { key: 'grade', label: '年级', hint: '1–8', type: 'number' },
  { key: 'gpa', label: '平均学分绩点', hint: '0–5', type: 'number' },
  { key: 'rank_percent', label: '专业排名百分比', hint: '0–100，越小越靠前', type: 'number' },
  { key: 'poverty_certified', label: '家庭经济困难认定', hint: 'yes / no', type: 'text' },
  { key: 'disciplinary_record', label: '纪律处分记录', hint: 'none 表示无', type: 'text' },
  { key: 'major', label: '专业', hint: '选填', type: 'text' },
  { key: 'credits_earned', label: '已修学分', hint: '选填', type: 'number' },
  { key: 'enrollment_status', label: '学籍状态', hint: '如 enrolled', type: 'text' },
]

async function loadDocuments() {
  try {
    const data = await api.get<Paged<DocumentRow>>(
      '/documents?page=1&page_size=50&parse_status=indexed',
    )
    // 只列出"已索引"的文件还不够：没有已复核条件规则的文件点进来只会拿到 2008。
    // 这里顺带查一次每份文件的已复核条件数，把能用的排在前面并默认选中。
    const counts: Record<string, number> = {}
    await Promise.all(
      data.items.map(async (doc) => {
        try {
          const rules = await api.get<Paged<RuleRow>>(
            `/documents/${doc.id}/rules?review_status=approved&page=1&page_size=100`,
          )
          counts[doc.id] = rules.items.filter(
            (r) => r.rule_type === 'eligibility' || r.rule_type === 'restriction',
          ).length
        } catch {
          counts[doc.id] = 0
        }
      }),
    )
    conditionsByDoc.value = counts
    documents.value = [...data.items].sort(
      (a, b) => (counts[b.id] ?? 0) - (counts[a.id] ?? 0),
    )
    if (!documentId.value) {
      const best = documents.value.find((d) => (counts[d.id] ?? 0) > 0)
      documentId.value = (best ?? documents.value[0])?.id ?? ''
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '文档加载失败'
  }
}

function buildProfile(): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const f of fields) {
    const raw = profile.value[f.key].trim()
    if (!raw) continue
    out[f.key] = f.type === 'number' ? Number(raw) : raw
  }
  return out
}

async function runMatch() {
  if (!documentId.value) return
  matching.value = true
  error.value = ''
  notice.value = ''
  result.value = null
  draft.value = null
  confirmedId.value = ''
  try {
    const envelope = await api.raw<MatchData>('/matches', {
      document_id: documentId.value,
      profile: buildProfile(),
    })
    if (envelope.code === 0) {
      result.value = envelope.data as MatchData
    } else if (envelope.code === 2008) {
      notice.value = envelope.message
    } else {
      error.value = `${envelope.code} · ${envelope.message}`
    }
  } catch (err) {
    error.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '匹配失败')
  } finally {
    matching.value = false
  }
}

async function makeDraft() {
  if (!result.value) return
  drafting.value = true
  error.value = ''
  try {
    draft.value = await api.post<DraftData>('/plans/draft', {
      document_id: result.value.document_id,
      match_session_id: result.value.session_id,
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : '生成计划失败'
  } finally {
    drafting.value = false
  }
}

async function confirmDraft() {
  if (!draft.value) return
  confirming.value = true
  error.value = ''
  try {
    const payload = {
      title: draft.value.title,
      tasks: draft.value.tasks.map((t) => ({
        temp_id: t.temp_id,
        title: t.title,
        description: t.description,
        priority: t.priority,
        due_at: t.due_at,
        required_materials: t.required_materials,
        dependency_ids: t.dependency_ids,
        // 溯源元数据原样回传：不回传的话，任务落库后就查不回是哪条规则、
        // 也说不清"为什么截止日期是空的"（后端 tasks 表有对应列）
        source_rule_code: t.source_rule_code,
        due_source: t.due_source,
        note: t.note,
        needs_human_confirmation: t.needs_human_confirmation,
      })),
    }
    const plan = await api.post<{ plan_id: string; task_count: number }>(
      `/plans/${draft.value.plan_id}/confirm`,
      payload,
    )
    confirmedId.value = plan.plan_id
    draft.value = null
  } catch (err) {
    error.value = err instanceof Error ? err.message : '确认失败'
  } finally {
    confirming.value = false
  }
}

const grouped = computed(() => {
  if (!result.value) return []
  const order: MatchItem['status'][] = ['not_met', 'unknown', 'met']
  return order
    .map((status) => ({
      status,
      items: result.value!.items.filter((i) => i.status === status),
    }))
    .filter((g) => g.items.length > 0)
})

const currentDoc = computed(() => documents.value.find((d) => d.id === documentId.value))

onMounted(loadDocuments)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2>资格匹配</h2>
        <p class="muted small">
          按政策条款逐条比对，结果只有三种：符合、不符合、无法判定。
          「无法判定」会说明缺什么——这是系统主动承认不知道，而不是猜一个结论。
        </p>
      </div>
    </header>

    <section class="card">
      <div class="row">
        <label class="field grow">
          <span>政策文件</span>
          <select v-model="documentId" class="select">
            <option value="" disabled>选择一份已索引的文件</option>
            <option v-for="d in documents" :key="d.id" :value="d.id">
              {{ d.title }}（已复核条件 {{ conditionsByDoc[d.id] ?? 0 }} 条 · {{ d.chunk_count }} 片段）
            </option>
          </select>
        </label>
      </div>
      <p v-if="currentDoc" class="muted tiny">
        <template v-if="(conditionsByDoc[currentDoc.id] ?? 0) > 0">
          该文件有 {{ conditionsByDoc[currentDoc.id] }} 条已复核条件，可以匹配。
        </template>
        <template v-else>
          该文件还没有已复核的条件类规则，现在匹配会返回「暂时无法匹配」。
          请管理员到「规则管理」抽取并复核，或换一份文件。
        </template>
      </p>

      <h3 class="sub">我的画像</h3>
      <p class="muted tiny">全部选填。留空的字段会变成「缺少信息」而不是「不符合」——两者含义完全不同。</p>
      <div class="profile-grid">
        <label v-for="f in fields" :key="f.key" class="field">
          <span>{{ f.label }}</span>
          <input v-model="profile[f.key]" :type="f.type" :placeholder="f.hint" />
        </label>
      </div>

      <div class="actions">
        <button class="btn" :disabled="matching || !documentId" @click="runMatch">
          {{ matching ? '比对中…' : '开始匹配' }}
        </button>
        <span class="muted tiny">不调用模型：同样的画像每次得到同样的结果。</span>
      </div>
    </section>

    <p v-if="error" class="err-line">{{ error }}</p>

    <section v-if="notice" class="card warn-card">
      <h3>暂时无法匹配</h3>
      <p class="muted">{{ notice }}</p>
      <p class="muted tiny">
        管理员需要先在该文件上运行规则抽取，并逐条复核通过——没复核的规则不参与判定，
        否则等于拿模型的一次猜测去回答"你符不符合条件"。
      </p>
    </section>

    <template v-if="result">
      <section class="card summary">
        <div>
          <h3>匹配结论</h3>
          <p class="muted tiny">{{ result.disclaimer }}</p>
        </div>
        <div class="counts">
          <span class="count met">符合 {{ result.summary.met }}</span>
          <span class="count not_met">不符合 {{ result.summary.not_met }}</span>
          <span class="count unknown">无法判定 {{ result.summary.unknown }}</span>
        </div>
      </section>

      <section v-for="group in grouped" :key="group.status" class="card">
        <h3 :class="['group-title', MATCH_TONE[group.status]]">
          {{ MATCH_LABEL[group.status] }}（{{ group.items.length }}）
        </h3>
        <ul class="items">
          <li v-for="item in group.items" :key="item.rule_id">
            <div class="item-head">
              <span :class="['tag', MATCH_TONE[item.status]]">{{ MATCH_LABEL[item.status] }}</span>
              <span class="tag plain">{{ RULE_TYPE_LABEL[item.rule_type] ?? item.rule_type }}</span>
              <span v-if="item.unknown_reason" class="tag unknown">
                {{ UNKNOWN_REASON_LABEL[item.unknown_reason] ?? item.unknown_reason }}
              </span>
              <span class="mono tiny muted">{{ item.rule_code }}</span>
            </div>
            <p class="desc">{{ item.description }}</p>
            <p class="rationale">{{ item.rationale }}</p>
            <div class="values">
              <span v-if="item.user_value !== null" class="muted tiny">你的值：<b>{{ item.user_value }}</b></span>
              <span v-if="item.expected_value !== null" class="muted tiny">政策要求：<b>{{ item.expected_value }}</b></span>
            </div>
            <blockquote class="quote">
              <div class="muted tiny">
                {{ evidenceLabel(item.evidence.page, item.evidence.section) }}
              </div>
              {{ item.evidence.quote }}
            </blockquote>
          </li>
        </ul>
      </section>

      <section class="card">
        <h3>下一步：生成申请计划</h3>
        <p class="muted small">
          按政策里的材料清单、截止时间与缺项信息，自动起草一份任务清单。草稿不会写进任务中心，
          确认之后才落库。
        </p>
        <div class="actions">
          <button class="btn" :disabled="drafting" @click="makeDraft">
            {{ drafting ? '生成中…' : '生成计划草稿' }}
          </button>
        </div>
        <p v-if="confirmedId" class="ok-line">
          计划已确认（{{ confirmedId }}），到「计划与任务」页可以推进任务状态。
        </p>
      </section>
    </template>

    <section v-if="draft" class="card">
      <h3>计划草稿：{{ draft.title }}</h3>
      <p class="muted tiny">{{ draft.note }}</p>
      <ol class="draft-tasks">
        <li v-for="t in draft.tasks" :key="t.temp_id">
          <div class="item-head">
            <span class="tag plain">{{ t.priority }}</span>
            <strong>{{ t.title }}</strong>
            <span v-if="t.needs_human_confirmation" class="tag unknown">需人工确认</span>
          </div>
          <p v-if="t.description" class="muted small">{{ t.description }}</p>
          <p class="muted tiny">
            截止：{{ t.due_at ? t.due_at.slice(0, 10) : '政策原文未给出，需自行确认' }}
            <template v-if="t.required_materials"> · 材料：{{ t.required_materials }}</template>
          </p>
        </li>
      </ol>
      <div v-if="draft.notes.length" class="notes">
        <p v-for="n in draft.notes" :key="n" class="muted tiny">· {{ n }}</p>
      </div>
      <div class="actions">
        <button class="btn" :disabled="confirming" @click="confirmDraft">
          {{ confirming ? '确认中…' : `确认并写入任务中心（${draft.tasks.length} 项）` }}
        </button>
        <button class="btn btn-ghost" :disabled="confirming" @click="draft = null">放弃草稿</button>
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

.small {
  font-size: 12px;
}

.tiny {
  font-size: 11px;
}

.row {
  display: flex;
  gap: var(--space-3);
}

.grow {
  flex: 1;
}

.select {
  min-height: 40px;
  padding: 0 var(--space-3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: #fff;
  font-family: inherit;
  font-size: 14px;
  color: var(--c-text);
}

.sub {
  margin: var(--space-4) 0 var(--space-2);
  font-size: 14px;
}

.profile-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-3);
}

.actions {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  margin-top: var(--space-4);
  flex-wrap: wrap;
}

.err-line {
  color: var(--c-not-met);
  font-size: 13px;
  margin: 0;
}

.ok-line {
  color: var(--c-met);
  font-size: 13px;
  margin: var(--space-2) 0 0;
}

.warn-card {
  border-color: #f0dfae;
  background: #fffdf7;
}

.summary {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.counts {
  display: flex;
  gap: var(--space-2);
}

.count {
  padding: 2px 12px;
  border-radius: 999px;
  font-size: 13px;
  border: 1px solid var(--c-border);
}

.count.met {
  color: var(--c-met);
  border-color: #cfe6da;
  background: #f1f8f4;
}

.count.not_met {
  color: var(--c-not-met);
  border-color: #f2d3d0;
  background: #fdf2f1;
}

.count.unknown {
  color: var(--c-unknown);
  border-color: #f0dfae;
  background: #fff8e6;
}

.group-title {
  font-size: 14px;
  margin-bottom: var(--space-3);
}

.group-title.met {
  color: var(--c-met);
}

.group-title.not_met {
  color: var(--c-not-met);
}

.group-title.unknown {
  color: var(--c-unknown);
}

.items,
.draft-tasks {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.items li {
  border-bottom: 1px dashed var(--c-border);
  padding-bottom: var(--space-3);
}

.items li:last-child {
  border-bottom: none;
  padding-bottom: 0;
}

.item-head {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  flex-wrap: wrap;
}

.desc {
  margin: var(--space-2) 0 var(--space-1);
  font-size: 13px;
}

.rationale {
  margin: 0 0 var(--space-2);
  font-size: 13px;
  color: var(--c-text-2);
}

.values {
  display: flex;
  gap: var(--space-4);
  margin-bottom: var(--space-2);
}

.quote {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  background: #fbfcfd;
  border-left: 3px solid var(--c-primary-weak);
  font-size: 12px;
  color: var(--c-text-2);
  line-height: 1.75;
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
  border-color: #cfe6da;
  background: #f1f8f4;
}

.tag.not_met {
  color: var(--c-not-met);
  border-color: #f2d3d0;
  background: #fdf2f1;
}

.tag.unknown {
  color: var(--c-unknown);
  border-color: #f0dfae;
  background: #fff8e6;
}

.tag.plain {
  color: var(--c-text-2);
  background: var(--c-bg);
}

.notes {
  margin-top: var(--space-3);
}

.draft-tasks li {
  padding: var(--space-2) 0;
  border-bottom: 1px dashed var(--c-border);
}

.draft-tasks li:last-child {
  border-bottom: none;
}
</style>
