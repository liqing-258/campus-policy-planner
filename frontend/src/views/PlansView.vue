<script setup lang="ts">
/**
 * 计划与任务（M5）。
 *
 * 两件事：看已确认的计划、推进任务状态。状态机在后端也校验一遍，
 * 这里只是把"能点哪个按钮"提前算出来，避免用户点完才被告知非法。
 */
import { computed, onMounted, ref } from 'vue'
import { ApiError, api, type Paged, type PlanRow, type TaskRow } from '../api/client'
import {
  PLAN_STATUS_LABEL,
  TASK_NEXT,
  TASK_STATUS_LABEL,
  formatDate,
  formatTime,
} from '../utils/format'

const plans = ref<PlanRow[]>([])
const planTotal = ref(0)
const statusFilter = ref('')
const activePlan = ref('')
const tasks = ref<TaskRow[]>([])
const loading = ref(false)
const taskLoading = ref(false)
const error = ref('')

const newTaskTitle = ref('')
const creating = ref(false)

async function loadPlans() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ page: '1', page_size: '30' })
    if (statusFilter.value) params.set('status', statusFilter.value)
    const data = await api.get<Paged<PlanRow>>(`/plans?${params}`)
    plans.value = data.items
    planTotal.value = data.total
    if (!activePlan.value && data.items.length) await openPlan(data.items[0])
  } catch (err) {
    error.value = err instanceof Error ? err.message : '计划加载失败'
  } finally {
    loading.value = false
  }
}

async function openPlan(plan: PlanRow) {
  activePlan.value = plan.id
  taskLoading.value = true
  newTaskTitle.value = ''
  try {
    const data = await api.get<Paged<TaskRow>>(`/tasks?plan_id=${plan.id}&page=1&page_size=100`)
    tasks.value = data.items
  } catch (err) {
    error.value = err instanceof Error ? err.message : '任务加载失败'
  } finally {
    taskLoading.value = false
  }
}

async function moveTask(task: TaskRow, to: string) {
  error.value = ''
  try {
    await api.post<TaskRow>(`/tasks/${task.id}/status`, { status: to })
    await openPlan({ id: activePlan.value } as PlanRow)
  } catch (err) {
    error.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '状态更新失败')
  }
}

async function createTask() {
  const title = newTaskTitle.value.trim()
  if (!title || !activePlan.value) return
  creating.value = true
  error.value = ''
  try {
    await api.post<TaskRow>('/tasks', { plan_id: activePlan.value, title, priority: 'P2' })
    newTaskTitle.value = ''
    await openPlan({ id: activePlan.value } as PlanRow)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '创建失败'
  } finally {
    creating.value = false
  }
}

async function archivePlan(plan: PlanRow) {
  if (!window.confirm(`归档「${plan.title}」？归档后任务不再推进。`)) return
  try {
    await api.patch<PlanRow>(`/plans/${plan.id}`, { status: 'archived' })
    await loadPlans()
  } catch (err) {
    error.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '归档失败')
  }
}

const activePlanRow = computed(() => plans.value.find((p) => p.id === activePlan.value) ?? null)

const groups = computed(() => {
  const order: TaskRow['status'][] = ['doing', 'todo', 'deferred', 'done', 'cancelled']
  return order
    .map((status) => ({ status, items: tasks.value.filter((t) => t.status === status) }))
    .filter((g) => g.items.length > 0)
})

const progress = computed(() => {
  const total = tasks.value.length
  if (!total) return 0
  return Math.round((tasks.value.filter((t) => t.status === 'done').length / total) * 100)
})

onMounted(loadPlans)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2>计划与任务</h2>
        <p class="muted small">
          计划由匹配结果生成，确认后才落库。任务状态只能按 todo → doing → done 前进，
          「撤销完成」只能退回待办——否则"完成时间"就没有解释力了。
        </p>
      </div>
      <select v-model="statusFilter" class="select" @change="loadPlans">
        <option value="">全部计划</option>
        <option value="draft">草稿</option>
        <option value="confirmed">已确认</option>
        <option value="archived">已归档</option>
      </select>
    </header>

    <p v-if="error" class="err-line">{{ error }}</p>

    <div class="layout">
      <section class="card list-card">
        <h3>计划（{{ planTotal }}）</h3>
        <p v-if="loading" class="muted small">加载中…</p>
        <p v-else-if="!plans.length" class="muted small">
          还没有计划。到「资格匹配」页比对一次，然后生成计划草稿并确认。
        </p>
        <ul v-else class="plan-list">
          <li
            v-for="p in plans"
            :key="p.id"
            :class="{ active: activePlan === p.id }"
            @click="openPlan(p)"
          >
            <div class="plan-title">{{ p.title }}</div>
            <div class="plan-meta">
              <span :class="['tag', p.status === 'confirmed' ? 'met' : 'plain']">
                {{ PLAN_STATUS_LABEL[p.status] ?? p.status }}
              </span>
              <span class="muted tiny">{{ formatTime(p.created_at) }}</span>
            </div>
          </li>
        </ul>
      </section>

      <section class="card detail-card">
        <template v-if="activePlanRow">
          <div class="detail-head">
            <div>
              <h3>{{ activePlanRow.title }}</h3>
              <p class="muted tiny">
                {{ PLAN_STATUS_LABEL[activePlanRow.status] }} ·
                创建 {{ formatTime(activePlanRow.created_at) }}
                <template v-if="activePlanRow.confirmed_at">
                  · 确认 {{ formatTime(activePlanRow.confirmed_at) }}
                </template>
              </p>
            </div>
            <button
              v-if="activePlanRow.status !== 'archived'"
              class="btn btn-ghost"
              @click="archivePlan(activePlanRow)"
            >归档</button>
          </div>

          <div class="progress">
            <div class="bar"><span :style="{ width: `${progress}%` }" /></div>
            <span class="muted tiny">已完成 {{ progress }}%（{{ tasks.length }} 项任务）</span>
          </div>

          <p v-if="taskLoading" class="muted small">任务加载中…</p>

          <div v-for="group in groups" :key="group.status" class="task-group">
            <h4 :class="['group-head', group.status]">
              {{ TASK_STATUS_LABEL[group.status] }}（{{ group.items.length }}）
            </h4>
            <ul class="tasks">
              <li v-for="t in group.items" :key="t.id" :class="{ done: t.status === 'done' }">
                <div class="task-head">
                  <span class="tag plain">{{ t.priority }}</span>
                  <span class="task-title">{{ t.title }}</span>
                  <span v-if="t.needs_human_confirmation" class="tag unknown">需确认</span>
                </div>
                <p v-if="t.description" class="muted small">{{ t.description }}</p>
                <p class="muted tiny">
                  截止：{{ t.due_at ? formatDate(t.due_at) : '未定' }}
                  <template v-if="t.due_at && t.due_source === 'rule'">（来源：政策原文）</template>
                  <template v-if="t.required_materials"> · 材料：{{ t.required_materials }}</template>
                </p>
                <p v-if="t.note" class="note">{{ t.note }}</p>
                <div class="task-actions">
                  <button
                    v-for="next in TASK_NEXT[t.status] ?? []"
                    :key="next.to"
                    class="btn btn-ghost small-btn"
                    @click="moveTask(t, next.to)"
                  >{{ next.label }}</button>
                </div>
              </li>
            </ul>
          </div>

          <div class="add-task">
            <input
              v-model="newTaskTitle"
              class="input"
              placeholder="补充一条自己的待办"
              @keyup.enter="createTask"
            />
            <button class="btn" :disabled="creating || !newTaskTitle.trim()" @click="createTask">
              {{ creating ? '添加中…' : '添加' }}
            </button>
          </div>
        </template>
        <p v-else class="muted small">左侧选择一个计划。</p>
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

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.small {
  font-size: 12px;
}

.tiny {
  font-size: 11px;
}

.select,
.input {
  min-height: 40px;
  padding: 0 var(--space-3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: #fff;
  font-family: inherit;
  font-size: 14px;
  color: var(--c-text);
}

.layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: var(--space-4);
  align-items: start;
}

.plan-list {
  list-style: none;
  margin: var(--space-3) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.plan-list li {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  cursor: pointer;
  transition: background 0.15s ease;
}

.plan-list li:hover {
  background: var(--c-bg);
}

.plan-list li.active {
  background: var(--c-primary-weak);
  border-color: #c9dcf5;
}

.plan-title {
  font-size: 13px;
  font-weight: 500;
  margin-bottom: var(--space-1);
}

.plan-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-2);
}

.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-3);
}

.progress {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin: var(--space-3) 0;
}

.bar {
  flex: 1;
  height: 6px;
  background: var(--c-border);
  border-radius: 999px;
  overflow: hidden;
}

.bar span {
  display: block;
  height: 100%;
  background: var(--c-met);
  transition: width 0.3s ease;
}

.task-group {
  margin-top: var(--space-4);
}

.group-head {
  font-size: 13px;
  margin: 0 0 var(--space-2);
  color: var(--c-text-2);
  font-weight: 600;
}

.group-head.doing {
  color: var(--c-primary);
}

.group-head.done {
  color: var(--c-met);
}

.tasks {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.tasks li {
  padding: var(--space-3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
}

.tasks li.done {
  opacity: 0.72;
}

.task-head {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  flex-wrap: wrap;
}

.task-title {
  font-size: 13px;
  font-weight: 500;
}

.tasks li.done .task-title {
  text-decoration: line-through;
}

.task-actions {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.note {
  margin: var(--space-1) 0 0;
  font-size: 11px;
  color: var(--c-unknown);
  background: #fffdf7;
  border-left: 2px solid #f0dfae;
  padding: 2px var(--space-2);
}

.small-btn {
  min-height: 30px;
  padding: 0 var(--space-3);
  font-size: 12px;
}

.add-task {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-4);
  padding-top: var(--space-4);
  border-top: 1px dashed var(--c-border);
}

.add-task .input {
  flex: 1;
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

.tag.unknown {
  color: var(--c-unknown);
  border-color: #f0dfae;
  background: #fff8e6;
}

.tag.plain {
  color: var(--c-text-2);
  background: var(--c-bg);
}

.err-line {
  color: var(--c-not-met);
  font-size: 13px;
  margin: 0;
}

@media (max-width: 860px) {
  .layout {
    grid-template-columns: 1fr;
  }
}
</style>
