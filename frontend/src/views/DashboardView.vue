<script setup lang="ts">
/**
 * 工作台。
 *
 * M2 时这里是"骨架 + 无数据就显示暂无"。M3–M5 落地后，每个格子都必须有真实来源：
 * 计数来自 /health，待办来自 /tasks，最近提问来自 /conversations。
 * 拿不到就显示"暂无"或错误，不用 mock 数据填空。
 */
import { computed, onMounted, ref } from 'vue'
import {
  api,
  type ConversationRow,
  type Health,
  type Paged,
  type PlanRow,
  type TaskRow,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import { TASK_STATUS_LABEL, formatDate, formatTime } from '../utils/format'

const auth = useAuthStore()

const health = ref<Health | null>(null)
const todos = ref<TaskRow[]>([])
const todoTotal = ref(0)
const conversations = ref<ConversationRow[]>([])
const plans = ref<PlanRow[]>([])
const loading = ref(true)
const error = ref('')

const milestones = [
  { id: 'M1', title: '文档基线', detail: '设计说明书 · 接口清单 · 数据模型 · 评测口径', state: '已完成' },
  { id: 'M2', title: '工程可跑', detail: '应用工厂 · 登录 · 健康检查 · 建表脚本', state: '已完成' },
  { id: 'M3', title: '知识库可用', detail: '上传 · 解析 · 结构切分 · BM25 索引', state: '已完成' },
  { id: 'M4', title: '问答可信', detail: '三道闸 · 编号引用 · 拒答 · 24 条评测集', state: '已完成' },
  { id: 'M5', title: '闭环可演', detail: '规则抽取 · 三态匹配 · 计划草稿 · 任务中心', state: '已完成' },
]

onMounted(async () => {
  try {
    const [h, t, c, p] = await Promise.allSettled([
      api.get<Health>('/health'),
      api.get<Paged<TaskRow>>('/tasks?status=todo&page=1&page_size=5'),
      api.get<Paged<ConversationRow>>('/conversations?page=1&page_size=4'),
      api.get<Paged<PlanRow>>('/plans?page=1&page_size=4'),
    ])
    if (h.status === 'fulfilled') health.value = h.value
    if (t.status === 'fulfilled') {
      todos.value = t.value.items
      todoTotal.value = t.value.total
    }
    if (c.status === 'fulfilled') conversations.value = c.value.items
    if (p.status === 'fulfilled') plans.value = p.value.items
    const failure = [h, t, c, p].find((r) => r.status === 'rejected') as PromiseRejectedResult | undefined
    if (failure) {
      error.value = failure.reason instanceof Error ? failure.reason.message : '部分数据未能加载'
    }
  } finally {
    loading.value = false
  }
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

      <section class="card next">
        <h3>计划与后续</h3>
        <p class="muted small">
          已确认计划 <b>{{ confirmedPlans }}</b> 份（仅显示最近 4 条计划里的数量）。
        </p>
        <p class="muted small">
          下一阶段 <b>M6</b> 做端到端验收与规则演进：把 24 条问答评测集扩到匹配与计划，
          并补齐多轮追问的上下文引用。
        </p>
        <p class="muted tiny">
          已知限制：检索只有 BM25（未接向量模型）；引用只校验"编号在范围内"，
          不校验"这段话真的支持该结论"；扫描件不做 OCR。
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

@media (max-width: 900px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
