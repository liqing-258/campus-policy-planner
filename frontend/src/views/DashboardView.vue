<script setup lang="ts">
/**
 * 工作台：用户打开系统第一眼看到的东西。
 *
 * 这一页只回答四个问题——我今天要做什么、哪些快到期了、政策有没有更新、我上次问到哪。
 * 数字全部来自接口（`/tasks/stats`、`/tasks`、`/documents`、`/conversations`、`/plans`），
 * 前端不自己数：自己数的话"待办 5 项"和列表里的 6 行迟早对不上。
 *
 * 不含任何建设进度信息（里程碑 / 接口条数 / 测试项数）。那些在仓库文档里。
 */
import { computed, onMounted, ref } from 'vue'
import {
  api,
  type ConversationRow,
  type DocumentRow,
  type Health,
  type Paged,
  type PlanRow,
  type TaskRow,
  type TaskStats,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import {
  EFFECTIVE_LABEL,
  PLAN_STATUS_LABEL,
  TASK_STATUS_LABEL,
  formatDate,
  formatTime,
} from '../utils/format'

const auth = useAuthStore()

const health = ref<Health | null>(null)
const tasks = ref<TaskRow[]>([])
const conversations = ref<ConversationRow[]>([])
const plans = ref<PlanRow[]>([])
const documents = ref<DocumentRow[]>([])
const stats = ref<TaskStats | null>(null)
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  const [h, t, c, p, d, s] = await Promise.allSettled([
    api.get<Health>('/health'),
    api.get<Paged<TaskRow>>('/tasks?page=1&page_size=6'),
    api.get<Paged<ConversationRow>>('/conversations?page=1&page_size=4'),
    api.get<Paged<PlanRow>>('/plans?page=1&page_size=4'),
    api.get<Paged<DocumentRow>>('/documents?page=1&page_size=4'),
    api.get<TaskStats>('/tasks/stats'),
  ])
  if (h.status === 'fulfilled') health.value = h.value
  if (t.status === 'fulfilled') tasks.value = t.value.items
  if (c.status === 'fulfilled') conversations.value = c.value.items
  if (p.status === 'fulfilled') plans.value = p.value.items
  // 只列能检索到内容的文件：空文本与扫描件留在知识库里，但在工作台上只会让人误点
  if (d.status === 'fulfilled') documents.value = d.value.items.filter((doc) => doc.chunk_count > 0)
  if (s.status === 'fulfilled') stats.value = s.value
  const failure = [h, t, c, p, d, s].find((r) => r.status === 'rejected') as
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

function dueText(task: TaskRow) {
  if (!task.due_at) return '无明确截止'
  return `截止 ${formatDate(task.due_at)}`
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

    <div class="grid">
      <!-- 近期待办 -->
      <section class="card">
        <div class="list-head">
          <h3>我的待办</h3>
          <span class="muted tiny">{{ upcoming.length }} 项在办</span>
        </div>
        <ul v-if="upcoming.length" class="mini-list">
          <li v-for="task in upcoming" :key="task.id">
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
          </li>
        </ul>
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
        <ul v-if="documents.length" class="mini-list">
          <li v-for="doc in documents" :key="doc.id">
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
          </li>
        </ul>
        <p v-else class="empty">{{ loading ? '读取中…' : '知识库还没有文件。' }}</p>
        <router-link class="more" to="/library">打开政策库 →</router-link>
      </section>

      <!-- 我的计划 -->
      <section class="card">
        <div class="list-head">
          <h3>我的计划</h3>
          <span class="muted tiny">{{ plans.length ? `${plans.length} 份` : '' }}</span>
        </div>
        <ul v-if="plans.length" class="mini-list">
          <li v-for="plan in plans" :key="plan.id">
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
          </li>
        </ul>
        <p v-else class="empty">{{ loading ? '读取中…' : '还没有计划。' }}</p>
        <router-link class="more" to="/plans">去计划与任务 →</router-link>
      </section>

      <!-- 最近提问 -->
      <section class="card">
        <div class="list-head">
          <h3>最近提问</h3>
          <span class="muted tiny">{{ conversations.length ? `${conversations.length} 条` : '' }}</span>
        </div>
        <ul v-if="conversations.length" class="mini-list">
          <li v-for="conv in conversations" :key="conv.id">
            <div class="grow">
              <div class="item-title">{{ conv.title }}</div>
              <div class="muted tiny">{{ formatTime(conv.created_at) }}</div>
            </div>
          </li>
        </ul>
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
</style>
