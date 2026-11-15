<script setup lang="ts">
/**
 * 工作台：用户打开系统第一眼看到的东西。
 *
 * 这一页只回答四个问题——我今天要做什么、哪些快到期了、政策有没有更新、我上次问到哪。
 * 数字全部来自接口（`/tasks/stats`、`/tasks`、`/documents`、`/conversations`、`/plans`），
 * 前端不自己数：自己数的话"待办 5 项"和列表里的 6 行迟早对不上。
 *
 * 不含任何建设进度信息（里程碑 / 接口条数 / 测试项数）。那些在仓库文档里。
 *
 * ## R9：四个列表改成自动轮播
 *
 * 每张卡的列表都套 `AutoScrollList`：内容超出高度就慢慢上下滚，鼠标移进去停住，
 * 点条目跳到对应页面。要点在"**停下**"——需求写明"鼠标所在的**板块**中的事项
 * 自动停止滚动"，所以暂停是绑在容器上的，不是绑在单条上的：指针在卡片任意
 * 位置都会停，这样才能从容点中想看的那一条。
 *
 * 一次多取一些：`page_size` 从 4/6 提到 12/14。只有 4 条时高度不够，永远不滚。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  api,
  type CalendarAgenda,
  type CalendarItem,
  type ConversationRow,
  type DocumentRow,
  type Health,
  type Paged,
  type PlanRow,
  type TaskRow,
  type TaskStats,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import AutoScrollList from '../components/AutoScrollList.vue'
import RecommendPanel from '../components/RecommendPanel.vue'
import {
  EFFECTIVE_LABEL,
  PLAN_STATUS_LABEL,
  TASK_STATUS_LABEL,
  formatDate,
  formatTime,
} from '../utils/format'

const auth = useAuthStore()
const router = useRouter()

const health = ref<Health | null>(null)
const tasks = ref<TaskRow[]>([])
const conversations = ref<ConversationRow[]>([])
const plans = ref<PlanRow[]>([])
const documents = ref<DocumentRow[]>([])
const stats = ref<TaskStats | null>(null)
const agenda = ref<CalendarAgenda | null>(null)
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  const [h, t, c, p, d, s, g] = await Promise.allSettled([
    api.get<Health>('/health'),
    api.get<Paged<TaskRow>>('/tasks?page=1&page_size=14'),
    api.get<Paged<ConversationRow>>('/conversations?page=1&page_size=10'),
    api.get<Paged<PlanRow>>('/plans?page=1&page_size=10'),
    api.get<Paged<DocumentRow>>('/documents?page=1&page_size=10'),
    api.get<TaskStats>('/tasks/stats'),
    // 窗口天数固定 7：与 /tasks/stats 的 due_soon 同一口径，
    // 两处不一致时用户会看到"7 天内到期 3 项"但这里只列 2 条
    api.get<CalendarAgenda>('/calendar/agenda?days=7'),
  ])
  if (h.status === 'fulfilled') health.value = h.value
  if (t.status === 'fulfilled') tasks.value = t.value.items
  if (c.status === 'fulfilled') conversations.value = c.value.items
  if (p.status === 'fulfilled') plans.value = p.value.items
  // 只列能检索到内容的文件：空文本与扫描件留在知识库里，但在工作台上只会让人误点
  if (d.status === 'fulfilled') documents.value = d.value.items.filter((doc) => doc.chunk_count > 0)
  if (s.status === 'fulfilled') stats.value = s.value
  if (g.status === 'fulfilled') agenda.value = g.value
  const failure = [h, t, c, p, d, s, g].find((r) => r.status === 'rejected') as
    | PromiseRejectedResult
    | undefined
  if (failure) {
    error.value = failure.reason instanceof Error ? failure.reason.message : '部分数据未能加载'
  }
  loading.value = false
})

/** 未完成的任务按截止时间排，没截止的放最后——"今天该做什么"才对得上直觉 */
const upcoming = computed(() => {
  const open = tasks.value.filter((t) => t.status === 'todo' || t.status === 'doing')
  return [...open].sort((a, b) => {
    if (!a.due_at) return 1
    if (!b.due_at) return -1
    return a.due_at < b.due_at ? -1 : 1
  })
})

const statusBars = computed(() => {
  if (!stats.value) return []
  const rows = Object.entries(stats.value.by_status) as [string, number][]
  const max = Math.max(1, ...rows.map(([, count]) => count))
  return rows
    .filter(([, count]) => count > 0)
    .map(([status, count]) => ({
      status,
      label: TASK_STATUS_LABEL[status] ?? status,
      count,
      percent: Math.round((count / max) * 100),
    }))
})

const greeting = computed(() => {
  const hour = new Date().getHours()
  if (hour < 6) return '凌晨好'
  if (hour < 12) return '上午好'
  if (hour < 14) return '中午好'
  if (hour < 18) return '下午好'
  return '晚上好'
})

const today = computed(() =>
  new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }),
)

const overdueTone = computed(() => ((stats.value?.overdue ?? 0) > 0 ? 'danger' : 'ok'))

/** 工作台上的日程：逾期 + 未来 7 天 + 待确认年份三类，都来自 /calendar/agenda */
const agendaOverdue = computed(() => agenda.value?.overdue ?? [])
const agendaSoon = computed(() => agenda.value?.soon ?? [])
const agendaUndated = computed(() => agenda.value?.undated ?? [])

function dueText(task: TaskRow) {
  if (!task.due_at) return '无明确截止'
  return `截止 ${formatDate(task.due_at)}`
}

/**
 * 点条目跳到对应页面。
 *
 * 这里**不做"跳到那一条"的深链**（`/ask?conv=xxx`）：问答页与计划页都还没有
 * 接受定位参数，先把参数拼上去只会让用户以为跳错了——落地在列表顶部，
 * 而他要找的那一条在下面第十行。跳页、由用户自己点开，比伪精确定位诚实。
 */
function go(path: string) {
  void router.push(path)
}

/**
 * 点工作台上的日程：有日期的落到那个月的日历，没有日期的落到日历页的
 * 「待确认」栏。不要统一跳 `/calendar`——用户点的是"11 月 20 日那条"，
 * 落到当月才叫跳对了。
 */
function goCalendar(item: CalendarItem) {
  if (!item.date) {
    void router.push('/calendar')
    return
  }
  void router.push({ path: '/calendar', query: { month: item.date.slice(0, 7) } })
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>{{ greeting }}，{{ auth.displayName }}</h2>
      <span class="sub">{{ today }}</span>
      <div class="tail">
        <router-link class="btn btn-sm" to="/ask">提个问题</router-link>
        <router-link class="btn btn-sm btn-ghost" to="/match">查申请资格</router-link>
      </div>
    </div>

    <p v-if="error" class="err-line">{{ error }}</p>

    <!-- 四个数：全部来自 /tasks/stats，口径只在那一个地方定义 -->
    <div class="grid grid-4">
      <div class="stat accent">
        <span class="k">待办事项</span>
        <span class="v">{{ stats?.open ?? '—' }}<small>项</small></span>
        <span class="d">含进行中，不含已完成</span>
      </div>
      <div class="stat info">
        <span class="k">7 天内到期</span>
        <span class="v">{{ stats?.due_soon ?? '—' }}<small>项</small></span>
        <span class="d">需要本周安排</span>
      </div>
      <div class="stat" :class="overdueTone">
        <span class="k">已逾期</span>
        <span class="v">{{ stats?.overdue ?? '—' }}<small>项</small></span>
        <span class="d">{{ (stats?.overdue ?? 0) > 0 ? '建议优先处理' : '暂无逾期' }}</span>
      </div>
      <div class="stat">
        <span class="k">完成率</span>
        <span class="v">{{ stats ? Math.round(stats.completion_rate * 100) : '—' }}<small>%</small></span>
        <span class="d">分母不含已取消</span>
      </div>
    </div>

    <!--
      「为你推荐」（R13）：整块单独一个组件，因为它有自己的一次请求、一组筛选
      和一套草稿弹层。放在四个统计数下面、六个列表卡上面——它是"我接下来该做什么"
      的答案，比"我的待办"更靠前，但低于"待办 5 项"这类一眼可读的状态数。
    -->
    <!--
      近期重要日程（R14）：逾期 → 未来 7 天 → 年份待确认，顺序就是处理顺序。
      三类都是"现在能动手的事"，与下面的「我的待办」不同：后者是全量列表，
      这里是按时间窗口筛过的。
    -->
    <section class="card agenda-card">
      <div class="list-head">
        <h3>近期重要日程</h3>
        <span class="muted tiny">
          未来 7 天 {{ agenda?.counts.soon ?? 0 }} 项
          <template v-if="(agenda?.counts.overdue ?? 0) > 0">
            · <span class="danger-text">逾期 {{ agenda?.counts.overdue }} 项</span>
          </template>
        </span>
        <router-link class="agenda-more" to="/calendar">打开校园日历 →</router-link>
      </div>

      <div v-if="agendaOverdue.length || agendaSoon.length" class="agenda-grid">
        <div v-if="agendaOverdue.length" class="agenda-col">
          <h4 class="agenda-h danger-text">已逾期（{{ agendaOverdue.length }}）</h4>
          <div
            v-for="item in agendaOverdue.slice(0, 4)"
            :key="item.id"
            class="arow overdue"
            role="button"
            tabindex="0"
            @click="goCalendar(item)"
            @keydown.enter="goCalendar(item)"
          >
            <span class="adate mono">{{ item.date?.slice(5) }}</span>
            <span class="atitle">{{ item.title }}</span>
            <span class="tag not-met">逾期</span>
          </div>
        </div>
        <div v-if="agendaSoon.length" class="agenda-col">
          <h4 class="agenda-h">未来 7 天（{{ agendaSoon.length }}）</h4>
          <div
            v-for="item in agendaSoon.slice(0, 4)"
            :key="item.id"
            class="arow"
            role="button"
            tabindex="0"
            @click="goCalendar(item)"
            @keydown.enter="goCalendar(item)"
          >
            <span class="adate mono">{{ item.date?.slice(5) }}</span>
            <span class="atitle">{{ item.title }}</span>
            <span class="tag plain">{{ item.category_label }}</span>
          </div>
        </div>
      </div>
      <p v-else-if="agenda" class="empty small">
        未来 7 天没有到期的待办。
      </p>
      <p v-else class="empty small">{{ loading ? '读取中…' : '日程暂不可用' }}</p>

      <div v-if="agendaUndated.length" class="undated-box">
        <div class="undated-head">
          <strong>年份待确认 {{ agenda?.counts.undated_year_missing ?? 0 }} 条</strong>
          <span class="muted tiny">
            政策原文只给了月日、没给年份。系统不替你补，补完才会进入日历。
          </span>
        </div>
        <div class="uchips">
          <button
            v-for="item in agendaUndated"
            :key="item.id"
            class="uchip"
            :title="item.title"
            @click="goCalendar(item)"
          >
            <span class="mono">{{ item.raw_text }}</span>
            <span class="utext">{{ item.title }}</span>
          </button>
        </div>
      </div>
    </section>

    <RecommendPanel />

    <div class="grid">
      <!-- 近期待办 -->
      <section class="card">
        <div class="list-head">
          <h3>我的待办</h3>
          <span class="muted tiny">
            {{ upcoming.length }} 项在办<template v-if="upcoming.length > 4"> · 悬停暂停</template>
          </span>
        </div>
        <AutoScrollList v-if="upcoming.length">
          <div
            v-for="task in upcoming"
            :key="task.id"
            class="row"
            role="button"
            tabindex="0"
            :title="`去「计划与任务」查看 ${task.title}`"
            @click="go('/plans')"
            @keydown.enter="go('/plans')"
            @keydown.space.prevent="go('/plans')"
          >
            <span class="tag" :class="task.status === 'doing' ? 'info' : 'plain'">
              {{ TASK_STATUS_LABEL[task.status] ?? task.status }}
            </span>
            <div class="grow">
              <div class="item-title">{{ task.title }}</div>
              <div class="muted tiny">
                {{ dueText(task) }}
                <template v-if="task.needs_human_confirmation"> · 需人工确认日期</template>
              </div>
            </div>
            <span class="tag plain">{{ task.priority }}</span>
          </div>
        </AutoScrollList>
        <p v-else-if="loading" class="empty">读取中…</p>
        <p v-else class="empty">
          名下暂无待办。先做一次「资格匹配」并确认计划，任务会自动生成。
        </p>
        <router-link v-if="upcoming.length" class="more" to="/plans">去任务中心 →</router-link>
      </section>

      <!-- 政策速览 -->
      <section class="card">
        <div class="list-head">
          <h3>政策速览</h3>
          <span class="muted tiny">
            {{ health?.document_count ?? 0 }} 份文件 · {{ health?.chunk_count ?? 0 }} 个片段
          </span>
        </div>
        <AutoScrollList v-if="documents.length">
          <div
            v-for="doc in documents"
            :key="doc.id"
            class="row"
            role="button"
            tabindex="0"
            :title="`去「政策知识库」查看 ${doc.title}`"
            @click="go('/library')"
            @keydown.enter="go('/library')"
            @keydown.space.prevent="go('/library')"
          >
            <div class="grow">
              <div class="item-title">{{ doc.title }}</div>
              <div class="muted tiny">
                {{ doc.department ?? '未标注部门' }}
                <template v-if="doc.academic_year"> · {{ doc.academic_year }}</template>
              </div>
            </div>
            <span class="tag" :class="doc.effective_status === 'active' ? 'met' : 'plain'">
              {{ EFFECTIVE_LABEL[doc.effective_status] ?? doc.effective_status }}
            </span>
          </div>
        </AutoScrollList>
        <p v-else class="empty">{{ loading ? '读取中…' : '知识库还没有文件。' }}</p>
        <router-link class="more" to="/library">打开政策库 →</router-link>
      </section>

      <!-- 我的计划 -->
      <section class="card">
        <div class="list-head">
          <h3>我的计划</h3>
          <span class="muted tiny">{{ plans.length ? `${plans.length} 份` : '' }}</span>
        </div>
        <AutoScrollList v-if="plans.length">
          <div
            v-for="plan in plans"
            :key="plan.id"
            class="row"
            role="button"
            tabindex="0"
            :title="`去「计划与任务」查看 ${plan.title}`"
            @click="go('/plans')"
            @keydown.enter="go('/plans')"
            @keydown.space.prevent="go('/plans')"
          >
            <span
              class="tag"
              :class="plan.status === 'confirmed' ? 'met' : plan.status === 'draft' ? 'unknown' : 'plain'"
            >
              {{ PLAN_STATUS_LABEL[plan.status] ?? plan.status }}
            </span>
            <div class="grow">
              <div class="item-title">{{ plan.title }}</div>
              <div class="muted tiny">
                {{ plan.confirmed_at ? `确认于 ${formatTime(plan.confirmed_at)}` : `创建于 ${formatTime(plan.created_at)}` }}
              </div>
            </div>
          </div>
        </AutoScrollList>
        <p v-else class="empty">{{ loading ? '读取中…' : '还没有计划。' }}</p>
        <router-link class="more" to="/plans">去计划与任务 →</router-link>
      </section>

      <!-- 最近提问 -->
      <section class="card">
        <div class="list-head">
          <h3>最近提问</h3>
          <span class="muted tiny">{{ conversations.length ? `${conversations.length} 条` : '' }}</span>
        </div>
        <AutoScrollList v-if="conversations.length">
          <div
            v-for="conv in conversations"
            :key="conv.id"
            class="row"
            role="button"
            tabindex="0"
            :title="`去「智能问答」继续 ${conv.title}`"
            @click="go('/ask')"
            @keydown.enter="go('/ask')"
            @keydown.space.prevent="go('/ask')"
          >
            <div class="grow">
              <div class="item-title">{{ conv.title }}</div>
              <div class="muted tiny">{{ formatTime(conv.created_at) }}</div>
            </div>
            <span class="row-arrow" aria-hidden="true">→</span>
          </div>
        </AutoScrollList>
        <p v-else class="empty">{{ loading ? '读取中…' : '还没有提问记录。' }}</p>
        <router-link class="more" to="/ask">去智能问答 →</router-link>
      </section>

      <!-- 任务分布 -->
      <section class="card">
        <div class="list-head">
          <h3>任务分布</h3>
          <span class="muted tiny">按当前账号统计</span>
        </div>
        <template v-if="stats && stats.total > 0">
          <ul class="bars">
            <li v-for="bar in statusBars" :key="bar.status">
              <span class="bar-label">{{ bar.label }}</span>
              <span class="bar-track">
                <span :class="['bar-fill', bar.status]" :style="{ width: `${bar.percent}%` }" />
              </span>
              <span class="bar-count mono">{{ bar.count }}</span>
            </li>
          </ul>
          <p class="muted tiny bar-foot">
            计划：草稿 {{ stats.plans.draft ?? 0 }} · 已确认 {{ stats.plans.confirmed ?? 0 }} ·
            已归档 {{ stats.plans.archived ?? 0 }}
          </p>
        </template>
        <p v-else class="empty">
          {{ loading ? '读取中…' : '这个账号名下还没有任务。管理员不办理学生事务，看到 0 属正常。' }}
        </p>
      </section>

      <!-- 服务状态：只给学生/管理员看最必要的一条，不放版本号与模式名 -->
      <section class="card">
        <div class="list-head">
          <h3>服务状态</h3>
          <span class="muted tiny">来自 /health</span>
        </div>
        <dl v-if="health" class="kv">
          <dt>数据库</dt>
          <dd>
            <span class="tag" :class="health.db === 'ok' ? 'met' : 'not-met'">
              {{ health.db === 'ok' ? '正常' : '不可用' }}
            </span>
          </dd>
          <dt>问答能力</dt>
          <dd>
            <span class="tag" :class="health.llm_mode === 'llm' ? 'met' : 'unknown'">
              {{ health.llm_mode === 'llm' ? '已接入模型' : '仅检索原文' }}
            </span>
          </dd>
          <dt>检索方式</dt>
          <dd class="small">
            {{ health.embedding_mode === 'vector' ? '向量 + 关键词混合' : '关键词（BM25）' }}
          </dd>
          <dt>政策文件</dt>
          <dd class="small">{{ health.document_count }} 份 · {{ health.chunk_count }} 个片段</dd>
        </dl>
        <p v-else class="empty">{{ loading ? '读取中…' : '未取到服务状态' }}</p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.grow {
  flex: 1;
  min-width: 0;
}

.bar-foot {
  margin: var(--space-3) 0 0;
}

/* ---------- 近期重要日程（R14） ---------- */
.danger-text {
  color: var(--c-not-met);
  font-weight: 600;
}

.agenda-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.agenda-more {
  margin-left: auto;
  font-size: 12px;
  color: var(--c-primary);
}

.agenda-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
}

.agenda-h {
  margin: 0 0 var(--space-1);
  font-size: 12px;
  color: var(--c-text-2);
  font-weight: 600;
}

.arow {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  padding: 5px var(--space-2);
  border-radius: var(--radius-control);
  cursor: pointer;
}

.arow:hover {
  background: var(--c-surface-2);
}

.arow.overdue {
  border-left: 2px solid var(--c-not-met);
}

.adate {
  flex: none;
  font-size: 11.5px;
  color: var(--c-text-2);
}

.atitle {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.undated-box {
  border-top: 1px dashed var(--c-border);
  padding-top: var(--space-2);
}

.undated-head {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-bottom: var(--space-2);
}

.undated-head strong {
  font-size: 12.5px;
  color: var(--c-unknown);
}

.uchips {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.uchip {
  display: flex;
  align-items: baseline;
  gap: 6px;
  max-width: 100%;
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid var(--c-warn-border);
  background: var(--c-warn-bg);
  color: var(--c-text);
  font-family: inherit;
  font-size: 11.5px;
  cursor: pointer;
}

.uchip .mono {
  color: var(--c-unknown);
  font-size: 11px;
  flex: none;
}

.utext {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 900px) {
  .agenda-grid {
    grid-template-columns: 1fr;
  }
}

/* 列表行：原来是 <li>，现在要在自动滚动的容器里既当列表项又当按钮。
   用 div + role="button"，是因为 <li> 套在 div 里语义就断了，
   而这里点击行为才是主要的。 */
.row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2);
  border-radius: var(--radius-control);
  cursor: pointer;
  transition: background 0.14s ease, box-shadow 0.14s ease;
}

.row:hover {
  background: var(--c-surface-2);
  /* 左侧 2px 色条：光标在哪一行，靠它比整行变亮更明确 */
  box-shadow: inset 2px 0 0 var(--c-primary);
}

.row:active {
  background: var(--c-surface-3);
}

/* 行内标题在 hover 时把颜色提亮 + 显示箭头，暗示"可以点" */
.row .item-title {
  font-size: 13.5px;
  line-height: 1.5;
  transition: color 0.14s ease;
}

.row:hover .item-title {
  color: var(--c-primary);
}

.row-arrow {
  color: var(--c-dim);
  font-size: 12px;
  opacity: 0;
  transform: translateX(-4px);
  transition: opacity 0.14s ease, transform 0.14s ease;
  flex: none;
  align-self: center;
}

.row:hover .row-arrow {
  opacity: 1;
  transform: none;
}
</style>
