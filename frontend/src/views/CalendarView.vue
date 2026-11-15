<script setup lang="ts">
/**
 * 我的校园日历（R14）。
 *
 * ## 这一页只做"摆位置"，不自己判断日期
 *
 * 每一条日程的 `date`、`kind`、`category`、`date_source` 都由后端算好。
 * 前端不做三件事：
 *
 * 1. **不给没有日期的条目猜一个日期**。政策只写「9 月 10 日」时，后端返回
 *    `date: null` + `undated_reason: 'year_missing'`，页面把它放进右侧
 *    「待确认日期」栏，格子里一个点都不画。用今年补上是最容易写、也最危险的一行代码。
 * 2. **不自己数**"本月几项""逾期几项"。`counts` 由接口给，口径只有一处。
 * 3. **不在前端保存修改结果**。改期走 `PATCH /tasks/{id}`，完成走
 *    `POST /tasks/{id}/status`，改完重新拉一次范围——日历没有副本，
 *    "同步到任务中心"因此是免费的。
 *
 * ## 三个视图的分工
 *
 * - 月：36/42 格，每格最多 3 条 + "+N"，看"这个月哪几天有事"；
 * - 周：7 列，每列能排下当天全部条目，看"这周怎么安排"；
 * - 列表：按日期分组平铺，看"接下来依次是什么"。
 *
 * 三个视图共用同一次请求（同一个 from/to），切换视图不重新拉数据，
 * 除非范围真的变了（月 ↔ 周）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ApiError,
  api,
  type CalendarCategory,
  type CalendarData,
  type CalendarItem,
} from '../api/client'
import { TASK_STATUS_LABEL, formatTime } from '../utils/format'

type ViewMode = 'month' | 'week' | 'list'

const route = useRoute()
const router = useRouter()

// ---------------------------------------------------------------- 日期工具
/** 一律用 `YYYY-MM-DD` 字符串当键：`Date` 对象比较会被时区与时分秒拖下水 */
const pad = (n: number) => String(n).padStart(2, '0')

function todayKey(): string {
  const now = new Date()
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

function parseKey(key: string): Date {
  const [y, m, d] = key.split('-').map(Number)
  return new Date(y, m - 1, d)
}

function keyOf(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function addDays(key: string, days: number): string {
  const date = parseKey(key)
  date.setDate(date.getDate() + days)
  return keyOf(date)
}

function startOfMonth(key: string): string {
  return `${key.slice(0, 7)}-01`
}

function endOfMonth(key: string): string {
  const date = parseKey(key)
  return keyOf(new Date(date.getFullYear(), date.getMonth() + 1, 0))
}

function addMonths(key: string, months: number): string {
  const date = parseKey(key)
  return keyOf(new Date(date.getFullYear(), date.getMonth() + months, 1))
}

/** 周一为一周之首：校园里"这周"几乎都指周一到周日 */
function startOfWeek(key: string): string {
  const date = parseKey(key)
  const offset = (date.getDay() + 6) % 7
  date.setDate(date.getDate() - offset)
  return keyOf(date)
}

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

// ---------------------------------------------------------------- 状态
const view = ref<ViewMode>(['month', 'week', 'list'].includes(String(route.query.view))
  ? (route.query.view as ViewMode)
  : 'month')
const cursor = ref(startOfMonth(todayKey()))
const selectedDay = ref(todayKey())
const selected = ref<CalendarItem | null>(null)
const data = ref<CalendarData | null>(null)
const loading = ref(false)
const error = ref('')
const saving = ref(false)
const categoryFilter = ref<CalendarCategory | ''>('')
const showAllUndated = ref(false)

const dialog = ref({
  open: false,
  mode: 'create' as 'create' | 'edit',
  id: '',
  title: '',
  date: todayKey(),
  time: '',
  note: '',
  category: 'personal' as CalendarCategory,
})

const dateDraft = ref('')

const today = computed(() => data.value?.today ?? todayKey())

// ---------------------------------------------------------------- 取数
const currentRange = computed<[string, string]>(() => {
  if (view.value === 'week') {
    const start = startOfWeek(selectedDay.value)
    return [start, addDays(start, 6)]
  }
  return [startOfMonth(cursor.value), endOfMonth(cursor.value)]
})

async function load() {
  const [from, to] = currentRange.value
  loading.value = true
  error.value = ''
  try {
    data.value = await api.get<CalendarData>(`/calendar?from=${from}&to=${to}`)
    // 服务器说今天是哪天，就以哪天为准：浏览器时区与服务器不一致时，
    // 用本地日期高亮会圈错格子
    if (!data.value.items.length && !data.value.undated.length) {
      selected.value = null
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '日历加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await load()
  if (data.value) selectedDay.value = data.value.today
  // 首屏落在本月：如果 URL 带了 month=2026-09，也认
  const month = String(route.query.month ?? '')
  if (/^\d{4}-\d{2}$/.test(month)) {
    cursor.value = `${month}-01`
    await load()
  }
})

watch(currentRange, load)

// ---------------------------------------------------------------- 派生
const activeCategory = computed(() => (categoryFilter.value
  ? data.value?.categories.find((row) => row.key === categoryFilter.value) ?? null
  : null))

function passFilter(item: CalendarItem): boolean {
  if (!categoryFilter.value) return true
  return item.category === categoryFilter.value
}

const filteredItems = computed(() => (data.value?.items ?? []).filter(passFilter))
const filteredUndated = computed(() => (data.value?.undated ?? []).filter(passFilter))

const itemsByDay = computed(() => {
  const map: Record<string, CalendarItem[]> = {}
  for (const item of filteredItems.value) {
    if (!item.date) continue
    if (!map[item.date]) map[item.date] = []
    map[item.date].push(item)
  }
  return map
})

/** 月视图固定 6 行：行数随月份跳动会让翻月时整页抖一下 */
const monthCells = computed(() => {
  const gridStart = startOfWeek(startOfMonth(cursor.value))
  const monthPrefix = cursor.value.slice(0, 7)
  const cells = []
  for (let index = 0; index < 42; index += 1) {
    const key = addDays(gridStart, index)
    cells.push({
      key,
      day: Number(key.slice(8, 10)),
      inMonth: key.slice(0, 7) === monthPrefix,
      isToday: key === today.value,
      items: itemsByDay.value[key] ?? [],
    })
  }
  return cells
})

const weekCells = computed(() => {
  const start = startOfWeek(selectedDay.value)
  return Array.from({ length: 7 }, (_, index) => {
    const key = addDays(start, index)
    return {
      key,
      day: Number(key.slice(8, 10)),
      month: Number(key.slice(5, 7)),
      isToday: key === today.value,
      items: itemsByDay.value[key] ?? [],
    }
  })
})

const listGroups = computed(() => {
  const groups: { key: string; items: CalendarItem[] }[] = []
  for (const item of filteredItems.value) {
    if (!item.date) continue
    const last = groups[groups.length - 1]
    if (last && last.key === item.date) last.items.push(item)
    else groups.push({ key: item.date, items: [item] })
  }
  return groups
})

const dayItems = computed(() => itemsByDay.value[selectedDay.value] ?? [])

/** 右侧栏在没有选中详情时显示"选中日"；选中了日程就显示详情 */
const sideTitle = computed(() => {
  if (selected.value) return '日程详情'
  return selectedDay.value === today.value ? '今天的日程' : `${selectedDay.value} 的日程`
})

const undatedByReason = computed(() => {
  const groups: { key: string; label: string; hint: string; items: CalendarItem[] }[] = []
  const meta: Record<string, { label: string; hint: string }> = {
    year_missing: {
      label: '年份待确认',
      hint: '政策原文给了月日、没给年份。确认年份后才会出现在日历上。',
    },
    no_date_in_policy: {
      label: '原文未给日期',
      hint: '政策里没有可用的时间节点，需要向学院确认后再填。',
    },
    not_set: {
      label: '未设置日期',
      hint: '这类事项本来就不要求截止日，例如"办理流程"类待办。',
    },
  }
  for (const key of ['year_missing', 'no_date_in_policy', 'not_set']) {
    const items = filteredUndated.value.filter((item) => item.undated_reason === key)
    if (items.length) {
      groups.push({ key, label: meta[key].label, hint: meta[key].hint, items })
    }
  }
  return groups
})

const undatedShown = computed(() => {
  if (showAllUndated.value) return undatedByReason.value
  // 默认只展开「年份待确认」+「原文未给日期」的摘要，避免右侧被 25 条挤爆
  return undatedByReason.value.filter((group) => group.key !== 'not_set')
})

const noticeLine = computed(() => {
  if (!data.value) return ''
  const { total, undated } = data.value.counts
  if (total === 0 && undated > 0) {
    return `本月还没有落到具体日期的日程；另有 ${undated} 条政策节点缺日期，确认后才会出现在日历上。`
  }
  return data.value.notice
})

const monthLabel = computed(() => {
  const date = parseKey(cursor.value)
  return `${date.getFullYear()} 年 ${date.getMonth() + 1} 月`
})

const weekLabel = computed(() => {
  const start = startOfWeek(selectedDay.value)
  const end = addDays(start, 6)
  return `${start} — ${end.slice(5)}`
})

// ---------------------------------------------------------------- 操作
function goToday() {
  const key = today.value
  cursor.value = startOfMonth(key)
  selectedDay.value = key
  selected.value = null
  if (currentRange.value[0] === startOfMonth(key)) void load()
}

function shiftMonth(step: number) {
  cursor.value = addMonths(cursor.value, step)
  selected.value = null
}

function shiftWeek(step: number) {
  selectedDay.value = addDays(selectedDay.value, step * 7)
  selected.value = null
}

function pickDay(key: string) {
  selectedDay.value = key
  selected.value = null
  if (view.value === 'month' && key.slice(0, 7) !== cursor.value.slice(0, 7)) {
    // 点到上/下月的补位格：整月跟着翻过去，否则"点了没反应"
    cursor.value = startOfMonth(key)
  }
}

function setView(mode: ViewMode) {
  view.value = mode
  selected.value = null
}

function openDetail(item: CalendarItem) {
  selected.value = item
  dateDraft.value = item.date ?? ''
}

function closeDetail() {
  selected.value = null
}

function flash(message: string) {
  error.value = message
  window.setTimeout(() => {
    if (error.value === message) error.value = ''
  }, 4000)
}

function toMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return `${err.code} · ${err.message}`
  return err instanceof Error ? err.message : fallback
}

/**
 * 保存详情里的日期。任务与个人日程走两套接口，**必须都覆盖**：
 * 第一版只写了 task 分支，个人日程的"保存"按钮点了没反应——
 * 模板里 `date-editor` 是对两类都渲染的，函数却只认一种，这种不一致
 * 接口测试看不见，只有真点一下才发现。
 */
async function saveDate() {
  const item = selected.value
  if (!item || !dateDraft.value) return
  saving.value = true
  try {
    if (item.kind === 'task') {
      await api.patch(`/tasks/${item.ref_id}`, { due_at: `${dateDraft.value}T00:00:00Z` })
    } else if (item.kind === 'event') {
      await api.patch(`/calendar/events/${item.ref_id}`, { date: dateDraft.value })
    } else {
      return
    }
    const keep = item.id
    await load()
    selected.value = data.value?.items.find((row) => row.id === keep)
      ?? data.value?.undated.find((row) => row.id === keep)
      ?? null
    if (!selected.value) {
      flash('日期已保存，这条日程已按新日期归入对应月份。')
    }
  } catch (err) {
    flash(toMessage(err, '日期保存失败'))
  } finally {
    saving.value = false
  }
}

async function toggleDone() {
  const item = selected.value
  if (!item || item.kind !== 'task') return
  saving.value = true
  try {
    await api.post(`/tasks/${item.ref_id}/status`, { status: item.done ? 'todo' : 'done' })
    const keep = item.id
    await load()
    selected.value = data.value?.items.find((row) => row.id === keep)
      ?? data.value?.undated.find((row) => row.id === keep)
      ?? null
  } catch (err) {
    flash(toMessage(err, '状态更新失败'))
  } finally {
    saving.value = false
  }
}

function goPlan(planId: string | null) {
  if (!planId) return
  void router.push({ path: '/plans', query: { plan: planId } })
}

function goDocument(documentId: string | null) {
  if (!documentId) return
  void router.push({ path: '/library', query: { doc: documentId } })
}

function openCreate() {
  // 默认日期 = 当前看得见的那一天。翻到 9 月时 `selectedDay` 还停在"今天"（11 月），
  // 直接拿它会让用户在 9 月的格子里新建出一条 11 月的日程——保存后原地消失。
  const [from, to] = currentRange.value
  const seed = selectedDay.value >= from && selectedDay.value <= to
    ? selectedDay.value
    : from
  dialog.value = {
    open: true,
    mode: 'create',
    id: '',
    title: '',
    date: seed,
    time: '',
    note: '',
    category: 'personal',
  }
}

function openEdit(item: CalendarItem) {
  if (!item.editable || item.kind !== 'event') return
  dialog.value = {
    open: true,
    mode: 'edit',
    id: item.ref_id,
    title: item.title,
    date: item.date ?? today.value,
    time: item.time ?? '',
    note: item.note ?? '',
    category: item.category,
  }
}

async function submitDialog() {
  const form = dialog.value
  const title = form.title.trim()
  if (!title) return
  saving.value = true
  try {
    const payload = {
      title,
      date: form.date,
      time: form.time || null,
      note: form.note.trim() || null,
      category: form.category,
    }
    if (form.mode === 'create') {
      await api.post('/calendar/events', payload)
    } else {
      await api.patch(`/calendar/events/${form.id}`, payload)
    }
    dialog.value.open = false
    selected.value = null
    await load()
  } catch (err) {
    flash(toMessage(err, '日程保存失败'))
  } finally {
    saving.value = false
  }
}

async function deleteEvent() {
  const item = selected.value
  if (!item || !item.deletable) return
  if (!window.confirm(`删除日程「${item.title}」？此操作不可撤销。`)) return
  saving.value = true
  try {
    await api.remove(`/calendar/events/${item.ref_id}`)
    selected.value = null
    await load()
  } catch (err) {
    flash(toMessage(err, '删除失败'))
  } finally {
    saving.value = false
  }
}

const CATEGORY_OPTIONS: { key: CalendarCategory; label: string }[] = [
  { key: 'personal', label: '个人事务' },
  { key: 'material', label: '材料提交' },
  { key: 'scholarship', label: '奖学金截止' },
  { key: 'award', label: '评奖评优' },
  { key: 'exam', label: '考试报名' },
  { key: 'library', label: '图书归还' },
  { key: 'other', label: '其他事务' },
]

function kindLabel(item: CalendarItem): string {
  if (item.kind === 'event') return '个人日程'
  if (item.kind === 'policy') return '政策节点'
  return TASK_STATUS_LABEL[item.status] ?? item.status
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2>校园日历</h2>
        <p class="muted small">
          待办与个人日程在同一张日历上；政策里写着时间但还没生成待办的，单独标成「政策节点」。
          原文只给了月日、没给年份的，一律进右侧「待确认日期」，不替你补年份。
        </p>
      </div>
      <div class="tail">
        <button class="btn" @click="openCreate">+ 新建日程</button>
      </div>
    </header>

    <p v-if="error" class="err-line">{{ error }}</p>

    <div class="toolbar card">
      <div class="nav-group">
        <button class="btn btn-ghost icon-btn" aria-label="上一段" @click="view === 'week' ? shiftWeek(-1) : shiftMonth(-1)">‹</button>
        <button class="btn btn-ghost" @click="goToday">今天</button>
        <button class="btn btn-ghost icon-btn" aria-label="下一段" @click="view === 'week' ? shiftWeek(1) : shiftMonth(1)">›</button>
        <strong class="period">{{ view === 'week' ? weekLabel : monthLabel }}</strong>
      </div>

      <div class="view-switch" role="tablist" aria-label="视图">
        <button
          v-for="mode in (['month', 'week', 'list'] as ViewMode[])"
          :key="mode"
          role="tab"
          :aria-selected="view === mode"
          :class="['seg', { on: view === mode }]"
          @click="setView(mode)"
        >{{ mode === 'month' ? '月视图' : mode === 'week' ? '周视图' : '列表' }}</button>
      </div>

      <div class="summary mono">
        本{{ view === 'week' ? '周' : '月' }} {{ data?.counts.total ?? 0 }} 项 ·
        完成 {{ data?.counts.done ?? 0 }} ·
        <span :class="{ danger: (data?.counts.overdue ?? 0) > 0 }">逾期 {{ data?.counts.overdue ?? 0 }}</span>
        · 待确认 {{ data?.counts.undated ?? 0 }}
      </div>
    </div>

    <div class="legend">
      <button
        :class="['chip', { on: categoryFilter === '' }]"
        @click="categoryFilter = ''"
      >全部</button>
      <button
        v-for="row in data?.categories ?? []"
        :key="row.key"
        :class="['chip', `cat-${row.key}`, { on: categoryFilter === row.key }]"
        :title="`只看${row.label}`"
        @click="categoryFilter = categoryFilter === row.key ? '' : row.key"
      >
        <span class="dot" />
        {{ row.label }}
        <span class="chip-count mono">{{ row.count }}</span>
        <!-- 日历上没有、但待确认里有：显示成 +N。只显示 0 会让人以为
             "这类政策跟日历无关"，实际只是缺一个年份 -->
        <span
          v-if="data?.counts.undated_by_category[row.key]"
          class="chip-undated mono"
          :title="`另有 ${data.counts.undated_by_category[row.key]} 条待确认日期，补上日期后进入日历`"
        >+{{ data.counts.undated_by_category[row.key] }}</span>
      </button>
    </div>

    <p v-if="noticeLine" class="notice">{{ noticeLine }}</p>

    <div class="layout">
      <!-- ============ 左：日历主体 ============ -->
      <section class="calendar card" :class="view">
        <p v-if="loading && !data" class="empty">读取中…</p>

        <!-- 月视图 -->
        <template v-else-if="view === 'month'">
          <div class="weekhead">
            <span v-for="(name, index) in WEEKDAYS" :key="name" :class="{ weekend: index >= 5 }">
              周{{ name }}
            </span>
          </div>
          <div class="month-grid">
            <div
              v-for="cell in monthCells"
              :key="cell.key"
              :class="['cell', {
                dim: !cell.inMonth,
                today: cell.isToday,
                picked: cell.key === selectedDay,
                has: cell.items.length,
              }]"
              role="button"
              tabindex="0"
              :aria-label="`${cell.key}，${cell.items.length} 项`"
              @click="pickDay(cell.key)"
              @keydown.enter="pickDay(cell.key)"
            >
              <div class="cell-head">
                <span class="daynum">{{ cell.day }}</span>
                <span v-if="cell.isToday" class="today-tag">今天</span>
                <span v-else-if="cell.items.length > 3" class="more mono">+{{ cell.items.length - 3 }}</span>
              </div>
              <div class="cell-body">
                <div
                  v-for="item in cell.items.slice(0, 3)"
                  :key="item.id"
                  :class="['pill', `cat-${item.category}`, {
                    done: item.done,
                    overdue: item.overdue,
                    policy: item.kind === 'policy',
                  }]"
                  :title="item.title"
                  @click.stop="openDetail(item)"
                >
                  <span v-if="item.time" class="pill-time mono">{{ item.time }}</span>
                  <span class="pill-text">{{ item.title }}</span>
                </div>
              </div>
            </div>
          </div>
        </template>

        <!-- 周视图 -->
        <template v-else-if="view === 'week'">
          <div class="week-grid">
            <div
              v-for="(cell, index) in weekCells"
              :key="cell.key"
              :class="['wcol', { today: cell.isToday, picked: cell.key === selectedDay }]"
            >
              <div class="wcol-head" @click="pickDay(cell.key)">
                <span :class="{ weekend: index >= 5 }">周{{ WEEKDAYS[index] }}</span>
                <strong class="mono">{{ cell.month }}/{{ cell.day }}</strong>
              </div>
              <div class="wcol-body">
                <div
                  v-for="item in cell.items"
                  :key="item.id"
                  :class="['wcard', `cat-${item.category}`, {
                    done: item.done,
                    overdue: item.overdue,
                    policy: item.kind === 'policy',
                  }]"
                  role="button"
                  tabindex="0"
                  @click="openDetail(item)"
                  @keydown.enter="openDetail(item)"
                >
                  <div class="wcard-top">
                    <span class="wtag">{{ item.category_label }}</span>
                    <span v-if="item.time" class="mono tiny">{{ item.time }}</span>
                  </div>
                  <div class="wcard-title">{{ item.title }}</div>
                  <div class="tiny muted">
                    {{ kindLabel(item) }}
                    <template v-if="item.date_source_label && item.kind !== 'event'">
                      · {{ item.date_source_label }}
                    </template>
                  </div>
                </div>
                <p v-if="!cell.items.length" class="wempty">—</p>
              </div>
            </div>
          </div>
        </template>

        <!-- 列表视图 -->
        <template v-else>
          <div v-if="!listGroups.length" class="empty list-empty">
            <strong>这个范围内没有已确认日期的日程</strong>
            <p class="muted small">
              右侧「待确认日期」里的条目补上日期后会出现在这里。
            </p>
          </div>
          <div v-for="group in listGroups" :key="group.key" class="lgroup">
            <div class="ldate">
              <strong>{{ group.key }}</strong>
              <span class="muted tiny">
                {{ ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][parseKey(group.key).getDay()] }}
                <template v-if="group.key === today"> · 今天</template>
              </span>
            </div>
            <div
              v-for="item in group.items"
              :key="item.id"
              :class="['lrow', `cat-${item.category}`, { done: item.done, overdue: item.overdue }]"
              role="button"
              tabindex="0"
              @click="openDetail(item)"
              @keydown.enter="openDetail(item)"
            >
              <span class="ltime mono">{{ item.time ?? '全天' }}</span>
              <span class="ltag">{{ item.category_label }}</span>
              <span class="ltitle">{{ item.title }}</span>
              <span class="lmeta muted tiny">
                {{ kindLabel(item) }}
                <template v-if="item.overdue"> · 已逾期</template>
              </span>
            </div>
          </div>
        </template>
      </section>

      <!-- ============ 右：日程栏 ============ -->
      <aside class="side">
        <section class="card side-card">
          <div class="side-head">
            <h3>{{ sideTitle }}</h3>
            <button v-if="selected" class="btn btn-ghost tiny-btn" @click="closeDetail">返回</button>
          </div>

          <!-- 详情 -->
          <template v-if="selected">
            <div :class="['detail-cat', `cat-${selected.category}`]">
              <span class="dot" />{{ selected.category_label }}
              <span class="sep">·</span>
              <span>{{ kindLabel(selected) }}</span>
            </div>

            <h4 class="detail-title">{{ selected.title }}</h4>

            <dl class="kv">
              <dt>日期</dt>
              <dd>
                <template v-if="selected.date">
                  <span class="mono">{{ selected.date }}</span>
                  <template v-if="selected.time"> <span class="mono">{{ selected.time }}</span></template>
                  <span class="tag plain date-src">{{ selected.date_source_label }}</span>
                </template>
                <template v-else>
                  <span class="tag unknown">{{ selected.undated_reason_label }}</span>
                  <span v-if="selected.raw_text" class="mono raw">原文：{{ selected.raw_text }}</span>
                </template>
              </dd>

              <template v-if="selected.document_title">
                <dt>来源文件</dt>
                <dd>
                  <button class="link" @click="goDocument(selected.document_id)">
                    {{ selected.document_title }}
                  </button>
                </dd>
              </template>

              <template v-if="selected.plan_title">
                <dt>所属计划</dt>
                <dd>
                  <button class="link" @click="goPlan(selected.plan_id)">{{ selected.plan_title }}</button>
                </dd>
              </template>

              <template v-if="selected.description">
                <dt>说明</dt>
                <dd class="small">{{ selected.description }}</dd>
              </template>

              <template v-if="selected.required_materials">
                <dt>所需材料</dt>
                <dd class="small">{{ selected.required_materials }}</dd>
              </template>

              <template v-if="selected.completed_at">
                <dt>完成于</dt>
                <dd class="small mono">{{ formatTime(selected.completed_at) }}</dd>
              </template>
            </dl>

            <p v-if="selected.note" class="note">{{ selected.note }}</p>

            <!-- 补年份 / 改期：任务与个人日程都走这里，只是接口不同 -->
            <div v-if="selected.kind !== 'policy'" class="date-editor">
              <label :for="`date-${selected.id}`">
                {{ selected.date ? '修改日期' : '补上完整日期' }}
              </label>
              <div class="row">
                <input
                  :id="`date-${selected.id}`"
                  v-model="dateDraft"
                  class="input"
                  type="date"
                />
                <button
                  class="btn btn-sm"
                  :disabled="saving || !dateDraft"
                  @click="saveDate"
                >保存</button>
              </div>
              <p v-if="!selected.date && selected.undated_reason === 'year_missing'" class="tiny muted">
                原文只写了{{ selected.raw_text ?? '月日' }}，年份需要你来确认。保存后这条会落到日历上。
              </p>
            </div>

            <div class="detail-actions">
              <button
                v-if="selected.kind === 'task'"
                class="btn btn-sm"
                :disabled="saving"
                @click="toggleDone"
              >{{ selected.done ? '撤销完成' : '标记完成' }}</button>
              <button
                v-if="selected.plan_id"
                class="btn btn-sm btn-ghost"
                @click="goPlan(selected.plan_id)"
              >打开计划</button>
              <button
                v-if="selected.kind === 'event'"
                class="btn btn-sm btn-ghost"
                @click="openEdit(selected)"
              >编辑日程</button>
              <button
                v-if="selected.deletable"
                class="btn btn-sm btn-ghost danger-btn"
                :disabled="saving"
                @click="deleteEvent"
              >删除</button>
            </div>
          </template>

          <!-- 选中日的日程 -->
          <template v-else>
            <p class="muted small picked-day">{{ selectedDay }}</p>
            <div v-if="dayItems.length" class="day-list">
              <div
                v-for="item in dayItems"
                :key="item.id"
                :class="['drow', `cat-${item.category}`, { done: item.done, overdue: item.overdue }]"
                role="button"
                tabindex="0"
                @click="openDetail(item)"
                @keydown.enter="openDetail(item)"
              >
                <span class="dtime mono">{{ item.time ?? '全天' }}</span>
                <div class="grow">
                  <div class="dtitle">{{ item.title }}</div>
                  <div class="muted tiny">
                    {{ item.category_label }} · {{ kindLabel(item) }}
                    <template v-if="item.date_source_label && item.kind !== 'event'">
                      · {{ item.date_source_label }}
                    </template>
                  </div>
                </div>
                <span v-if="item.overdue" class="flag danger">逾期</span>
                <span v-else-if="item.done" class="flag ok">已完成</span>
              </div>
            </div>
            <p v-else class="empty small">
              这一天没有日程。可以点「新建日程」自己加一条。
            </p>
          </template>
        </section>

        <!-- 待确认日期 -->
        <section class="card side-card">
          <div class="side-head">
            <h3>待确认日期</h3>
            <span class="muted tiny">{{ filteredUndated.length }} 项</span>
          </div>
          <p class="muted tiny">
            政策原文里的时间节点，缺日期或缺年份。系统不会替你补，确认后才会落到日历上。
          </p>

          <div v-for="group in undatedShown" :key="group.key" class="ugroup">
            <div class="uhead">
              <span :class="['utag', group.key]">{{ group.label }}</span>
              <span class="muted tiny">{{ group.items.length }}</span>
            </div>
            <p class="muted tiny">{{ group.hint }}</p>
            <div
              v-for="item in group.items.slice(0, showAllUndated ? 100 : 6)"
              :key="item.id"
              :class="['urow', `cat-${item.category}`]"
              role="button"
              tabindex="0"
              @click="openDetail(item)"
              @keydown.enter="openDetail(item)"
            >
              <div class="grow">
                <div class="utitle">{{ item.title }}</div>
                <div class="muted tiny">
                  <span v-if="item.raw_text" class="mono">{{ item.raw_text }}</span>
                  <span v-else>无日期</span>
                  · {{ item.category_label }}
                </div>
              </div>
            </div>
            <button
              v-if="!showAllUndated && group.items.length > 6"
              class="link more-link"
              @click="showAllUndated = true"
            >展开全部 {{ group.items.length }} 项</button>
          </div>

          <p v-if="!filteredUndated.length" class="empty small">
            没有待确认的条目。
          </p>
        </section>
      </aside>
    </div>

    <!-- ============ 新建 / 编辑弹层 ============ -->
    <div v-if="dialog.open" class="overlay" @click.self="dialog.open = false">
      <div class="dialog card" role="dialog" aria-modal="true">
        <h3>{{ dialog.mode === 'create' ? '新建日程' : '编辑日程' }}</h3>
        <p class="muted tiny">
          个人日程只写在 `calendar_events` 里，不会混进任何一份政策计划。
        </p>

        <label class="field">
          <span>标题</span>
          <input v-model="dialog.title" class="input" maxlength="100" placeholder="例如：去图书馆还书" />
        </label>

        <div class="field-row">
          <label class="field">
            <span>日期</span>
            <input v-model="dialog.date" class="input" type="date" />
          </label>
          <label class="field">
            <span>时间（可空）</span>
            <input v-model="dialog.time" class="input" type="time" />
          </label>
        </div>

        <label class="field">
          <span>类型</span>
          <select v-model="dialog.category" class="input">
            <option v-for="opt in CATEGORY_OPTIONS" :key="opt.key" :value="opt.key">{{ opt.label }}</option>
          </select>
        </label>

        <label class="field">
          <span>备注（可空）</span>
          <textarea v-model="dialog.note" class="input" rows="2" maxlength="500" />
        </label>

        <div class="dialog-actions">
          <button class="btn btn-ghost" @click="dialog.open = false">取消</button>
          <button class="btn" :disabled="saving || !dialog.title.trim()" @click="submitDialog">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.page-head h2 {
  margin: 0 0 2px;
}

.tail {
  display: flex;
  gap: var(--space-2);
}

/* 分类色：一处定义，四类元素（chip / pill / 边框 / 标签）共用 */
.cat-scholarship { --cat: var(--c-primary); --cat-bg: var(--c-primary-weak); --cat-border: var(--c-primary-border); }
.cat-award { --cat: var(--c-purple); --cat-bg: rgba(107, 79, 187, 0.1); --cat-border: rgba(107, 79, 187, 0.3); }
.cat-exam { --cat: var(--c-info); --cat-bg: rgba(31, 110, 168, 0.1); --cat-border: rgba(31, 110, 168, 0.3); }
.cat-material { --cat: var(--c-accent); --cat-bg: var(--c-accent-weak); --cat-border: rgba(156, 100, 16, 0.32); }
.cat-library { --cat: var(--c-met); --cat-bg: var(--c-met-bg); --cat-border: var(--c-met-border); }
.cat-personal { --cat: var(--c-cal-personal); --cat-bg: var(--c-cal-personal-bg); --cat-border: var(--c-cal-personal-border); }
.cat-other { --cat: var(--c-dim); --cat-bg: var(--c-surface-2); --cat-border: var(--c-border-strong); }

/* ---------- 工具条 ---------- */
.toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
  padding: var(--space-2) var(--space-3);
}

.nav-group {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

.icon-btn {
  min-width: 32px;
  padding: 0 var(--space-2);
  font-size: 16px;
  line-height: 1;
}

.period {
  font-size: 14.5px;
  margin-left: var(--space-2);
  white-space: nowrap;
}

.view-switch {
  display: flex;
  padding: 2px;
  border-radius: var(--radius-control);
  background: var(--c-elev-1);
  border: 1px solid var(--c-border);
}

.seg {
  border: none;
  background: transparent;
  color: var(--c-text-2);
  font-family: inherit;
  font-size: 12.5px;
  padding: 4px 10px;
  border-radius: 7px;
  cursor: pointer;
}

.seg.on {
  background: var(--c-surface);
  color: var(--c-primary);
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(18, 48, 43, 0.08);
}

.summary {
  margin-left: auto;
  font-size: 12px;
  color: var(--c-text-2);
}

.summary .danger {
  color: var(--c-not-met);
  font-weight: 600;
}

/* ---------- 图例 ---------- */
.legend {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--c-border);
  background: var(--c-surface);
  color: var(--c-text-2);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}

.chip .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--cat, var(--c-dim));
}

.chip.on {
  border-color: var(--cat, var(--c-primary-border));
  background: var(--cat-bg, var(--c-primary-weak));
  color: var(--cat, var(--c-primary));
  font-weight: 600;
}

.chip-count {
  font-size: 11px;
  opacity: 0.72;
}

.chip-undated {
  font-size: 10.5px;
  color: var(--c-unknown);
  border-left: 1px solid var(--c-border);
  padding-left: 6px;
}

.notice {
  margin: 0;
  font-size: 12px;
  color: var(--c-unknown);
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
  border-left-width: 3px;
  border-radius: var(--radius-control);
  padding: var(--space-2) var(--space-3);
}

/* ---------- 布局 ---------- */
.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 330px;
  gap: var(--space-4);
  align-items: start;
}

.calendar {
  padding: var(--space-3);
  min-height: 520px;
}

.side {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  position: sticky;
  top: 72px;
  max-height: calc(100vh - 88px);
  overflow: auto;
}

/* ---------- 月视图 ---------- */
.weekhead {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 4px;
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--c-border);
  margin-bottom: 4px;
}

.weekhead span {
  text-align: center;
  font-size: 11.5px;
  color: var(--c-text-2);
  padding: 2px 0;
}

.weekhead span.weekend {
  color: var(--c-dim);
}

.month-grid {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  grid-auto-rows: minmax(92px, 1fr);
  gap: 4px;
}

.cell {
  border: 1px solid var(--c-border);
  border-radius: 8px;
  padding: 4px 5px;
  min-width: 0;
  cursor: pointer;
  background: var(--c-surface);
  transition: background 0.14s ease, border-color 0.14s ease;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cell:hover {
  background: var(--c-surface-2);
}

.cell.dim {
  background: var(--c-bg);
  border-style: dashed;
}

.cell.dim .daynum {
  color: var(--c-dim);
  opacity: 0.7;
}

.cell.picked {
  border-color: var(--c-primary-border);
  box-shadow: inset 0 0 0 1px var(--c-primary-border);
}

.cell.today {
  background: var(--c-primary-weak);
  border-color: var(--c-primary-border);
}

.cell-head {
  display: flex;
  align-items: center;
  gap: 4px;
  min-height: 16px;
}

.daynum {
  font-size: 12px;
  font-weight: 600;
  color: var(--c-text);
  font-variant-numeric: tabular-nums;
}

.today-tag {
  font-size: 10px;
  color: var(--c-primary);
  background: var(--c-surface);
  border: 1px solid var(--c-primary-border);
  border-radius: 999px;
  padding: 0 5px;
  line-height: 14px;
}

.more {
  margin-left: auto;
  font-size: 10px;
  color: var(--c-text-2);
}

.cell-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow: hidden;
}

.pill {
  display: flex;
  align-items: baseline;
  gap: 3px;
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--cat-bg);
  border-left: 2px solid var(--cat);
  font-size: 10.5px;
  line-height: 1.35;
  color: var(--c-text);
  overflow: hidden;
}

.pill-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pill-time {
  color: var(--cat);
  font-size: 10px;
  flex: none;
}

.pill.done {
  opacity: 0.62;
  text-decoration: line-through;
}

.pill.overdue {
  /* 逾期不换色（换色会盖掉分类），改在右侧加一道红边 */
  box-shadow: inset -2px 0 0 var(--c-not-met);
}

.pill.policy {
  background: transparent;
  border-left-style: dashed;
  border-top: 1px dashed var(--cat-border);
  border-right: 1px dashed var(--cat-border);
  border-bottom: 1px dashed var(--cat-border);
}

/* ---------- 周视图 ---------- */
.week-grid {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 4px;
}

.wcol {
  border: 1px solid var(--c-border);
  border-radius: 8px;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.wcol.today {
  background: var(--c-primary-weak);
  border-color: var(--c-primary-border);
}

.wcol.picked {
  box-shadow: inset 0 0 0 1px var(--c-primary-border);
}

.wcol-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 4px;
  padding: 5px 6px;
  border-bottom: 1px solid var(--c-border);
  font-size: 11.5px;
  color: var(--c-text-2);
  cursor: pointer;
}

.wcol-head .weekend {
  color: var(--c-dim);
}

.wcol-head strong {
  font-size: 12px;
  color: var(--c-text);
}

.wcol-body {
  padding: 4px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 180px;
}

.wcard {
  border-radius: 6px;
  border: 1px solid var(--cat-border);
  border-left: 3px solid var(--cat);
  background: var(--cat-bg);
  padding: 4px 5px;
  cursor: pointer;
  min-width: 0;
}

.wcard.done {
  opacity: 0.6;
}

.wcard.overdue {
  border-color: var(--c-not-met-border);
}

.wcard.policy {
  background: transparent;
  border-style: dashed;
  border-left-style: dashed;
}

.wcard-top {
  display: flex;
  justify-content: space-between;
  gap: 4px;
  align-items: baseline;
}

.wtag {
  font-size: 10px;
  color: var(--cat);
  font-weight: 600;
}

.wcard-title {
  font-size: 11.5px;
  line-height: 1.4;
  margin: 2px 0;
  word-break: break-word;
}

.wcard.done .wcard-title {
  text-decoration: line-through;
}

.wempty {
  margin: 0;
  text-align: center;
  color: var(--c-dim);
  font-size: 11px;
}

/* ---------- 列表视图 ---------- */
.list-empty {
  padding: var(--space-8) 0;
}

.lgroup {
  border-top: 1px solid var(--c-border);
  padding: var(--space-2) 0;
}

.lgroup:first-of-type {
  border-top: none;
}

.ldate {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  padding: 0 var(--space-2) var(--space-1);
}

.ldate strong {
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.lrow {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  padding: var(--space-2);
  border-radius: var(--radius-control);
  border-left: 3px solid var(--cat);
  cursor: pointer;
}

.lrow:hover {
  background: var(--c-surface-2);
}

.lrow.done {
  opacity: 0.62;
}

.lrow.done .ltitle {
  text-decoration: line-through;
}

.lrow.overdue .ltime {
  color: var(--c-not-met);
}

.ltime {
  width: 44px;
  flex: none;
  font-size: 11.5px;
  color: var(--c-text-2);
}

.ltag {
  flex: none;
  font-size: 11px;
  color: var(--cat);
  background: var(--cat-bg);
  border-radius: 999px;
  padding: 0 7px;
}

.ltitle {
  font-size: 13px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lmeta {
  margin-left: auto;
  white-space: nowrap;
}

/* ---------- 右侧 ---------- */
.side-card {
  padding: var(--space-3);
}

.side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}

.side-head h3 {
  margin: 0;
  font-size: 13.5px;
}

.tiny-btn {
  min-height: 24px;
  font-size: 11.5px;
  padding: 0 var(--space-2);
}

.picked-day {
  margin: 0 0 var(--space-2);
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 11.5px;
}

.day-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.drow {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2);
  border-radius: var(--radius-control);
  border-left: 3px solid var(--cat);
  cursor: pointer;
}

.drow:hover {
  background: var(--c-surface-2);
}

.drow.done {
  opacity: 0.65;
}

.drow.done .dtitle {
  text-decoration: line-through;
}

.dtime {
  font-size: 11px;
  color: var(--c-text-2);
  flex: none;
  width: 38px;
  padding-top: 1px;
}

.dtitle {
  font-size: 12.5px;
  line-height: 1.45;
}

.grow {
  flex: 1;
  min-width: 0;
}

.flag {
  flex: none;
  font-size: 10px;
  border-radius: 999px;
  padding: 0 6px;
  line-height: 16px;
}

.flag.danger {
  color: var(--c-not-met);
  background: var(--c-not-met-bg);
  border: 1px solid var(--c-not-met-border);
}

.flag.ok {
  color: var(--c-met);
  background: var(--c-met-bg);
  border: 1px solid var(--c-met-border);
}

.detail-cat {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--cat);
  font-weight: 600;
}

.detail-cat .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--cat);
}

.detail-cat .sep {
  color: var(--c-dim);
  font-weight: 400;
}

.detail-title {
  margin: var(--space-1) 0 var(--space-2);
  font-size: 14px;
  line-height: 1.5;
}

.kv {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 4px var(--space-2);
  margin: 0 0 var(--space-2);
  font-size: 12px;
}

.kv dt {
  color: var(--c-text-2);
}

.kv dd {
  margin: 0;
  min-width: 0;
  word-break: break-word;
}

.date-src {
  margin-left: 6px;
  font-size: 10.5px;
}

.raw {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  color: var(--c-unknown);
}

.link {
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  color: var(--c-primary);
  cursor: pointer;
  text-align: left;
}

.link:hover {
  text-decoration: underline;
}

.more-link {
  font-size: 11.5px;
  margin-top: 2px;
}

.note {
  margin: var(--space-2) 0;
  font-size: 11.5px;
  color: var(--c-unknown);
  background: var(--c-warn-bg);
  border-left: 2px solid var(--c-warn-border);
  padding: 4px var(--space-2);
  border-radius: 4px;
}

.date-editor {
  margin-top: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--c-border);
}

.date-editor label {
  display: block;
  font-size: 11.5px;
  color: var(--c-text-2);
  margin-bottom: 4px;
}

.date-editor .row {
  display: flex;
  gap: var(--space-2);
}

.input {
  min-height: 34px;
  padding: 0 var(--space-2);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-surface);
  font-family: inherit;
  font-size: 13px;
  color: var(--c-text);
  min-width: 0;
}

.date-editor .input {
  flex: 1;
}

.detail-actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-top: var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px solid var(--c-border);
}

.danger-btn {
  color: var(--c-not-met);
}

.ugroup {
  margin-top: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--c-border);
}

.uhead {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.utag {
  font-size: 11px;
  padding: 0 7px;
  border-radius: 999px;
  border: 1px solid var(--c-warn-border);
  background: var(--c-warn-bg);
  color: var(--c-unknown);
}

.utag.not_set {
  border-color: var(--c-border);
  background: var(--c-surface-2);
  color: var(--c-text-2);
}

.urow {
  display: flex;
  gap: var(--space-2);
  padding: 5px var(--space-2);
  border-radius: 6px;
  border-left: 2px solid var(--cat);
  cursor: pointer;
  margin-top: 2px;
}

.urow:hover {
  background: var(--c-surface-2);
}

.utitle {
  font-size: 12px;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ---------- 弹层 ---------- */
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(18, 48, 43, 0.32);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 60;
  padding: var(--space-4);
}

.dialog {
  width: min(440px, 100%);
  box-shadow: var(--shadow-pop);
}

.dialog h3 {
  margin: 0 0 4px;
}

.field {
  display: block;
  margin-top: var(--space-3);
}

.field > span {
  display: block;
  font-size: 11.5px;
  color: var(--c-text-2);
  margin-bottom: 4px;
}

.field .input {
  width: 100%;
  box-sizing: border-box;
}

textarea.input {
  padding: var(--space-2);
  resize: vertical;
}

.field-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-2);
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-4);
}

.err-line {
  color: var(--c-not-met);
  font-size: 13px;
  margin: 0;
}

@media (max-width: 1180px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .side {
    position: static;
    max-height: none;
  }
}

@media (max-width: 760px) {
  .month-grid {
    grid-auto-rows: minmax(72px, 1fr);
  }
  .pill-text {
    display: none;
  }
  .week-grid {
    grid-template-columns: 1fr;
  }
  .wcol-body {
    min-height: 0;
  }
}
</style>
