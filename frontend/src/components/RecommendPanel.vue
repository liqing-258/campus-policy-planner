<script setup lang="ts">
/**
 * 工作台上的「为你推荐」（R13）。
 *
 * ## 这一块只画后端算好的东西
 *
 * 卡片上的三态、推荐理由、逐条判定、动作可用性**全部来自 `/recommendations`**，
 * 组件不自己判断"这个学生符不符合"：
 * - 三态聚合规则（不符合优先于无法判定）在后端一处定义，前端照 `status_label` 显示；
 * - 「进行资格匹配」能不能点是 `actions.match.available`（该奖项有没有已复核条件规则），
 *   前端不去猜——猜错了就会把用户送进 2008；
 * - 「生成办理计划」同理，`actions.plan.available` 为假时按钮置灰并显示原因。
 *
 * ## 三个动作都没有另起一套业务
 *
 * - 查看详情 → 指南详情页，没有指南条目时落到知识库的 `?doc=`（既有深链能力）；
 * - 进行资格匹配 → `/match?document_id=&target_group=`，复用资格匹配页；
 * - 生成办理计划 → `POST /plans/draft` + `POST /plans/{id}/confirm`，与匹配页同一对接口、
 *   同一份 payload 映射（`utils/plan.ts`）。草稿仍然要人工点"确认"才落任务，
 *   这里没有绕过确认机制。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ApiError,
  api,
  type DraftData,
  type RecommendCard,
  type RecommendConditionResult,
  type RecommendData,
} from '../api/client'
import { EFFECTIVE_LABEL, evidenceLabel, formatDate } from '../utils/format'
import { toConfirmPayload } from '../utils/plan'

const router = useRouter()

const data = ref<RecommendData | null>(null)
const loading = ref(true)
const error = ref('')
const filter = ref<'all' | 'met' | 'unknown' | 'not_met'>('all')

const draft = ref<DraftData | null>(null)
const draftCard = ref<RecommendCard | null>(null)
const drafting = ref(false)
const confirming = ref(false)
const planMsg = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.get<RecommendData>('/recommendations')
  } catch (err) {
    error.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '推荐加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

const summary = computed(() => data.value?.summary ?? null)

/** 筛选条的四个计数与后端 summary 同源，不在前端重数一遍 */
const filters = computed(() => {
  const s = summary.value
  return [
    { key: 'all' as const, label: '全部', count: s?.total ?? 0 },
    { key: 'met' as const, label: '可能符合', count: s?.met ?? 0 },
    { key: 'unknown' as const, label: '暂无法判断', count: s?.unknown ?? 0 },
    { key: 'not_met' as const, label: '暂不符合', count: s?.not_met ?? 0 },
  ]
})

const visible = computed(() =>
  (data.value?.items ?? []).filter((card) => filter.value === 'all' || card.status === filter.value),
)

/** 画像来源一句话。学生要能知道"这些推荐依据的是哪次提交的画像"。 */
const profileLine = computed(() => {
  const profile = data.value?.profile
  if (!profile) return ''
  if (profile.source === 'none') return '暂无画像记录'
  const when = profile.submitted_at ? formatDate(profile.submitted_at) : '—'
  return `依据 ${when} 的资格匹配画像（${profile.filled}/${profile.total} 项已填）`
})

function openDetail(card: RecommendCard) {
  const path = card.actions.detail.path
  if (path) void router.push(path)
}

function goMatch(card: RecommendCard) {
  if (!card.actions.match.document_id) return
  void router.push({
    path: '/match',
    query: {
      document_id: card.actions.match.document_id,
      // 只有该奖项真有已复核规则时才有 target_group；否则不带，免得落地成空选项
      ...(card.actions.match.target_group ? { target_group: card.actions.match.target_group } : {}),
    },
  })
}

function goAsk(card: RecommendCard) {
  void router.push({ path: '/ask', query: { q: card.ask } })
}

function evidenceText(row: RecommendConditionResult) {
  return `${evidenceLabel(row.evidence.page, row.evidence.section)} · 规则 ${row.rule_code}`
}

/**
 * 生成计划草稿。
 *
 * `match_session_id` **只在画像来自同一份文件的匹配时才带**：后端会按那次匹配里
 * "缺信息"的条件追加「补充信息」待办，带错文件会生成一条与这份政策无关的任务。
 */
async function generatePlan(card: RecommendCard) {
  if (!card.actions.plan.available || !card.actions.plan.document_id) return
  drafting.value = true
  planMsg.value = ''
  error.value = ''
  try {
    const body: Record<string, unknown> = { document_id: card.actions.plan.document_id }
    const profile = data.value?.profile
    if (profile?.session_id && profile.document_id === card.actions.plan.document_id) {
      body.match_session_id = profile.session_id
    }
    draft.value = await api.post<DraftData>('/plans/draft', body)
    draftCard.value = card
  } catch (err) {
    planMsg.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '生成计划失败')
  } finally {
    drafting.value = false
  }
}

async function confirmPlan() {
  if (!draft.value) return
  confirming.value = true
  planMsg.value = ''
  try {
    const plan = await api.post<{ plan_id: string; task_count: number }>(
      `/plans/${draft.value.plan_id}/confirm`,
      toConfirmPayload(draft.value),
    )
    planMsg.value = `已写入任务中心：${plan.task_count} 项待办（计划 ${plan.plan_id}）。`
    draft.value = null
    draftCard.value = null
  } catch (err) {
    planMsg.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '确认失败')
  } finally {
    confirming.value = false
  }
}
</script>

<template>
  <section class="card rec">
    <div class="list-head">
      <h3>为你推荐</h3>
      <span class="muted tiny">{{ profileLine }}</span>
      <button class="btn btn-sm btn-ghost rec-refresh" :disabled="loading" @click="load">
        {{ loading ? '刷新中…' : '刷新推荐' }}
      </button>
    </div>

    <p v-if="error" class="err-line">{{ error }}</p>
    <p v-if="planMsg" class="rec-planmsg">{{ planMsg }}</p>

    <p
      v-for="notice in data?.notices ?? []"
      :key="notice.code"
      :class="['rec-notice', notice.level]"
    >
      {{ notice.text }}
    </p>

    <div v-if="data && data.summary.total" class="chips rec-filters">
      <button
        v-for="item in filters"
        :key="item.key"
        :class="['chip', { on: filter === item.key, dim: !item.count }]"
        @click="filter = item.key"
      >
        {{ item.label }}<span class="count">{{ item.count }}</span>
      </button>
    </div>

    <div v-if="visible.length" class="rec-grid">
      <article v-for="card in visible" :key="card.key" :class="['rec-card', card.status]">
        <header class="rec-card-head">
          <h4 :title="card.title">{{ card.title }}</h4>
          <span :class="['tag', card.status]">{{ card.status_label }}</span>
        </header>

        <div class="rec-meta">
          <span class="tag plain">{{ card.category_label }}</span>
          <span
            :class="['tag', card.document?.effective_status === 'active' ? 'met' : 'plain']"
            :title="`政策有效状态：${EFFECTIVE_LABEL[card.document?.effective_status ?? 'unknown']}`"
          >
            {{ EFFECTIVE_LABEL[card.document?.effective_status ?? 'unknown'] }}
          </span>
          <span class="muted tiny rec-file" :title="card.document?.title ?? ''">
            {{ card.document?.title ?? '未关联政策文件' }}
            <template v-if="card.document?.academic_year"> · {{ card.document.academic_year }}</template>
          </span>
        </div>

        <dl class="rec-fields">
          <dt>适用对象</dt>
          <dd :title="card.audience?.text ?? ''">
            {{ card.audience?.text || '原文未单列适用对象' }}
          </dd>

          <dt>推荐理由</dt>
          <dd class="reason">
            {{ card.reason.text }}
            <ul v-if="card.reason.highlights.length" class="rec-hl">
              <li v-for="(line, index) in card.reason.highlights.slice(0, 3)" :key="index">
                {{ line }}
              </li>
            </ul>
          </dd>

          <dt>主要申请条件</dt>
          <dd :title="card.conditions_preview">
            {{ card.conditions_preview || '原文未单列申请条件' }}
            <span v-if="card.conditions_source === 'policy'" class="rec-src">（原文摘录，规则未复核）</span>
          </dd>
        </dl>

        <p v-if="card.missing_labels.length" class="rec-missing">
          还缺信息：{{ card.missing_labels.join('、') }}
        </p>

        <details v-if="card.conditions.length" class="rec-cond">
          <summary>
            逐条判定 · 符合 {{ card.condition_counts.met }}／{{ card.conditions.length }}
            <span class="muted tiny">（点开看每条的比对值与原文出处）</span>
          </summary>
          <ul>
            <li v-for="row in card.conditions" :key="row.rule_code">
              <div class="rec-cond-head">
                <span :class="['tag', row.status]">{{ row.status_label }}</span>
                <span class="rec-cond-text">{{ row.rationale }}</span>
              </div>
              <div class="muted tiny">{{ evidenceText(row) }}</div>
            </li>
          </ul>
        </details>

        <p v-if="card.note" class="rec-note">{{ card.note }}</p>

        <div class="rec-actions">
          <button
            class="btn btn-sm"
            :disabled="!card.actions.detail.available"
            :title="card.actions.detail.reason ?? '查看政策详情'"
            @click="openDetail(card)"
          >
            查看详情
          </button>
          <button
            class="btn btn-sm btn-ghost"
            :disabled="!card.actions.match.available"
            :title="card.actions.match.reason ?? '带着这份政策去资格匹配'"
            @click="goMatch(card)"
          >
            进行资格匹配
          </button>
          <button
            class="btn btn-sm btn-ghost"
            :disabled="!card.actions.plan.available || drafting"
            :title="card.actions.plan.reason ?? '生成可编辑的办理计划草稿'"
            @click="generatePlan(card)"
          >
            生成办理计划
          </button>
          <button class="btn btn-sm btn-ghost rec-ask" title="带着这个问题去智能问答" @click="goAsk(card)">
            问 AI
          </button>
        </div>

        <p v-if="!card.actions.match.available && card.actions.match.reason" class="rec-hint muted tiny">
          {{ card.actions.match.reason }}
        </p>
      </article>
    </div>

    <!--
      两种"空"必须分开说：
      - 后端一条都没算出来 → 需求 3.6 的「暂未发现匹配度较高的政策」+ 改进建议；
      - 后端算出来了、只是当前筛选下为 0 → 说"这个筛选下没有"，
        否则用户点「可能符合 0」会以为系统真的什么都没给他找。
    -->
    <p v-else-if="!loading && !data?.summary.total" class="empty">
      <strong>{{ data?.empty_title ?? '暂未发现匹配度较高的政策' }}</strong>
      <br />
      {{ data?.empty_hint }}
    </p>
    <p v-else-if="!loading" class="empty">
      当前筛选下没有政策，切回「全部」可以看到其余 {{ data?.summary.total }} 条。
    </p>
    <p v-else class="empty">正在按你的画像筛政策…</p>

    <details v-if="data && (data.excluded.length || data.coverage.length)" class="rec-more">
      <summary>
        未推荐 {{ data.excluded.length }} 条 · 覆盖范围 {{ data.coverage.length }} 类
      </summary>
      <div class="rec-more-body">
        <div v-if="data.excluded.length">
          <h5>没有推荐给你的政策</h5>
          <ul>
            <li v-for="item in data.excluded" :key="item.key">
              <b>{{ item.title }}</b>：{{ item.reason }}
            </li>
          </ul>
        </div>
        <div>
          <h5>需求点名的政策落在哪</h5>
          <ul>
            <li v-for="row in data.coverage" :key="row.label">
              <b>{{ row.label }}</b>
              <span :class="['tag', row.status === 'covered' ? 'plain' : 'unknown']">
                {{ row.status === 'covered' ? '已覆盖' : row.status === 'missing' ? '知识库未收录' : '本次未进推荐' }}
              </span>
              {{ row.note }}
            </li>
          </ul>
        </div>
      </div>
    </details>

    <p class="rec-foot muted tiny">{{ data?.disclaimer }}</p>
  </section>

  <!-- 草稿预览：确认动作与匹配页同一个接口、同一份 payload 映射 -->
  <div v-if="draft" class="rec-mask" @click.self="draft = null">
    <div class="rec-modal">
      <div class="list-head">
        <h3>办理计划草稿</h3>
        <span class="muted tiny">{{ draftCard?.title }}</span>
      </div>
      <p class="muted tiny">
        共 {{ draft.tasks.length }} 项任务，<strong>尚未写入任务中心</strong>：确认后才会生成待办，放弃则什么都不留。
      </p>
      <ul class="rec-draft">
        <li v-for="task in draft.tasks" :key="task.temp_id">
          <span :class="['tag', task.needs_human_confirmation ? 'unknown' : 'plain']">{{ task.priority }}</span>
          <div class="rec-draft-body">
            <div>{{ task.title }}</div>
            <div class="muted tiny">
              {{ task.due_at ? `截止 ${formatDate(task.due_at)}` : '无明确截止' }}
              <template v-if="task.note"> · {{ task.note }}</template>
            </div>
          </div>
        </li>
      </ul>
      <p v-if="planMsg" class="rec-planmsg">{{ planMsg }}</p>
      <div class="rec-actions">
        <button class="btn btn-sm" :disabled="confirming" @click="confirmPlan">
          {{ confirming ? '写入中…' : '确认并写入任务中心' }}
        </button>
        <button class="btn btn-sm btn-ghost" :disabled="confirming" @click="draft = null">放弃草稿</button>
        <router-link class="btn btn-sm btn-ghost" to="/plans">去计划与任务</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rec-refresh {
  margin-left: auto;
}

.rec-notice {
  margin: 0 0 var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-control);
  font-size: 12.5px;
  line-height: 1.6;
  background: var(--c-surface-2);
  color: var(--c-text-2);
  border: 1px solid var(--c-border);
}

/* 未填画像用卡片级警示：它决定了整块推荐的可信度，不该只是一行灰字 */
.rec-notice.warn {
  background: var(--c-warn-bg);
  border-color: var(--c-warn-border);
  color: var(--c-unknown);
}

.rec-planmsg {
  margin: 0 0 var(--space-2);
  font-size: 12.5px;
  color: var(--c-primary);
}

.rec-filters {
  margin-bottom: var(--space-3);
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
  opacity: 0.7;
}

.chip .count {
  font-size: 11px;
  color: var(--c-dim);
  font-variant-numeric: tabular-nums;
}

.chip.on .count {
  color: var(--c-primary);
}

.rec-grid {
  display: grid;
  gap: var(--space-3);
  /* 卡片密度跟着容器宽度走：1440 视口三列，1024 两列，窄屏一列 */
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
}

.rec-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-card);
  background: var(--c-surface);
  /* 左侧 3px 色条直接用三态色：一眼分辨"可能符合 / 还是判不了"，
     比在标题旁放一个小标签更快 */
  box-shadow: inset 3px 0 0 var(--c-border-strong);
}

.rec-card.met {
  box-shadow: inset 3px 0 0 var(--c-met);
}

.rec-card.unknown {
  box-shadow: inset 3px 0 0 var(--c-unknown);
}

.rec-card.not_met {
  box-shadow: inset 3px 0 0 var(--c-not-met);
}

.rec-card-head {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
}

.rec-card-head h4 {
  margin: 0;
  font-size: 14.5px;
  line-height: 1.45;
  flex: 1;
  min-width: 0;
}

.rec-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.rec-file {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rec-fields {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px var(--space-2);
  margin: 0;
  font-size: 12.5px;
  line-height: 1.6;
}

.rec-fields dt {
  color: var(--c-dim);
  white-space: nowrap;
}

.rec-fields dd {
  margin: 0;
  color: var(--c-text);
  /* 三行截断：卡片是"要不要点进去"的判断依据，不是阅读区 */
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.rec-fields dd.reason {
  color: var(--c-text);
}

.rec-hl {
  margin: 4px 0 0;
  padding-left: 1.1em;
  color: var(--c-text-2);
  font-size: 12px;
}

.rec-hl li {
  margin: 2px 0;
}

.rec-src {
  color: var(--c-dim);
  font-size: 11.5px;
}

.rec-missing {
  margin: 0;
  font-size: 12px;
  color: var(--c-unknown);
}

.rec-cond summary {
  cursor: pointer;
  font-size: 12.5px;
  color: var(--c-primary);
}

.rec-cond ul {
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 6px;
}

.rec-cond li {
  padding: 6px 8px;
  border-radius: var(--radius-control);
  background: var(--c-elev-1);
}

.rec-cond-head {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  font-size: 12px;
  line-height: 1.55;
}

.rec-cond-text {
  flex: 1;
  min-width: 0;
}

.rec-note {
  margin: 0;
  padding: 6px 8px;
  border-radius: var(--radius-control);
  background: var(--c-elev-1);
  color: var(--c-text-2);
  font-size: 12px;
  line-height: 1.6;
}

.rec-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: auto;
  padding-top: var(--space-2);
}

.rec-ask {
  margin-left: auto;
}

.rec-hint {
  margin: 0;
  line-height: 1.5;
}

.rec-more {
  margin-top: var(--space-3);
  font-size: 12.5px;
}

.rec-more summary {
  cursor: pointer;
  color: var(--c-text-2);
}

.rec-more-body {
  display: grid;
  gap: var(--space-3);
  margin-top: var(--space-2);
  padding: var(--space-3);
  border-radius: var(--radius-control);
  background: var(--c-elev-1);
}

.rec-more-body h5 {
  margin: 0 0 6px;
  font-size: 12.5px;
  color: var(--c-text);
}

.rec-more-body ul {
  margin: 0;
  padding-left: 1.1em;
  color: var(--c-text-2);
  line-height: 1.75;
}

.rec-foot {
  margin: var(--space-3) 0 0;
  line-height: 1.6;
}

/* ---------- 草稿弹层 ---------- */
.rec-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: rgba(8, 26, 22, 0.42);
}

.rec-modal {
  width: min(680px, 100%);
  max-height: 82vh;
  overflow: auto;
  padding: var(--space-4);
  border-radius: var(--radius-card);
  border: 1px solid var(--c-border);
  background: var(--c-surface);
  box-shadow: var(--shadow-card);
}

.rec-draft {
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 6px;
}

.rec-draft li {
  display: flex;
  gap: var(--space-2);
  align-items: flex-start;
  padding: 8px;
  border-radius: var(--radius-control);
  background: var(--c-elev-1);
  font-size: 12.5px;
}

.rec-draft-body {
  flex: 1;
  min-width: 0;
  display: grid;
  gap: 2px;
}
</style>
