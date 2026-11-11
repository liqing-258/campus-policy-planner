<script setup lang="ts">
/**
 * 工作台。
 *
 * M2 时这里是"骨架 + 无数据就显示暂无"。M3–M6 落地后，每个格子都必须有真实来源：
 * 计数来自 /health，待办来自 /tasks，统计来自 /tasks/stats（M6），
 * 最近提问来自 /conversations。拿不到就显示"暂无"或错误，不用 mock 数据填空。
 *
 * M6 新增的统计卡**不自己数**：完成率、逾期数都来自 `/tasks/stats`，
 * 口径只在那一个地方定义。前端自己算的话，"待办 5 项"和列表里的 6 行迟早对不上。
 */
import { computed, onMounted, ref } from 'vue'
import {
  api,
  type ConversationRow,
  type Health,
  type Paged,
  type PlanRow,
  type TaskRow,
  type TaskStats,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import { TASK_STATUS_LABEL, formatDate, formatTime } from '../utils/format'

const auth = useAuthStore()

const health = ref<Health | null>(null)
const todos = ref<TaskRow[]>([])
const todoTotal = ref(0)
const conversations = ref<ConversationRow[]>([])
const plans = ref<PlanRow[]>([])
const stats = ref<TaskStats | null>(null)
const loading = ref(true)
const error = ref('')

const milestones = [
  { id: 'M1', title: '文档基线', detail: '设计说明书 · 接口清单 · 数据模型 · 评测口径', state: '已完成' },
  { id: 'M2', title: '工程可跑', detail: '应用工厂 · 登录 · 健康检查 · 建表脚本', state: '已完成' },
  { id: 'M3', title: '知识库可用', detail: '上传 · 解析 · 结构切分 · BM25 索引', state: '已完成' },
  { id: 'M4', title: '问答可信', detail: '三道闸 · 编号引用 · 拒答 · 24 条评测集', state: '已完成' },
  { id: 'M5', title: '闭环可演', detail: '规则抽取 · 三态匹配 · 计划草稿 · 任务中心', state: '已完成' },
  { id: 'M6', title: '体验与质量', detail: '节点轨迹 · 多轮上下文 · 注入防护 · 版本冲突 · 222 项测试', state: '已完成' },
]

onMounted(async () => {
  try {
    const [h, t, c, p, s] = await Promise.allSettled([
      api.get<Health>('/health'),
      api.get<Paged<TaskRow>>('/tasks?status=todo&page=1&page_size=5'),
      api.get<Paged<ConversationRow>>('/conversations?page=1&page_size=4'),
      api.get<Paged<PlanRow>>('/plans?page=1&page_size=4'),
      api.get<TaskStats>('/tasks/stats'),
    ])
    if (h.status === 'fulfilled') health.value = h.value
    if (t.status === 'fulfilled') {
      todos.value = t.value.items
      todoTotal.value = t.value.total
    }
    if (c.status === 'fulfilled') conversations.value = c.value.items
    if (p.status === 'fulfilled') plans.value = p.value.items
    if (s.status === 'fulfilled') stats.value = s.value
    const failure = [h, t, c, p, s].find((r) => r.status === 'rejected') as PromiseRejectedResult | undefined
    if (failure) {
      error.value = failure.reason instanceof Error ? failure.reason.message : '部分数据未能加载'
    }
  } finally {
    loading.value = false
  }
})

/** 状态分布的条形图数据。宽度按最大值归一，避免"1 项占满一条"的视觉误导。 */
const statusBars = computed(() => {
  if (!stats.value) return []
  const rows = Object.entries(stats.value.by_status)
  const max = Math.max(1, ...rows.map(([, count]) => count))
  return rows.map(([status, count]) => ({
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

const confirmedPlans = computed(() => plans.value.filter((p) => p.status === 'confirmed').length)

</script>

<template>
  <div class="page">
    <section class="card welcome">
      <h2>{{ greeting }}，{{ auth.displayName }}</h2>
      <p class="muted">
        当前版本 <b>v{{ health?.version ?? '—' }}</b>，
        知识库有 <b>{{ health?.document_count ?? 0 }}</b> 份政策、
        <b>{{ health?.chunk_count ?? 0 }}</b> 个可检索片段。
        你可以直接提问，也可以先做一次资格匹配再生成申请计划。
      </p>
      <div class="quick">
        <router-link class="quick-link" to="/ask">去提问</router-link>
        <router-link class="quick-link" to="/match">做资格匹配</router-link>
        <router-link class="quick-link" to="/library">看政策库</router-link>
      </div>
    </section>

    <p v-if="error" class="err-line">{{ error }}</p>

    <div class="grid">
      <section class="card">
        <h3>系统状态</h3>
        <dl v-if="health" class="kv">
          <dt>版本</dt><dd class="mono">{{ health.version }}</dd>
          <dt>数据库</dt><dd>{{ health.db }}</dd>
          <dt>模型模式</dt>
          <dd>
            <span :class="['pill', health.llm_mode]">{{ health.llm_mode }}</span>
            <span v-if="health.llm_model" class="muted tiny"> {{ health.llm_model }}</span>
          </dd>
          <dt>检索模式</dt><dd class="mono">{{ health.embedding_mode }}</dd>
          <dt>政策文件</dt><dd>{{ health.document_count }} 份</dd>
          <dt>文本片段</dt><dd>{{ health.chunk_count }} 条</dd>
        </dl>
        <p v-else class="muted">{{ loading ? '读取中…' : '未取到健康检查' }}</p>
      </section>

      <section class="card milestones">
        <h3>里程碑进度</h3>
        <ol>
          <li v-for="m in milestones" :key="m.id" class="done">
            <span class="badge">{{ m.id }}</span>
            <div>
              <strong>{{ m.title }}</strong>
              <div class="muted small">{{ m.detail }}</div>
            </div>
            <span class="state">{{ m.state }}</span>
          </li>
        </ol>
      </section>

      <section class="card">
        <div class="list-head">
          <h3>我的待办</h3>
          <span class="muted tiny">{{ todoTotal }} 项</span>
        </div>
        <ul v-if="todos.length" class="mini-list">
          <li v-for="t in todos" :key="t.id">
            <span class="tag plain">{{ t.priority }}</span>
            <div>
              <div class="item-title">{{ t.title }}</div>
              <div class="muted tiny">
                截止 {{ t.due_at ? formatDate(t.due_at) : '未定' }}
              </div>
            </div>
          </li>
        </ul>
        <p v-else class="muted small">
          暂无待办。做一次资格匹配并确认计划后，任务会出现在这里。
        </p>
        <router-link v-if="todos.length" class="more" to="/plans">去任务中心 →</router-link>
      </section>

      <section class="card">
        <div class="list-head">
          <h3>最近提问</h3>
          <span class="muted tiny">{{ conversations.length ? `${conversations.length} 条` : '' }}</span>
        </div>
        <ul v-if="conversations.length" class="mini-list">
          <li v-for="c in conversations" :key="c.id">
            <div>
              <div class="item-title">{{ c.title }}</div>
              <div class="muted tiny">{{ formatTime(c.created_at) }}</div>
            </div>
          </li>
        </ul>
        <p v-else class="muted small">还没有提问记录。到「智能问答」问一个试试。</p>
        <router-link v-if="conversations.length" class="more" to="/ask">继续问 →</router-link>
      </section>

      <section class="card">
        <div class="list-head">
          <h3>任务分布</h3>
          <span class="muted tiny">来自 /tasks/stats</span>
        </div>
        <template v-if="stats">
          <div class="stat-row">
            <div class="stat">
              <span class="stat-num">{{ stats.open }}</span>
              <span class="muted tiny">未完成</span>
            </div>
            <div class="stat">
              <span class="stat-num">{{ stats.due_soon }}</span>
              <span class="muted tiny">7 天内到期</span>
            </div>
            <div class="stat">
              <span :class="['stat-num', stats.overdue ? 'danger' : '']">{{ stats.overdue }}</span>
              <span class="muted tiny">已逾期</span>
            </div>
            <div class="stat">
              <span class="stat-num">{{ (stats.completion_rate * 100).toFixed(0) }}%</span>
              <span class="muted tiny">完成率（不含已取消）</span>
            </div>
          </div>
          <ul class="bars">
            <li v-for="bar in statusBars" :key="bar.status">
              <span class="bar-label">{{ bar.label }}</span>
              <span class="bar-track">
                <span :class="['bar-fill', bar.status]" :style="{ width: `${bar.percent}%` }" />
              </span>
              <span class="bar-count mono">{{ bar.count }}</span>
            </li>
          </ul>
          <p class="muted tiny">
            计划：草稿 {{ stats.plans.draft ?? 0 }} · 已确认 {{ stats.plans.confirmed ?? 0 }} ·
            已归档 {{ stats.plans.archived ?? 0 }}
          </p>
        </template>
        <p v-else class="muted small">{{ loading ? '读取中…' : '未取到统计' }}</p>
      </section>

      <section class="card next">
        <h3>阶段说明</h3>
        <p class="muted small">
          已确认计划 <b>{{ stats?.plans.confirmed ?? confirmedPlans }}</b> 份。
          当前 <b>v{{ health?.version ?? '—' }}</b> 覆盖 M1–M6：知识库、问答、匹配、计划、任务、
          节点轨迹全部可用，接口 31 条、测试 222 项。
        </p>
        <p class="muted small">
          下一步 <b>M7</b>：干净目录 10 分钟复现、技术文档压缩到 30 页内、录 5–8 分钟演示视频。
        </p>
        <p class="muted tiny">
          已知限制（不粉饰）：检索只有 BM25，同义改写仍召回不到；引用只校验"编号在范围内"，
          不校验"这段话真的支持该结论"；扫描件不做 OCR；版本冲突只在"标题规范化后相同"时提示，
          改了名的版本查不出来；疑似注入靠模式匹配，改写过的注入语句挡不住。
        </p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.welcome {
  background: linear-gradient(180deg, #fff 0%, #fbfcfe 100%);
}

.quick {
  display: flex;
  gap: var(--space-3);
  margin-top: var(--space-3);
  flex-wrap: wrap;
}

.quick-link {
  padding: var(--space-2) var(--space-4);
  border: 1px solid #c9dcf5;
  border-radius: 999px;
  background: var(--c-primary-weak);
  font-size: 13px;
}

.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-4);
}

.kv {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: var(--space-2) var(--space-4);
  margin: var(--space-3) 0 0;
}

.kv dt {
  color: var(--c-text-2);
}

.kv dd {
  margin: 0;
}

.pill {
  display: inline-block;
  padding: 1px 10px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid var(--c-border);
}

.pill.llm {
  color: var(--c-met);
  border-color: #cfe6da;
  background: #f1f8f4;
}

.pill.degraded {
  color: var(--c-unknown);
  border-color: #f0dfae;
  background: #fff8e6;
}

.milestones ol {
  list-style: none;
  margin: var(--space-3) 0 0;
  padding: 0;
}

.milestones li {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: var(--space-3);
  align-items: center;
  padding: var(--space-2) 0;
  border-bottom: 1px dashed var(--c-border);
}

.milestones li:last-child {
  border-bottom: none;
}

.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 24px;
  border-radius: 6px;
  background: var(--c-primary-weak);
  border: 1px solid #c9dcf5;
  color: var(--c-primary);
  font-size: 12px;
  font-family: "Cascadia Mono", monospace;
}

.state {
  font-size: 12px;
  color: var(--c-met);
}

.small {
  font-size: 12px;
}

.tiny {
  font-size: 11px;
}

.list-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.mini-list {
  list-style: none;
  margin: var(--space-3) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.mini-list li {
  display: flex;
  gap: var(--space-2);
  align-items: flex-start;
  padding-bottom: var(--space-2);
  border-bottom: 1px dashed var(--c-border);
}

.mini-list li:last-child {
  border-bottom: none;
}

.item-title {
  font-size: 13px;
  line-height: 1.5;
}

.more {
  display: inline-block;
  margin-top: var(--space-2);
  font-size: 12px;
}

.tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid var(--c-border);
  white-space: nowrap;
  color: var(--c-text-2);
  background: var(--c-bg);
}

.next p {
  margin: var(--space-2) 0;
}

.err-line {
  color: var(--c-not-met);
  font-size: 13px;
  margin: 0;
}

/* ---------- M6：任务分布 ---------- */
.stat-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-2);
  margin-top: var(--space-3);
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-2);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-surface);
}

.stat-num {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.2;
}

.stat-num.danger {
  color: var(--c-not-met);
}

.bars {
  list-style: none;
  margin: var(--space-3) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.bars li {
  display: grid;
  grid-template-columns: 56px 1fr 28px;
  gap: var(--space-2);
  align-items: center;
  font-size: 12px;
}

.bar-label {
  color: var(--c-text-2);
}

.bar-track {
  height: 8px;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 999px;
  overflow: hidden;
  display: block;
}

.bar-fill {
  display: block;
  height: 100%;
  background: var(--c-primary-weak);
  border-radius: 999px;
}

.bar-fill.done {
  background: var(--c-met);
}

.bar-fill.todo {
  background: var(--c-primary);
}

.bar-fill.doing {
  background: var(--c-unknown);
}

.bar-fill.cancelled {
  background: #cfd6de;
}

.bar-count {
  text-align: right;
  color: var(--c-text-2);
}

@media (max-width: 900px) {
  .grid {
    grid-template-columns: 1fr;
  }

  .stat-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
